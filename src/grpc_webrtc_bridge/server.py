import argparse
import asyncio
import logging
import sys

import aiortc
from gst_signalling import GstSession, GstSignallingProducer
from reachy2_sdk_api.webrtc_bridge_pb2 import (
    AnyCommands,
    ConnectionStatus,
    ServiceRequest,
    ServiceResponse,
)

from .grpc_client import GRPCClient


class GRPCWebRTCBridge:
    def __init__(self, args: argparse.Namespace) -> None:
        self.logger = logging.getLogger(__name__)

        self.producer = GstSignallingProducer(
            host=args.webrtc_signaling_host,
            port=args.webrtc_signaling_port,
            name="grpc_webrtc_bridge",
        )

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
        self.logger.info(f"Received message: {request}")

        if request.HasField("get_reachy"):
            reachy = await grpc_client.get_reachy()

            resp = ServiceResponse(
                connection_status=ConnectionStatus(
                    connected=True,
                    state_channel=f"reachy_state_{reachy.id.id}",
                    command_channel=f"reachy_command_{reachy.id.id}",
                    reachy=reachy,
                ),
            )
            self.logger.info(f"Sending reachy message: {resp}")
            return resp

        if request.HasField("connect"):
            reachy_state_datachannel = pc.createDataChannel(
                f"reachy_state_{request.connect.reachy_id.id}"
            )

            @reachy_state_datachannel.on("open")  # type: ignore[misc]
            def on_reachy_state_datachannel_open() -> None:
                async def send_joint_state() -> None:
                    try:
                        async for state in grpc_client.get_reachy_state(
                            request.connect.reachy_id,
                            request.connect.update_frequency,
                        ):
                            reachy_state_datachannel.send(state.SerializeToString())
                    except aiortc.exceptions.InvalidStateError:
                        logging.info("Data channel closed.")

                asyncio.ensure_future(send_joint_state())

            reachy_command_datachannel = pc.createDataChannel(
                f"reachy_command_{request.connect.reachy_id.id}"
            )

            @reachy_command_datachannel.on("message")  # type: ignore[misc]
            async def on_reachy_command_datachannel_message(message: bytes) -> None:
                commands = AnyCommands()
                commands.ParseFromString(message)

                await grpc_client.handle_commands(commands)

            return ServiceResponse()

        if request.HasField("disconnect"):
            # TODO: Close data channels
            print(f"DISCONNECT")
            return ServiceResponse()

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
