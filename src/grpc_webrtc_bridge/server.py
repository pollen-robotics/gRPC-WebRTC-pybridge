from aiortc import (
    RTCIceCandidate,
    RTCDataChannel,
    RTCPeerConnection,
    RTCSessionDescription,
)
import argparse
import asyncio
from gst_signalling.aiortc_adapter import BYE, GstSignalingForAiortc
import sys
import logging


from .grpc_client import GRPCClient


async def main(args: argparse.Namespace) -> int:  # noqa: C901
    grpc_client = GRPCClient(args.grpc_host, args.grpc_port)

    signaling = GstSignalingForAiortc(
        signaling_host=args.webrtc_signaling_host,
        signaling_port=args.webrtc_signaling_port,
        role="producer",
        name="grpc_webrtc_bridge",
    )
    await signaling.connect()

    pc = RTCPeerConnection()
    joint_state_datachannel = pc.createDataChannel("joint_state")

    @joint_state_datachannel.on("open")  # type: ignore[misc]
    def on_joint_state_datachannel_open() -> None:
        async def send_joint_state() -> None:
            async for state in grpc_client.get_state():
                joint_state_datachannel.send(state.SerializeToString())

        asyncio.ensure_future(send_joint_state())

    @pc.on("datachannel")  # type: ignore[misc]
    def on_datachannel(channel: RTCDataChannel) -> None:
        logging.info(f"New data channel: {channel.label}")

        if channel.label == "joint_command":

            @channel.on("message")  # type: ignore[misc]
            async def on_message(message: bytes) -> None:
                logging.info(f"Received message: {message!r}")
                await grpc_client.send_command(message)

    await pc.setLocalDescription(await pc.createOffer())
    await signaling.send(pc.localDescription)

    while True:
        obj = await signaling.receive()
        if isinstance(obj, RTCSessionDescription):
            await pc.setRemoteDescription(obj)

            # Negotiation needed
            if obj.type == "offer":
                await pc.setLocalDescription(await pc.createAnswer())
                await signaling.send(pc.localDescription)

        elif isinstance(obj, RTCIceCandidate):
            pc.addIceCandidate(obj)

        elif obj is BYE:
            break

    return 0


if __name__ == "__main__":
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
        default=50055,
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

    sys.exit(asyncio.run(main(args)))
