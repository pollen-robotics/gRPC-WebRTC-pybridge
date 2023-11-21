#
# Register to ReachyState channel
# Send Commands to apropiate channels

from aiortc import RTCDataChannel
import argparse
import asyncio
from gst_signalling import GstSession, GstSignallingConsumer
from gst_signalling.utils import find_producer_peer_id_by_name
import logging
import sys


class TeleopApp:
    def __init__(
        self,
        webrtc_signaling_host: str,
        webrtc_signaling_port: int,
        webrtc_producer_name: str,
        producer_peer_id: str,
    ) -> None:
        self.logger = logging.getLogger(__name__)

        self.producer_peer_id = find_producer_peer_id_by_name(
            webrtc_signaling_host,
            webrtc_signaling_port,
            webrtc_producer_name,
        )

        self.signaling = GstSignallingConsumer(
            host=webrtc_signaling_host,
            port=webrtc_signaling_port,
            producer_peer_id=producer_peer_id,
        )

        self.got_reachy = asyncio.Event()

        @signaling.on("new_session")  # type: ignore[misc]
        def on_new_session(session: GstSession) -> None:
            self.logger.info(f"New session: {session}")

            pc = session.pc

            @pc.on("datachannel")  # type: ignore[misc]
            def on_datachannel(channel: RTCDataChannel) -> None:
                self.logger.info(f"Joined new data channel: {channel.label}")

                if channel.label == "service":

                    @channel.on("message")  # type: ignore[misc]
                    def on_message(message: bytes) -> None:
                        response = ServiceResponse()
                        response.ParseFromString(message)

                        if response.HasField("reachy"):
                            print(f"Received message: {response.reachy}")
                            self.reachy = response.reachy
                            self.got_reachy.set()

                        if response.HasField("error"):
                            print(f"Received error message: {response.error}")

                    # Ask for Reachy description (id, present parts, etc.)
                    channel.send(ServiceRequest(GetReachy()))
                    self.got_reachy.wait()

                    # Request for state stream update
                    channel.send(
                        ServiceRequest(
                            StreamState(id=self.reachy.id, publish_frequency=100)
                        )
                    )

                if channel.label == "reachy_state":

                    @channel.on("message")  # type: ignore[misc]
                    def on_message(message: bytes) -> None:
                        reachy_state = reachy_pb2.ReachyState()
                        reachy_state.ParseFromString(message)
                        self.reachy_state = reachy_state

    async def run_consumer(self) -> None:
        await self.signaling.connect()
        await self.signaling.consume()

    async def close(self) -> None:
        await self.signaling.close()


def main(args: argparse.Namespace) -> int:  # noqa: C901
    teleop = TeleopApp(**args)

    # run event loop
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(teleop.run_consumer())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(teleop.close())

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

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
    parser.add_argument(
        "--webrtc-producer-name",
        type=str,
        default="grpc_webrtc_bridge",
        help="Name of the producer.",
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
