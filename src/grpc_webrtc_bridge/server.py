import aiortc
import argparse
import asyncio
from gst_signalling import GstSession, GstSignallingProducer
import logging
import sys


from .grpc_client import GRPCClient


def main(args: argparse.Namespace) -> int:  # noqa: C901
    logger = logging.getLogger(__name__)

    producer = GstSignallingProducer(
        host=args.webrtc_signaling_host,
        port=args.webrtc_signaling_port,
        name="grpc_webrtc_bridge",
    )

    @producer.on("new_session")  # type: ignore[misc]
    def on_new_session(session: GstSession) -> None:
        logger.info(f"New session: {session}")
        pc = session.pc

        grpc_client = GRPCClient(args.grpc_host, args.grpc_port)

        reachy_state_datachannel = pc.createDataChannel("reachy_state")

        @reachy_state_datachannel.on("open")  # type: ignore[misc]
        def on_jreachy_state_datachannel_open() -> None:
            async def send_joint_state() -> None:
                try:
                    async for state in grpc_client.get_reachy_state():
                        reachy_state_datachannel.send(state.SerializeToString())
                except aiortc.exceptions.InvalidStateError:
                    logging.info("Data channel closed.")

            asyncio.ensure_future(send_joint_state())

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(producer.serve4ever())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(producer.close())

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

    sys.exit(main(args))
