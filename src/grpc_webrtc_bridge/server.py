import argparse
import asyncio
import logging
import sys
import time
from collections import deque
from queue import Queue
import os

# from multiprocessing import Process, Queue,
from threading import Semaphore, Thread

# from queue import Queue
import prometheus_client as pc
import gi
from gst_signalling import GstSignallingProducer
from gst_signalling.gst_abstract_role import GstSession
from reachy2_sdk_api.webrtc_bridge_pb2 import (
    AnyCommands,
    Connect,
    ConnectionStatus,
    ServiceRequest,
    ServiceResponse,
)

from .grpc_client import GRPCClient

gi.require_version("Gst", "1.0")

from gi.repository import GLib, Gst, GstWebRTC  # noqa : E402

last_freq_counter = {"neck": 0, "r_arm": 0, "l_arm": 0, "r_hand": 0, "l_hand": 0, "mobile_base": 0}
last_freq_update = {
    "neck": time.time(),
    "r_arm": time.time(),
    "l_arm": time.time(),
    "r_hand": time.time(),
    "l_hand": time.time(),
    "mobile_base": time.time(),
}
last_drop_counter = 0
freq_rates = []
drop_rates = []
drop_array = []
reentrancte_counter = 0
parse_time_arr = []
test_time_arr = []
init = False
# std_queue = deque(maxlen=1)
important_queue = Queue()
std_queue = {
    "neck": deque(maxlen=1),
    "r_arm": deque(maxlen=1),
    "l_arm": deque(maxlen=1),
    "r_hand": deque(maxlen=1),
    "l_hand": deque(maxlen=1),
    "mobile_base": deque(maxlen=1),
}
# important_queue = {
#     "neck": Queue(),
#     "r_arm": Queue(),
#     "l_arm": Queue(),
#     "r_hand": Queue(),
#     "l_hand": Queue(),
#     "mobile_base": Queue(),
# }


