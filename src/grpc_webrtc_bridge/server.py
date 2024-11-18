import argparse
import asyncio
import logging
import os
import sys
import time
from collections import deque
from queue import Empty, Queue

# from multiprocessing import Process, Queue,
from threading import Event, Thread
from typing import Callable, Dict, List

import gi

# from queue import Queue
import prometheus_client as prc
import reachy2_monitoring as rm
from gst_signalling import GstSignallingProducer
from gst_signalling.gst_abstract_role import GstSession
from reachy2_sdk_api.webrtc_bridge_pb2 import (
    AnyCommand,
    AnyCommands,
    Connect,
    ConnectionStatus,
    ServiceRequest,
    ServiceResponse,
)

from .grpc_client import GRPCClient

gi.require_version("Gst", "1.0")

from gi.repository import GLib, Gst, GstWebRTC  # noqa : E402


class GRPCWebRTCBridge:
    """Class that forwards commands from a gstreamer webrtc data channel to a grpc server.

    It can accept commands from multiple clients and open a grpc client for each.
    It is not recommended to have multiple clients that sends commands though (i.e. only one for teleoperation).
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.logger = logging.getLogger(__name__)
        prc.start_http_server(10001)

        NODE_NAME = "grpc-webrtc_bridge"
        rm.configure_pyroscope(
            NODE_NAME,
            tags={
                "server": "false",
                "client": "true",
            },
        )
        self.tracer = rm.tracer(NODE_NAME, grpc_type="client")

        self.counter_all_commands = prc.Counter("webrtcbridge_all_commands", "Amount of commands received")
        self.counter_important_commands = prc.Counter(
            "webrtcbridge_important_commands", "Amount of important commands received"
        )
        self.counter_dropped_commands = prc.Counter("webrtcbridge_dropped_commands", "Amount of commands dropped")

        self.sum_important = prc.Summary("webrtcbridge_commands_time_important", "Time spent during important commands")
        self.sum_part = {
            "neck": prc.Summary("webrtcbridge_commands_time_neck", "Time spent during neck commands"),
            "r_arm": prc.Summary("webrtcbridge_commands_time_r_arm", "Time spent during r_arm commands"),
            "l_arm": prc.Summary("webrtcbridge_commands_time_l_arm", "Time spent during l_arm commands"),
            "r_hand": prc.Summary("webrtcbridge_commands_time_r_hand", "Time spent during r_hand commands"),
            "l_hand": prc.Summary("webrtcbridge_commands_time_l_hand", "Time spent during l_hand commands"),
            "mobile_base": prc.Summary("webrtcbridge_commands_time_mobile_base", "Time spent during mobile_base commands"),
        }

        self.important_queue: Queue[AnyCommands] = Queue()
        self.std_queue: Dict[str, deque[AnyCommand]] = {
            "neck": deque(maxlen=1),
            "r_arm": deque(maxlen=1),
            "l_arm": deque(maxlen=1),
            "r_hand": deque(maxlen=1),
            "l_hand": deque(maxlen=1),
            "mobile_base": deque(maxlen=1),
        }

        self.producer = GstSignallingProducer(
            host=args.webrtc_signaling_host,
            port=args.webrtc_signaling_port,
            name="grpc_webrtc_bridge",
        )

        self._threads_running: Dict[str, Event] = {}
        self._gloop = GLib.MainLoop()
        self._thread_bus_calls = Thread(target=self._handle_bus_calls, daemon=True)
        self._thread_bus_calls.start()

        @self.producer.on("new_session")  # type: ignore[misc]
        def on_new_session(session: GstSession) -> None:
            pc = session.pc

            grpc_client = GRPCClient(args.grpc_host, args.grpc_port, self.tracer)
            ########################################################################
            # NOTE used for testing code that instantiates multiple grpc servers
            # grpc_clients = []
            # for nn in range(0, 6):
            #     grpc_clients.append(GRPCClient(args.grpc_host, args.grpc_port+nn, None))
            # part_handlers = {
            #     "neck": grpc_clients[0].handle_neck_command,
            #     "r_arm": grpc_clients[1].handle_arm_command,
            #     "l_arm": grpc_clients[2].handle_arm_command,
            #     "r_hand": grpc_clients[3].handle_hand_command,
            #     "l_hand": grpc_clients[4].handle_hand_command,
            #     "mobile_base": grpc_clients[5].handle_mobile_base_command,
            # }
            # grpc_client = grpc_clients[-1]
            ########################################################################

            important_queue: Queue[AnyCommands] = Queue()
            std_queue: Dict[str, deque[AnyCommand]] = {
                "neck": deque(maxlen=1),
                "r_arm": deque(maxlen=1),
                "l_arm": deque(maxlen=1),
                "r_hand": deque(maxlen=1),
                "l_hand": deque(maxlen=1),
                "mobile_base": deque(maxlen=1),
            }

            self.configure_grpc_client_threads(grpc_client, session, important_queue, std_queue)

            data_channel = pc.emit("create-data-channel", "service", None)

            if data_channel:
                data_channel.connect("on-open", self.on_open_service_channel)
                data_channel.connect("on-close", self.on_close_service_channel, important_queue, grpc_client, session)
                data_channel.connect(
                    "on-message-data", self.on_data_service_channel, grpc_client, session, important_queue, std_queue
                )
            else:
                self.logger.error("Failed to create data channel")

        @self.producer.on("close_session")  # type: ignore[misc]
        def on_session_closed(session: GstSession) -> None:
            self.logger.info(f"Session closed. peer id {session.peer_id}")
            thread_cancel = self._threads_running.pop(session.peer_id)
            thread_cancel.set()

    def configure_grpc_client_threads(
        self,
        grpc_client: GRPCClient,
        session: GstSession,
        important_queue: Queue[AnyCommands],
        std_queue: Dict[str, deque[AnyCommand]],
    ) -> None:
        part_handlers = {
            "neck": grpc_client.handle_neck_command,
            "r_arm": grpc_client.handle_arm_command,
            "l_arm": grpc_client.handle_arm_command,
            "r_hand": grpc_client.handle_hand_command,
            "l_hand": grpc_client.handle_hand_command,
            "mobile_base": grpc_client.handle_mobile_base_command,
        }

        last_freq_counter = {"neck": 0, "r_arm": 0, "l_arm": 0, "r_hand": 0, "l_hand": 0, "mobile_base": 0}
        self.last_drop_counter = {"neck": 0, "r_arm": 0, "l_arm": 0, "r_hand": 0, "l_hand": 0, "mobile_base": 0}
        last_freq_update = {
            "neck": time.time(),
            "r_arm": time.time(),
            "l_arm": time.time(),
            "r_hand": time.time(),
            "l_hand": time.time(),
            "mobile_base": time.time(),
        }

        thread_cancel = Event()
        self._threads_running[session.peer_id] = thread_cancel

        for part in std_queue.keys():
            Thread(
                target=self.handle_std_queue_routine,
                args=(
                    std_queue[part],
                    part,
                    part_handlers[part],
                    last_freq_counter,
                    last_freq_update,
                    thread_cancel,
                ),
                daemon=True,
            ).start()

        Thread(
            target=self.handle_important_queue_routine,
            args=(
                grpc_client,
                important_queue,
                thread_cancel,
            ),
            daemon=True,
        ).start()

    def on_open_service_channel(self, channel: GstWebRTC.WebRTCDataChannel) -> None:
        self.logger.info("channel service opened")

    def on_close_service_channel(
        self,
        channel: GstWebRTC.WebRTCDataChannel,
        important_queue: Queue[AnyCommands],
        grpc_client: GRPCClient,
        session: GstSession,
    ) -> None:
        self.logger.info("channel service closed")
        asyncio.run_coroutine_threadsafe(grpc_client.close(), self.producer._asyncloop)

    def on_data_service_channel(
        self,
        data_channel: GstWebRTC.WebRTCDataChannel,
        message: GLib.Bytes,
        grpc_client: GRPCClient,
        session: GstSession,
        important_queue: Queue[AnyCommands],
        std_queue: Dict[str, deque[AnyCommand]],
    ) -> None:
        self.logger.debug(f"Message from DataChannel: {message}")
        request = ServiceRequest()
        request.ParseFromString(message.get_data())

        asyncio.run_coroutine_threadsafe(
            self.handle_service_request(request, data_channel, grpc_client, session, important_queue, std_queue),
            self.producer._asyncloop,
        )

    async def serve4ever(self) -> None:
        await self.producer.serve4ever()

    async def close(self) -> None:
        self.logger.info("Close bridge")
        self._gloop.quit()
        self._thread_bus_calls.join()
        await self.producer.close()

    # Handle service request
    async def handle_service_request(
        self,
        request: ServiceRequest,
        data_channel: GstWebRTC.WebRTCDataChannel,
        grpc_client: GRPCClient,
        session: GstSession,
        important_queue: Queue[AnyCommands],
        std_queue: Dict[str, deque[AnyCommand]],
    ) -> None:
        self.logger.debug(f"Received service request message: {request}")

        if request.HasField("get_reachy"):
            resp = await self.handle_get_reachy_request(grpc_client)
            byte_data = resp.SerializeToString()
            gbyte_data = GLib.Bytes.new(byte_data)
            data_channel.send_data(gbyte_data)

        elif request.HasField("connect"):
            resp = await self.handle_connect_request(request.connect, grpc_client, session, important_queue, std_queue)

        elif request.HasField("disconnect"):
            resp = await self.handle_disconnect_request()

    async def handle_get_reachy_request(self, grpc_client: GRPCClient) -> ServiceResponse:
        self.logger.debug("Ask for reachy state")
        reachy = await grpc_client.get_reachy()
        resp = ServiceResponse(
            connection_status=ConnectionStatus(
                connected=True,
                state_channel=f"reachy_state_{reachy.id.id}",
                command_channel=f"reachy_command_{reachy.id.id}",
                reachy=reachy,
            ),
        )

        return resp

    def on_open_state_channel(self, channel: GstWebRTC.WebRTCDataChannel) -> None:
        self.logger.debug("channel state opened")

    def on_close_state_channel(self, channel: GstWebRTC.WebRTCDataChannel) -> None:
        self.logger.debug("channel state closed")

    def on_close_audit_channel(self, channel: GstWebRTC.WebRTCDataChannel) -> None:
        self.logger.debug("channel audit status closed")

    def on_open_audit_channel(self, channel: GstWebRTC.WebRTCDataChannel) -> None:
        self.logger.debug("channel audit status opened")

    def on_error_state_channel(self, channel: GstWebRTC.WebRTCDataChannel, error: GLib.Error) -> None:
        self.logger.error(f"Error on state channel: {error.message}")

    def on_error_audit_channel(self, channel: GstWebRTC.WebRTCDataChannel, error: GLib.Error) -> None:
        self.logger.error(f"Error on audit status channel: {error.message}")

    def on_error_commands_channel(self, channel: GstWebRTC.WebRTCDataChannel, error: GLib.Error) -> None:
        self.logger.error(f"Error on commands channel: {error.message}")

    async def _send_joint_state(self, channel: GstWebRTC.WebRTCDataChannel, request: Connect, grpc_client: GRPCClient) -> None:
        # span_links = rm.span_links([rm.trace.get_current_span().get_span_context()])
        # with self.tracer.start_as_current_span(f"_send_joint_state",
        #                                        kind=rm.trace.SpanKind.INTERNAL,
        #                                        context=rm.otel_rootctx,
        #                                        links=span_links,
        #                                        ):
        self.logger.info("start streaming state")

        try:
            async for state in grpc_client.get_reachy_state(
                request.reachy_id,
                request.update_frequency,
            ):
                byte_data = state.SerializeToString()
                gbyte_data = GLib.Bytes.new(byte_data)
                channel.send_data(gbyte_data)
        except Exception as e:
            self.logger.error(f"Error while streaming state: {e}")

        self.logger.debug("leaving streaming state")

    async def _send_joint_audit_status(
        self, channel: GstWebRTC.WebRTCDataChannel, request: Connect, grpc_client: GRPCClient
    ) -> None:
        # span_links = rm.span_links([rm.trace.get_current_span().get_span_context()])
        # with self.tracer.start_as_current_span(f"_send_joint_audit_status",
        #                                        kind=rm.trace.SpanKind.INTERNAL,
        #                                        context=rm.otel_rootctx,
        #                                        links=span_links,
        #                                        ):
        self.logger.info("start streaming audit status")
        try:
            async for state in grpc_client.get_reachy_audit_status(
                request.reachy_id,
                request.audit_frequency,
            ):
                byte_data = state.SerializeToString()
                gbyte_data = GLib.Bytes.new(byte_data)
                channel.send_data(gbyte_data)
        except Exception as e:
            self.logger.error(f"Error while streaming audit state: {e}")
        self.logger.debug("leaving streaming audit state")

    async def handle_connect_request(
        self,
        request: Connect,
        grpc_client: GRPCClient,
        session: GstSession,
        important_queue: Queue[AnyCommands],
        std_queue: Dict[str, deque[AnyCommand]],
    ) -> ServiceResponse:
        # Create state data channel and start sending state

        if request.update_frequency >= 1000:
            max_packet_lifetime = 1
        else:
            max_packet_lifetime = (int)(1000 // request.update_frequency)

        channel_options = Gst.Structure.new_empty("application/x-data-channel")
        channel_options.set_value("max-packet-lifetime", max_packet_lifetime)

        data_channel_state = session.pc.emit("create-data-channel", f"reachy_state_{request.reachy_id.id}", channel_options)
        if data_channel_state:
            data_channel_state.connect("on-open", self.on_open_state_channel)
            data_channel_state.connect("on-close", self.on_close_state_channel)
            data_channel_state.connect("on-error", self.on_error_state_channel)
            asyncio.run_coroutine_threadsafe(
                self._send_joint_state(data_channel_state, request, grpc_client), self.producer._asyncloop
            )
        else:
            self.logger.error("Failed to create data channel state")

        audit_channel_state = session.pc.emit("create-data-channel", f"reachy_audit_{request.reachy_id.id}", channel_options)
        if audit_channel_state:
            audit_channel_state.connect("on-open", self.on_open_audit_channel)
            audit_channel_state.connect("on-close", self.on_close_audit_channel)
            audit_channel_state.connect("on-error", self.on_error_audit_channel)
            asyncio.run_coroutine_threadsafe(
                self._send_joint_audit_status(audit_channel_state, request, grpc_client),
                self.producer._asyncloop,
            )
        else:
            self.logger.error("Failed to create data channel state")

        channel_options = Gst.Structure.new_empty("application/x-data-channel")
        channel_options.set_value("priority", GstWebRTC.WebRTCPriorityType.HIGH)
        reachy_command_datachannel = session.pc.emit(
            "create-data-channel", f"reachy_command_{request.reachy_id.id}", channel_options
        )
        if reachy_command_datachannel:
            reachy_command_datachannel.connect(
                "on-message-data",
                self.on_reachy_command_datachannel_message,
                grpc_client,
                important_queue,
                std_queue,
            )
            reachy_command_datachannel.connect("on-error", self.on_error_commands_channel)
        else:
            self.logger.error("Failed to create data channel command")

        return ServiceResponse()

    @staticmethod
    def _process_important_fields(important_msgs: int, part_command: AnyCommand, important_fields: List[str]) -> int:
        for field in important_fields:
            important_msgs += part_command.HasField(field)
            part_command.ClearField(field)
        return important_msgs

    def _process_important_commands(self, commands: AnyCommands) -> int:
        important_msgs = 0
        for cmd in commands.commands:
            if cmd.HasField("arm_command"):
                important_msgs = GRPCWebRTCBridge._process_important_fields(
                    important_msgs, cmd.arm_command, ["turn_on", "turn_off", "speed_limit", "torque_limit"]
                )
            elif cmd.HasField("hand_command"):
                important_msgs = GRPCWebRTCBridge._process_important_fields(
                    important_msgs, cmd.hand_command, ["turn_on", "turn_off"]
                )
            elif cmd.HasField("neck_command"):
                important_msgs = GRPCWebRTCBridge._process_important_fields(
                    important_msgs, cmd.neck_command, ["turn_on", "turn_off", "speed_limit", "torque_limit"]
                )
            elif cmd.HasField("mobile_base_command"):
                important_msgs = GRPCWebRTCBridge._process_important_fields(
                    important_msgs, cmd.mobile_base_command, ["mobile_base_mode"]
                )
            else:
                self.logger.warning(f"Important command not processed: {cmd}")
        return important_msgs

    def _insert_or_drop(self, std_queue: Dict[str, deque[AnyCommand]], queue_name: str, command: AnyCommand) -> bool:
        dropped = True
        try:
            elem = std_queue[queue_name]
            dropped = bool(elem)  # True means len(element) > 0
            elem.append(command)  # drop current element if any (maxlen=1)
        except KeyError:
            self.logger.warning(f"Dropping invalid command : {queue_name}")
        return dropped

    @staticmethod
    def _create_important_commands(commands: AnyCommands) -> AnyCommands:
        commands_important = AnyCommands()
        commands_important.CopyFrom(commands)
        for cmd in commands_important.commands:
            if cmd.HasField("arm_command"):
                cmd.arm_command.ClearField("arm_cartesian_goal")
            elif cmd.HasField("hand_command"):
                cmd.hand_command.ClearField("hand_goal")
            elif cmd.HasField("neck_command"):
                cmd.neck_command.ClearField("neck_goal")
            elif cmd.HasField("mobile_base_command"):
                cmd.mobile_base_command.ClearField("target_direction")
        return commands_important

    def on_reachy_command_datachannel_message(
        self,
        data_channel: GstWebRTC.WebRTCDataChannel,
        message: GLib.Bytes,
        grpc_client: GRPCClient,
        important_queue: Queue[AnyCommands],
        std_queue: Dict[str, deque[AnyCommand]],
    ) -> None:
        commands = AnyCommands()
        commands.ParseFromString(message.get_data())

        if not commands.commands:
            self.logger.warning("No command or incorrect message received {message}")
            return

        self.counter_all_commands.inc(len(commands.commands))
        ###############################
        # TODO better, temporary message priority
        # HACK: split important and non-important commands to avoid
        # potentially executing old goal commands (when important commands
        # are handled)
        # this will leave only non-important commands in the received commands
        # in the future, important and non-important commands should be separated
        # in different proto messages
        commands_important = GRPCWebRTCBridge._create_important_commands(commands)
        ###############################
        dropped_msg = 0
        important_msgs = self._process_important_commands(commands)
        self.counter_important_commands.inc(important_msgs)

        if not important_msgs:
            for cmd in commands.commands:
                if cmd.HasField("arm_command"):
                    dropped = self._insert_or_drop(std_queue, cmd.arm_command.arm_cartesian_goal.id.name, cmd.arm_command)
                    if dropped:
                        self.last_drop_counter[cmd.arm_command.arm_cartesian_goal.id.name] += 1
                    dropped_msg += dropped
                elif cmd.HasField("hand_command"):
                    dropped = self._insert_or_drop(std_queue, cmd.hand_command.hand_goal.id.name, cmd.hand_command)
                    if dropped:
                        self.last_drop_counter[cmd.hand_command.hand_goal.id.name] += 1
                    dropped_msg += dropped
                elif cmd.HasField("neck_command"):
                    dropped = self._insert_or_drop(std_queue, "neck", cmd.neck_command)
                    if dropped:
                        self.last_drop_counter["neck"] += 1
                    dropped_msg += dropped
                elif cmd.HasField("mobile_base_command"):
                    dropped = self._insert_or_drop(std_queue, "mobile_base", cmd.mobile_base_command)
                    if dropped:
                        self.last_drop_counter["mobile_base"] += 1
                    dropped_msg += dropped
                else:
                    self.logger.warning(f"Unknown command: {cmd}")
        else:
            important_queue.put(commands_important)
            self.logger.debug(f"important: {commands_important}")

        self.counter_dropped_commands.inc(dropped_msg)

    async def handle_disconnect_request(self) -> ServiceResponse:
        # TODO: implement me
        self.logger.info("Disconnecting...")

        return ServiceResponse()

    def msg_handling(
        self,
        message: AnyCommand,
        part_name: str,
        part_handler: Callable[[GRPCClient], None],
        summary: prc.Summary,
        last_freq_counter: Dict[str, int],
        last_freq_update: Dict[str, float],
    ) -> None:
        last_freq_counter[part_name] += 1

        with summary.time():
            part_handler(message)

        now = time.time()
        if now - last_freq_update[part_name] > 1:
            current_freq_rate = int(last_freq_counter[part_name] / (now - last_freq_update[part_name]))
            current_drop_rate = int(self.last_drop_counter[part_name] / (now - last_freq_update[part_name]))
            self.logger.info(f"Freq {part_name} {current_freq_rate} Hz\tDropped {current_drop_rate} Hz")

            last_freq_counter[part_name] = 0
            self.last_drop_counter[part_name] = 0
            last_freq_update[part_name] = now

    ####################
    # Routines
    def handle_std_queue_routine(
        self,
        std_queue: deque[AnyCommand],
        part_name: str,
        part_handler: Callable[[GRPCClient], None],
        last_freq_counter: Dict[str, int],
        last_freq_update: Dict[str, float],
        thread_cancel: Event,
    ) -> None:
        while not thread_cancel.is_set():
            try:
                msg = std_queue.pop()
                self.msg_handling(
                    msg,
                    part_name,
                    part_handler,
                    self.sum_part[part_name],
                    last_freq_counter,
                    last_freq_update,
                )
            except IndexError:
                time.sleep(0.001)
            except Exception as e:
                self.logger.error(f"Error while handling {part_name} commands: {e}")
        self.logger.debug(f"Thread {part_name} closed")

    def handle_important_queue_routine(
        self, grpc_client: GRPCClient, important_queue: Queue[AnyCommands], thread_cancel: Event
    ) -> None:
        while not thread_cancel.is_set():
            try:
                msg = important_queue.get(timeout=1)
                with self.sum_important.time():
                    grpc_client.handle_commands(msg)
            except Empty:
                pass  # required to properly close the thread
            except Exception as e:
                self.logger.error(f"Error while handling important commands: {e}")
        self.logger.debug("Thread important queues closed")

    def bus_message_cb(self, bus: Gst.Bus, msg: Gst.Message, loop) -> bool:  # type: ignore[no-untyped-def]
        t = msg.type
        if t == Gst.MessageType.EOS:
            self.logger.error("End-of-stream")
            return False
        elif t == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            self.logger.error("Error: %s: %s" % (err, debug))
            loop.quit()
            return False
        elif t == Gst.MessageType.STATE_CHANGED:
            if isinstance(msg.src, Gst.Pipeline):
                old_state, new_state, pending_state = msg.parse_state_changed()
                self.logger.info(("Pipeline state changed from %s to %s." % (old_state.value_nick, new_state.value_nick)))
                if old_state.value_nick == "paused" and new_state.value_nick == "ready":
                    self.logger.info("stopping bus message loop")
                    loop.quit()
                    return False
        elif t == Gst.MessageType.LATENCY:
            if self.producer._pipeline:
                try:
                    self.producer._pipeline.recalculate_latency()
                except Exception as e:
                    self.logger.warning("failed to recalculate warning, exception: %s" % str(e))
        return True

    def _handle_bus_calls(self) -> None:
        self.logger.debug("Starting bus call loop")
        self.producer._pipeline.set_property("auto-flush-bus", True)
        bus = self.producer._pipeline.get_bus()
        bus.add_watch(GLib.PRIORITY_DEFAULT, self.bus_message_cb, self._gloop)
        self._gloop.run()
        bus.remove_watch()

        self.logger.debug("Stopping bus call loop")


def main() -> int:
    parser = argparse.ArgumentParser()

    # gRPC
    parser.add_argument(
        "--grpc-host",
        type=str,
        default="127.0.0.1",
        help="Host of the grpc server.",
    )
    parser.add_argument(
        "--grpc-port",
        type=int,
        default=50051,
        help="Port of the grpc server.",
    )

    # WebRTC
    parser.add_argument(
        "--webrtc-signaling-host",
        type=str,
        default="127.0.0.1",
        help="Host of the gstreamer webrtc signaling server.",
    )
    parser.add_argument(
        "--webrtc-signaling-port",
        type=int,
        default=8443,
        help="Port of the gstreamer webrtc signaling server.",
    )

    # Logging
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging.",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
        os.environ["GST_DEBUG"] = "3"
    else:
        logging.basicConfig(level=logging.INFO)

    bridge = GRPCWebRTCBridge(args)

    time.sleep(2)

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(bridge.serve4ever())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(bridge.close())

    return 0


if __name__ == "__main__":
    sys.exit(main())
