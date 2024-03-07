import argparse
import asyncio
import logging
import sys
import time
from threading import Semaphore

import aiortc
from gst_signalling import GstSession, GstSignallingProducer
from reachy2_sdk_api.webrtc_bridge_pb2 import (
    AnyCommands,
    Connect,
    ConnectionStatus,
    ServiceRequest,
    ServiceResponse,
)

from .grpc_client import GRPCClient

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
        # self.smart_lock = Lock()
        self.smart_lock = Semaphore(10)

        @self.producer.on("new_session")  # type: ignore[misc]
        def on_new_session(session: GstSession) -> None:
            pc = session.pc

            grpc_client = GRPCClient(args.grpc_host, args.grpc_port)

            service_channel = pc.createDataChannel("service")

            @service_channel.on("message")  # type: ignore[misc]
            async def service_channel_message(message: bytes) -> None:
                request = ServiceRequest()
                request.ParseFromString(message)

                response = await self.handle_service_request(request, grpc_client, pc)

                service_channel.send(response.SerializeToString())

    async def serve4ever(self) -> None:
        await self.producer.serve4ever()

    async def close(self) -> None:
        await self.producer.close()

    # Handle service request
    async def handle_service_request(
        self,
        request: ServiceRequest,
        grpc_client: GRPCClient,
        pc: aiortc.RTCPeerConnection,
    ) -> ServiceResponse:
        self.logger.info(f"Received service request message: {request}")

        if request.HasField("get_reachy"):
            resp = await self.handle_get_reachy_request(grpc_client)

        elif request.HasField("connect"):
            resp = await self.handle_connect_request(request.connect, grpc_client, pc)

        elif request.HasField("disconnect"):
            resp = await self.handle_disconnect_request()

        self.logger.info(f"Sending service response message: {resp}")
        return resp

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

    async def handle_connect_request(  # noqa: C901
        self,
        request: Connect,
        grpc_client: GRPCClient,
        pc: aiortc.RTCPeerConnection,
    ) -> ServiceResponse:
        # Create state data channel and start sending state
        if request.update_frequency >= 1000:
            max_packet_lifetime = 1
        else:
            max_packet_lifetime = (int)(1000 // request.update_frequency)

        reachy_state_datachannel = pc.createDataChannel(
            f"reachy_state_{request.reachy_id.id}",
            maxPacketLifeTime=max_packet_lifetime,
        )

        @reachy_state_datachannel.on("open")  # type: ignore[misc]
        def on_reachy_state_datachannel_open() -> None:
            async def send_joint_state() -> None:
                try:
                    async for state in grpc_client.get_reachy_state(
                        request.reachy_id,
                        request.update_frequency,
                    ):
                        reachy_state_datachannel.send(state.SerializeToString())
                except aiortc.exceptions.InvalidStateError:
                    logging.info("Data channel closed.")

            asyncio.ensure_future(send_joint_state())

        # Create command data channel and start handling commands
        # assuming data sent by Unity in the FixedUpdate loop. frequency 50Hz / ev 20ms
        reachy_command_datachannel = pc.createDataChannel(f"reachy_command_{request.reachy_id.id}", maxPacketLifeTime=20)

        @reachy_command_datachannel.on("message")  # type: ignore[misc]
        async def on_reachy_command_datachannel_message(
            message: bytes,
        ) -> None:
            global last_freq_counter
            global last_freq_update
            global last_drop_counter
            global freq_rates
            global drop_rates
            global init

            commands = AnyCommands()
            commands.ParseFromString(message)

            if not commands.commands:
                self.logger.info("No command or incorrect message received {message}")
                return

            # TODO better, temporary message priority
            important_msg = False
            for cmd in commands.commands:
                if cmd.HasField("arm_command"):
                    if cmd.arm_command.HasField("turn_on") or cmd.arm_command.HasField("turn_off"):
                        important_msg = True
                        important_log = f"Arm command: turn_on {cmd.arm_command.HasField('turn_on')} \
                                          turn_off {cmd.arm_command.HasField('turn_off')}"
                elif cmd.HasField("hand_command"):
                    if cmd.hand_command.HasField("turn_on") or cmd.hand_command.HasField("turn_off"):
                        important_msg = True
                        important_log = f"Hand command: turn_on {cmd.hand_command.HasField('turn_on')} \
                                          turn_off {cmd.hand_command.HasField('turn_off')}"
                elif cmd.HasField("neck_command"):
                    if cmd.neck_command.HasField("turn_on") or cmd.neck_command.HasField("turn_off"):
                        important_msg = True
                        important_log = f"Neck command: turn_on {cmd.neck_command.HasField('turn_on')} \
                                          turn_off {cmd.neck_command.HasField('turn_off')}"
                elif cmd.HasField("mobile_base_command"):
                    if cmd.mobile_base_command.HasField("mobile_base_mode"):
                        important_msg = True
                        important_log = f"Mobile base command: mobile_base_mode {str(cmd.mobile_base_command.mobile_base_mode)}"

            # take lock
            if important_msg or self.smart_lock.acquire(blocking=False):
                last_freq_counter += 1
                await grpc_client.handle_commands(commands)

                if important_msg:
                    self.logger.info(f"Some important command has been allowed\n{important_log}")

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
                # self.logger.info("Nevermind, I'll send the next one")
                last_drop_counter += 1

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
