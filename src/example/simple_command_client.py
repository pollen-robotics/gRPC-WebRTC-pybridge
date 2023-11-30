import argparse
import asyncio
import logging
import sys

import aioconsole
import numpy as np
from google.protobuf.wrappers_pb2 import FloatValue
from gst_signalling import GstSession, GstSignallingConsumer
from gst_signalling.utils import find_producer_peer_id_by_name
from reachy_sdk_api import any_joint_command_pb2, joint_pb2


def main(args: argparse.Namespace) -> int:  # noqa: C901
    logger = logging.getLogger(__name__)

    producer_peer_id = find_producer_peer_id_by_name(
        args.webrtc_signaling_host,
        args.webrtc_signaling_port,
        args.webrtc_producer_name,
    )

    signaling = GstSignallingConsumer(
        host=args.webrtc_signaling_host,
        port=args.webrtc_signaling_port,
        producer_peer_id=producer_peer_id,
    )

    @signaling.on("new_session")  # type: ignore[misc]
    def on_new_session(session: GstSession) -> None:
        logger.info(f"New session: {session}")

        pc = session.pc

        # Joint Command channel
        joint_command_datachannel = pc.createDataChannel("joint_command")

        @joint_command_datachannel.on("open")  # type: ignore[misc]
        def on_joint_command_datachannel_open() -> None:
            async def send_joint_command() -> None:
                while True:
                    pos = await aioconsole.ainput("Enter a position: ")
                    if pos == "q":
                        break

                    pos = np.deg2rad(float(pos))

                    joint_command = joint_pb2.JointCommand(
                        id=joint_pb2.JointId(name="r_elbow_pitch"),
                        goal_position=FloatValue(value=pos),
                    )
                    cmd = any_joint_command_pb2.AnyJointsCommand(
                        joints=joint_pb2.JointsCommand(
                            commands=[joint_command],
                        ),
                    )
                    joint_command_datachannel.send(cmd.SerializeToString())

            asyncio.ensure_future(send_joint_command())

    async def run_consumer(consumer: GstSignallingConsumer) -> None:
        await signaling.connect()
        await signaling.consume()

    # run event loop
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(run_consumer(signaling))
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(signaling.close())

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
