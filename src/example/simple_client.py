import aioconsole
from aiortc import RTCIceCandidate, RTCPeerConnection, RTCSessionDescription
import argparse
import asyncio
from google.protobuf.wrappers_pb2 import FloatValue
from grpc_webrtc_bridge.grpc_client import GRPCClient
from gst_signalling.aiortc_adapter import BYE, GstSignalingForAiortc
import logging
from reachy_sdk_api import any_joint_command_pb2, joint_pb2
import sys


async def main(args: argparse.Namespace) -> int:
    logger = logging.getLogger(__name__)

    signaling = GstSignalingForAiortc(
        signaling_host=args.webrtc_signaling_host,
        signaling_port=args.webrtc_signaling_port,
        role="consumer",
        name="grpc_webrtc_bridge",
        remote_producer_peer_id=args.webrtc_producer_peer_id,
    )
    await signaling.connect()

    pc = RTCPeerConnection()

    joint_command_datachannel = pc.createDataChannel("joint_command")

    @joint_command_datachannel.on("open")
    def on_joint_command_datachannel_open():
        async def send_joint_command():
            while True:
                pos = await aioconsole.ainput("Enter a position: ")
                if pos == "q":
                    break
                joint_command = joint_pb2.JointCommand(
                    id=joint_pb2.JointId(name="r_elbow_pitch"),
                    goal_position=FloatValue(value=float(pos)),
                )
                cmd = any_joint_command_pb2.AnyJointsCommand(
                    joints=joint_pb2.JointsCommand(
                        commands=[joint_command],
                    ),
                )
                joint_command_datachannel.send(cmd.SerializeToString())

        asyncio.ensure_future(send_joint_command())

    @pc.on("datachannel")
    def on_datachannel(channel):
        logger.info(f"New data channel: {channel.label}")

        if channel.label == "joint_state":

            @channel.on("message")
            def on_message(message):
                joint_state = joint_pb2.JointsState()
                joint_state.ParseFromString(message)
                logger.debug(f"Received message: {joint_state}")

    while True:
        obj = await signaling.receive()
        if isinstance(obj, RTCSessionDescription):
            await pc.setRemoteDescription(obj)

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
        "--webrtc-producer-peer-id",
        type=str,
        required=True,
        help="Peer id of the producer.",
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
