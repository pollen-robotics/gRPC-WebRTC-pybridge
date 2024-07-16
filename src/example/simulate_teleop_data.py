import argparse
import asyncio
import logging
import sys
import time

import gi
import numpy as np
from google.protobuf.wrappers_pb2 import FloatValue
from gst_signalling import GstSignallingConsumer
from gst_signalling.gst_abstract_role import GstSession
from gst_signalling.utils import find_producer_peer_id_by_name
from reachy2_sdk_api.arm_pb2 import ArmCartesianGoal
from reachy2_sdk_api.kinematics_pb2 import Matrix4x4
from reachy2_sdk_api.reachy_pb2 import ReachyId, ReachyState, ReachyStatus
from reachy2_sdk_api.webrtc_bridge_pb2 import (
    AnyCommand,
    AnyCommands,
    ArmCommand,
    Connect,
    GetReachy,
    ServiceRequest,
    ServiceResponse,
)

gi.require_version("Gst", "1.0")

from gi.repository import GLib, Gst, GstWebRTC  # noqa : E402


class TeleopApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self._logger = logging.getLogger(__name__)

        producer_peer_id = find_producer_peer_id_by_name(
            args.webrtc_signalling_host,
            args.webrtc_signalling_port,
            args.webrtc_producer_name,
        )

        self.consumer = GstSignallingConsumer(
            host=args.webrtc_signalling_host,
            port=args.webrtc_signalling_port,
            producer_peer_id=producer_peer_id,
        )

        @self.consumer.on("new_session")  # type: ignore[misc]
        def on_new_session(session: GstSession) -> None:
            self._logger.info(f"New session: {session}")

            webrtcbin = session.pc
            webrtcbin.connect("on-data-channel", self._on_data_channel)

    async def run_consumer(self) -> None:
        await self.consumer.connect()
        await self.consumer.consume()

    async def close(self) -> None:
        await self.consumer.close()

    def _on_data_channel(self, webrtcbin: Gst.Element, channel: GstWebRTC.WebRTCDataChannel) -> None:
        label = channel.get_property("label")
        self._logger.info(f"received data channel {label}")

        if label.startswith("reachy_state"):
            channel.connect("on-message-data", self._handle_state_channel)

        elif label.startswith("reachy_audit"):
            channel.connect("on-message-data", self._handle_audit_channel)

        elif label.startswith("reachy_command"):
            self.ensure_send_command(channel)

        elif label == "service":
            self.setup_connection(channel)

    def _on_data_service_channel(self, channel: GstWebRTC.WebRTCDataChannel, message: GLib.Bytes) -> None:
        resp = ServiceResponse()
        resp.ParseFromString(message.get_data())
        self._logger.debug(f"Message from service channel: {resp}")

        if resp.HasField("connection_status"):
            self.connection = resp.connection_status

        if resp.HasField("error"):
            self._logger.error(f"Received error message: {resp.error}")

        # Ask for opening of state and audit channels
        req = ServiceRequest(
            connect=Connect(reachy_id=ReachyId(id=resp.connection_status.reachy.id.id), update_frequency=60, audit_frequency=1)
        )
        byte_data = req.SerializeToString()
        gbyte_data = GLib.Bytes.new(byte_data)
        channel.send_data(gbyte_data)

    def setup_connection(self, channel: GstWebRTC.WebRTCDataChannel) -> None:
        channel.connect("on-message-data", self._on_data_service_channel)
        # Ask for Reachy description (id, present parts, etc.)
        req = ServiceRequest(
            get_reachy=GetReachy(),
        )
        byte_data = req.SerializeToString()
        gbyte_data = GLib.Bytes.new(byte_data)
        channel.send_data(gbyte_data)

    def _handle_state_channel(self, channel: GstWebRTC.WebRTCDataChannel, message: GLib.Bytes) -> None:
        reachy_state = ReachyState()
        reachy_state.ParseFromString(message.get_data())
        self._logger.debug(reachy_state)

    def _handle_audit_channel(self, channel: GstWebRTC.WebRTCDataChannel, message: GLib.Bytes) -> None:
        reachy_status = ReachyStatus()
        reachy_status.ParseFromString(message.get_data())
        self._logger.debug(reachy_status)

    def get_arm_cartesian_goal(self, x: float, y: float, z: float, partid: int = 1) -> ArmCartesianGoal:
        goal = np.array(
            [
                [0, 0, 1, x],
                [0, 1, 0, y],
                [1, 0, 0, z],
                [0, 0, 0, 1],
            ]
        )
        return ArmCartesianGoal(
            id={"id": partid, "name": "r_arm"},
            goal_pose=Matrix4x4(data=goal.flatten().tolist()),
            duration=FloatValue(value=1.0),
        )

    def make_arm_cartesian_goal(self, x: float, y: float, z: float, partid: int = 1) -> ArmCartesianGoal:
        goal = np.array(
            [
                [0, 0, 1, x],
                [0, 1, 0, y],
                [1, 0, 0, z],
                [0, 0, 0, 1],
            ]
        )
        return ArmCartesianGoal(
            # id={"id": partid, "name": "r_arm" if partid==1 else "l_arm"},
            id=partid,
            goal_pose=Matrix4x4(data=goal.flatten().tolist()),
            duration=FloatValue(value=1.0),
        )

    def ensure_send_command(self, channel: GstWebRTC.WebRTCDataChannel, freq: float = 100) -> None:
        async def send_command() -> None:
            radius = 0.2  # Circle radius
            fixed_x = 0.4  # Fixed x-coordinate
            center_y, center_z = 0, 0.1  # Center of the circle in y-z plane
            num_steps = 200  # Number of steps to complete the circle
            frequency = 100  # Update frequency in Hz
            step = 0  # Current step
            circle_period = 3
            t0 = time.time()
            while True:
                angle = 2 * np.pi * (step / num_steps)
                angle = 2 * np.pi * (time.time() - t0) / circle_period
                self._logger.debug(f"command angle {angle}")
                step += 1
                if step >= num_steps:
                    step = 0
                # Calculate y and z coordinates
                y = center_y + radius * np.cos(angle)
                z = center_z + radius * np.sin(angle)

                commands = AnyCommands(
                    commands=[
                        AnyCommand(  # right arm
                            arm_command=ArmCommand(
                                arm_cartesian_goal=self.make_arm_cartesian_goal(
                                    fixed_x, y - 0.2, z, partid=self.connection.reachy.r_arm.part_id
                                )
                            ),
                        ),
                        AnyCommand(  # left arm
                            arm_command=ArmCommand(
                                arm_cartesian_goal=self.make_arm_cartesian_goal(
                                    fixed_x, y + 0.2, z, partid=self.connection.reachy.l_arm.part_id
                                )
                            ),
                        ),
                    ],
                )

                byte_data = commands.SerializeToString()
                gbyte_data = GLib.Bytes.new(byte_data)
                channel.send_data(gbyte_data)
                # self._logger.debug(f"send command : {byte_data}")

                await asyncio.sleep(1 / frequency)

        asyncio.run_coroutine_threadsafe(send_command(), self.consumer._asyncloop)


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
        "--webrtc-signalling-host",
        type=str,
        default="127.0.0.1",
        help="Host of the gstreamer webrtc signalling server.",
    )
    parser.add_argument(
        "--webrtc-signalling-port",
        type=int,
        default=8443,
        help="Port of the gstreamer webrtc signalling server.",
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
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    sys.exit(main(args))
