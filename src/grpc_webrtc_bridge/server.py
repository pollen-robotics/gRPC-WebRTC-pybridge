import argparse
import asyncio
import logging
import os
import sys
import time
from threading import Semaphore

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

last_freq_counter = 0
last_freq_update = time.time()
last_drop_counter = 0
freq_rates = []
drop_rates = []
init = False


class GRPCWebRTCBridge:
    def __init__(self, args: argparse.Namespace) -> None:
        self.logger = logging.getLogger(__name__)

        self.producer = GstSignallingProducer(
            host=args.webrtc_signaling_host,
            port=args.webrtc_signaling_port,
            name="grpc_webrtc_bridge",
        )
        # not needed anymore?
        # self.smart_lock = Lock()
        self.smart_lock = Semaphore(10)

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
            resp = await self.handle_get_reachy_request(grpc_client)

            self.logger.info(f"Sending service response message: {resp}")
            byte_data = resp.SerializeToString()
            gbyte_data = GLib.Bytes.new(byte_data)
            data_channel.send_data(gbyte_data)

        elif request.HasField("connect"):
            resp = await self.handle_connect_request(request.connect, grpc_client, pc)

        elif request.HasField("disconnect"):
            resp = await self.handle_disconnect_request()




    async def handle_get_reachy_request(self, grpc_client: GRPCClient) -> ServiceResponse:
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

    def on_reachy_command_datachannel_message(
        self, data_channel: GstWebRTC.WebRTCDataChannel, message: GLib.Bytes, grpc_client: GRPCClient
    ) -> None:
        global last_freq_counter
        global last_freq_update
        global last_drop_counter
        global freq_rates
        global drop_rates
        global init

        commands = AnyCommands()
        commands.ParseFromString(message.get_data())

        if not commands.commands:
            self.logger.info("No command or incorrect message received {message}")
            return

        # take lock. To removed gstreamer callback is not re-entrant
        if self.smart_lock.acquire(blocking=False):
            last_freq_counter += 1

            # ignore the return value to reach high frequencies > 1000Hz), and comment the try catch
            # if we need to do this a better way would be to have a buffer and manually drop late messages
            future = asyncio.run_coroutine_threadsafe(grpc_client.handle_commands(commands), self.producer._asyncloop)

            try:
                _ = future.result(timeout=1)
            except TimeoutError:
                self.logger.warning("The coroutine took too long, cancelling the task...")
                future.cancel()
            except Exception as exc:
                self.logger.error(f"The coroutine raised an exception: {exc!r}")

            now = time.time()
            if now - last_freq_update > 1:
                current_freq_rate = int(last_freq_counter / (now - last_freq_update))
                current_drop_rate = int(last_drop_counter / (now - last_freq_update))

                self.logger.info(f"Freq {current_freq_rate} Hz\tDrop {current_drop_rate} Hz")

                if init:
                    freq_rates.append(current_freq_rate)
                    drop_rates.append(current_drop_rate)
                    if len(freq_rates) > 10000:
                        freq_rates.pop(0)
                        drop_rates.pop(0)
                    mean_freq_rate = sum(freq_rates) / len(freq_rates)
                    mean_drop_rate = sum(drop_rates) / len(drop_rates)
                    self.logger.info(f"[MEAN] Freq {mean_freq_rate} Hz\tDrop {mean_drop_rate} Hz")
                else:
                    init = True
                # Calculate mean values

                last_freq_counter = 0
                last_drop_counter = 0
                last_freq_update = now

            self.smart_lock.release()
        else:
            self.logger.info("Nevermind, I'll send the next one")
            last_drop_counter += 1

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
        channel_options.set_value("max-packet-lifetime", 20)  # Unity sending frequency minimum 50Hz in fixed updated
        channel_options.set_value("priority", GstWebRTC.WebRTCPriorityType.HIGH)
        reachy_command_datachannel = pc.emit("create-data-channel", f"reachy_command_{request.reachy_id.id}", channel_options)
        if reachy_command_datachannel:
            reachy_command_datachannel.connect("on-message-data", self.on_reachy_command_datachannel_message, grpc_client)
        else:
            self.logger.error("Failed to create data channel command")

        return ServiceResponse()

    async def handle_disconnect_request(self) -> ServiceResponse:
        # TODO: implement me
        self.logger.info("Disconnecting...")

        return ServiceResponse()


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