class GRPCWebRTCBridge:
    def __init__(self, args: argparse.Namespace) -> None:
        self.logger = logging.getLogger(__name__)
        pc.start_http_server(10001)
        # self.sum_time_important_commands = pc.Summary('webrtcbridge_time_important_commands',
        #                                               'Time spent during handle important commands')
        self.counter_all_commands = pc.Counter("webrtcbridge_all_commands", "Amount of commands received")
        self.counter_important_commands = pc.Counter("webrtcbridge_important_commands", "Amount of important commands received")
        self.counter_dropped_commands = pc.Counter("webrtcbridge_dropped_commands", "Amount of commands dropped")

        self.producer = GstSignallingProducer(
            host=args.webrtc_signaling_host,
            port=args.webrtc_signaling_port,
            name="grpc_webrtc_bridge",
        )
        # self.smart_lock = Lock()
        self.smart_lock = Semaphore(1)

        @self.producer.on("new_session")  # type: ignore[misc]
        def on_new_session(session: GstSession) -> None:
            pc = session.pc

            grpc_client = GRPCClient(args.grpc_host, args.grpc_port)

            data_channel = pc.emit("create-data-channel", "service", None)
            if data_channel:
                data_channel.connect("on-open", self.on_open_service_channel)
                data_channel.connect("on-message-data", self.on_data_service_channel, grpc_client, pc)
            else:
                self.logger.error("Failed to create data channel")

    def on_open_service_channel(self, channel: GstWebRTC.WebRTCDataChannel) -> None:
        self.logger.info("channel service opened")

    def on_data_service_channel(
        self, data_channel: GstWebRTC.WebRTCDataChannel, message: GLib.Bytes, grpc_client: GRPCClient, pc: Gst.Element
    ) -> None:
        self.logger.info(f"Message from DataChannel: {message}")
        request = ServiceRequest()
        request.ParseFromString(message.get_data())

        asyncio.run_coroutine_threadsafe(
            self.handle_service_request(request, data_channel, grpc_client, pc), self.producer._asyncloop
        )

    async def serve4ever(self) -> None:
        await self.producer.serve4ever()

    async def close(self) -> None:
        await self.producer.close()

    # Handle service request
    async def handle_service_request(
        self, request: ServiceRequest, data_channel: GstWebRTC.WebRTCDataChannel, grpc_client: GRPCClient, pc: Gst.Element
    ) -> None:
        self.logger.info(f"Received service request message: {request}")

        if request.HasField("get_reachy"):
            resp = self.handle_get_reachy_request(grpc_client)

            self.logger.info(f"Sending service response message: {resp}")
            byte_data = resp.SerializeToString()
            gbyte_data = GLib.Bytes.new(byte_data)
            data_channel.send_data(gbyte_data)

        elif request.HasField("connect"):
            resp = await self.handle_connect_request(request.connect, grpc_client, pc)

        elif request.HasField("disconnect"):
            resp = await self.handle_disconnect_request()

    def handle_get_reachy_request(self, grpc_client: GRPCClient) -> ServiceResponse:
        reachy = grpc_client.get_reachy()

        resp = ServiceResponse(
            connection_status=ConnectionStatus(
                connected=True,
                state_channel=f"reachy_state_{reachy.id.id}",
                command_channel=f"reachy_command_{reachy.id.id}",
                reachy=reachy,
            ),
        )

        return resp

    def on_open_state_channel(self, channel: GstWebRTC.WebRTCDataChannel, request: Connect, grpc_client: GRPCClient) -> None:
        self.logger.info("channel state opened")

        async def send_joint_state() -> None:
            async for state in grpc_client.get_reachy_state(
                request.reachy_id,
                request.update_frequency,
            ):
                byte_data = state.SerializeToString()
                gbyte_data = GLib.Bytes.new(byte_data)
                channel.send_data(gbyte_data)

        asyncio.run_coroutine_threadsafe(send_joint_state(), self.producer._asyncloop)

    async def handle_connect_request(self, request: Connect, grpc_client: GRPCClient, pc: Gst.Element) -> ServiceResponse:
        # Create state data channel and start sending state
        if request.update_frequency >= 1000:
            max_packet_lifetime = 1
        else:
            max_packet_lifetime = (int)(1000 // request.update_frequency)

        channel_options = Gst.Structure.new_empty("application/x-data-channel")
        channel_options.set_value("max-packet-lifetime", max_packet_lifetime)
        data_channel_state = pc.emit("create-data-channel", f"reachy_state_{request.reachy_id.id}", channel_options)
        if data_channel_state:
            data_channel_state.connect("on-open", self.on_open_state_channel, request, grpc_client)
        else:
            self.logger.error("Failed to create data channel state")

        channel_options = Gst.Structure.new_empty("application/x-data-channel")
        channel_options.set_value("priority", GstWebRTC.WebRTCPriorityType.HIGH)
        reachy_command_datachannel = pc.emit("create-data-channel", f"reachy_command_{request.reachy_id.id}", channel_options)
        if reachy_command_datachannel:
            reachy_command_datachannel.connect("on-message-data", self.on_reachy_command_datachannel_message, grpc_client)
        else:
            self.logger.error("Failed to create data channel command")

        return ServiceResponse()

    def on_reachy_command_datachannel_message(
        self, data_channel: GstWebRTC.WebRTCDataChannel, message: GLib.Bytes, grpc_client: GRPCClient
    ) -> None:
        global last_freq_counter
        global last_freq_update
        global last_drop_counter
        global freq_rates
        global drop_rates
        global init
        global drop_array
        global reentrancte_counter
        global parse_time_arr
        global test_time_arr
        global msg_queue
        reentrancte_counter += 1

        commands = AnyCommands()
        commands.ParseFromString(message.get_data())

        if not commands.commands:
            self.logger.waring("No command or incorrect message received {message}")
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
        ###############################
        important_msgs = 0
        dropped_msg = 0

        for cmd in commands.commands:
            if cmd.HasField("arm_command"):
                part_command = cmd.arm_command
                important_fields = ["turn_on", "turn_off", "speed_limit", "torque_limit"]
                for field in important_fields:
                    important_msgs += part_command.HasField(field)
                    part_command.ClearField(field)

            elif cmd.HasField("hand_command"):
                part_command = cmd.hand_command
                important_fields = ["turn_on", "turn_off"]
                for field in important_fields:
                    important_msgs += part_command.HasField(field)
                    part_command.ClearField(field)

            elif cmd.HasField("neck_command"):
                part_command = cmd.neck_command
                important_fields = ["turn_on", "turn_off"]
                for field in important_fields:
                    important_msgs += part_command.HasField(field)
                    part_command.ClearField(field)

            elif cmd.HasField("mobile_base_command"):
                part_command = cmd.mobile_base_command
                important_fields = ["mobile_base_mode"]
                for field in important_fields:
                    important_msgs += part_command.HasField(field)
                    part_command.ClearField(field)

        self.counter_important_commands.inc(important_msgs)

        if not important_msgs:
            for cmd in commands.commands:
                if cmd.HasField("arm_command"):
                    elem = std_queue[cmd.arm_command.arm_cartesian_goal.id.name]
                    dropped_msg += bool(elem)
                    elem.append(cmd.arm_command)
                if cmd.HasField("hand_command"):
                    elem = std_queue[cmd.hand_command.hand_goal.id.name]
                    dropped_msg += bool(elem)
                    elem.append(cmd.hand_command)
                if cmd.HasField("neck_command"):
                    elem = std_queue["neck"]
                    dropped_msg += bool(elem)
                    elem.append(cmd.neck_command)
                if cmd.HasField("mobile_base_command"):
                    elem = std_queue["mobile_base"]
                    dropped_msg += bool(elem)
                    elem.append(cmd.mobile_base_command)
        else:
            important_queue.put(commands_important)
            # self.logger.debug(f"important: {commands_important}")

        self.counter_dropped_commands.inc(dropped_msg)

        reentrancte_counter -= 1

    async def handle_disconnect_request(self) -> ServiceResponse:
        # TODO: implement me
        self.logger.info("Disconnecting...")

        return ServiceResponse()


def msg_handling(message, logger, part_name, part_handler, summary):
    global last_freq_counter
    global last_freq_update

    last_freq_counter[part_name] += 1

    with summary.time():
        part_handler(message)

    now = time.time()
    if now - last_freq_update[part_name] > 1:
        current_freq_rate = int(last_freq_counter[part_name] / (now - last_freq_update[part_name]))

        logger.info(f"Freq {part_name} {current_freq_rate} Hz")

        last_freq_counter[part_name] = 0
        last_freq_update[part_name] = now


####################
# Routines


def handle_std_queue_routine(std_queue, part_name, part_handler):
    logger = logging.getLogger(__name__)
    sum_part = pc.Summary(f"webrtcbridge_commands_time_{part_name}", f"Time spent during {part_name} commands")

    while True:
        try:
            msg = std_queue.pop()
            msg_handling(msg, logger, part_name, part_handler, sum_part)
        except IndexError:
            time.sleep(0.001)


def handle_important_queue_routine(grpc_client, logger):
    sum_important = pc.Summary("webrtcbridge_commands_time_important", "Time spent during important commands")
    while True:
        msg = important_queue.get()
        with sum_important.time():
            grpc_client.handle_commands(msg)


####################
# MAIN


def main() -> int:  # noqa: C901
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

    bridge = GRPCWebRTCBridge(args)

    time.sleep(2)

    grpc_client = GRPCClient(args.grpc_host, args.grpc_port)
    part_handlers = {
        "neck": grpc_client.handle_neck_command,
        "r_arm": grpc_client.handle_arm_command,
        "l_arm": grpc_client.handle_arm_command,
        "r_hand": grpc_client.handle_hand_command,
        "l_hand": grpc_client.handle_hand_command,
        "mobile_base": grpc_client.handle_mobile_base_command,
    }

    for part in std_queue.keys():
        Thread(
            target=handle_std_queue_routine,
            args=(
                std_queue[part],
                part,
                part_handlers[part],
            ),
        ).start()

    Thread(
        target=handle_important_queue_routine,
        args=(
            grpc_client,
            bridge.logger,
        ),
    ).start()

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
