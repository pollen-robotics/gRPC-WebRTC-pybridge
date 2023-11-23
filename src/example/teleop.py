from aiortc import RTCDataChannel
import argparse
import asyncio
from gst_signalling import GstSession, GstSignallingConsumer
from gst_signalling.utils import find_producer_peer_id_by_name
import logging
import sys


from reachy2_sdk_api.reachy_pb2 import ReachyState
from reachy2_sdk_api.webrtc_bridge_pb2 import (
    AnyCommand,
    AnyCommands,
    ArmCommand,
    Connect,
    Disconnect,
    GetReachy,
    ServiceRequest,
    ServiceResponse,
)


class TeleopApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.logger = logging.getLogger(__name__)

        producer_peer_id = find_producer_peer_id_by_name(
            args.webrtc_signaling_host,
            args.webrtc_signaling_port,
            args.webrtc_producer_name,
        )

        self.signaling = GstSignallingConsumer(
            host=args.webrtc_signaling_host,
            port=args.webrtc_signaling_port,
            producer_peer_id=producer_peer_id,
        )

        self.connected = asyncio.Event()

        @self.signaling.on("new_session")  # type: ignore[misc]
        def on_new_session(session: GstSession) -> None:
            self.logger.info(f"New session: {session}")

            pc = session.pc

            @pc.on("datachannel")  # type: ignore[misc]
            async def on_datachannel(channel: RTCDataChannel) -> None:
                self.logger.info(f"Joined new data channel: {channel.label}")
                # TODO: tmp
                print(f"Joined new data channel: {channel.label}")

                if channel.label.startswith("reachy_state"):

                    @channel.on("message")  # type: ignore[misc]
                    def on_message(message: bytes) -> None:
                        reachy_state = ReachyState()
                        reachy_state.ParseFromString(message)
                        self.reachy_state = reachy_state

                if channel.label.startswith("reachy_command"):

                    async def send_command() -> None:
                        while True:
                            commands = AnyCommands(
                                commands=[
                                    AnyCommand(
                                        arm_command=ArmCommand(
                                            turn_on=self.connection.reachy.l_arm.part_id,
                                        ),
                                    ),
                                    AnyCommand(
                                        arm_command=ArmCommand(
                                            turn_on=self.connection.reachy.r_arm.part_id,
                                        ),
                                    ),
                                ],
                            )
                            channel.send(commands.SerializeToString())
                            await asyncio.sleep(1)

                    asyncio.ensure_future(send_command())

                if channel.label == "service":

                    @channel.on("message")  # type: ignore[misc]
                    def on_message(message: bytes) -> None:
                        response = ServiceResponse()
                        response.ParseFromString(message)

                        if response.HasField("connection_status"):
                            self.connection = response.connection_status
                            self.connected.set()

                        if response.HasField("error"):
                            print(f"Received error message: {response.error}")

                    # Ask for Reachy description (id, present parts, etc.)
                    req = ServiceRequest(
                        get_reachy=GetReachy(),
                    )
                    channel.send(req.SerializeToString())
                    await self.connected.wait()
                    self.logger.info(f"Got reachy: {self.connection.reachy}")

                    # Then, Request for state stream update and start sending commands
                    req = ServiceRequest(
                        connect=Connect(
                            reachy_id=self.connection.reachy.id,
                            update_frequency=100,
                        )
                    )
                    channel.send(req.SerializeToString())

    async def run_consumer(self) -> None:
        await self.signaling.connect()
        await self.signaling.consume()

    async def close(self) -> None:
        await self.signaling.close()


def main(args: argparse.Namespace) -> int:  # noqa: C901
    teleop = TeleopApp(args)

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
