import logging
from typing import AsyncGenerator
import grpc

from google.protobuf.empty_pb2 import Empty
from reachy2_sdk_api import (
    arm_pb2,
    arm_pb2_grpc,
    hand_pb2,
    hand_pb2_grpc,
    head_pb2,
    head_pb2_grpc,
    reachy_pb2,
    reachy_pb2_grpc,
    webrtc_bridge_pb2,
)


class GRPCClient:
    def __init__(
        self,
        host: str,
        port: int,
    ) -> None:
        self.logger = logging.getLogger(__name__)

        self.host = host
        self.port = port

        # Prepare channel for states/commands
        self.async_channel = grpc.aio.insecure_channel(f"{host}:{port}")

        self.reachy_stub = reachy_pb2_grpc.ReachyServiceStub(self.async_channel)
        self.arm_stub = arm_pb2_grpc.ArmServiceStub(self.async_channel)
        self.hand_stub = hand_pb2_grpc.HandServiceStub(self.async_channel)
        self.head_stub = head_pb2_grpc.HeadServiceStub(self.async_channel)

    # Got Reachy(s) description
    async def get_reachy(self) -> reachy_pb2.Reachy:
        return await self.reachy_stub.GetReachy(Empty())

    # Retrieve Reachy entire state
    async def get_reachy_state(
        self,
        reachy_id: reachy_pb2.ReachyId,
        publish_frequency: float = 100,
    ) -> AsyncGenerator[reachy_pb2.ReachyState, None]:
        stream_req = reachy_pb2.ReachyStreamStateRequest(
            id=reachy_id,
            publish_frequency=publish_frequency,
        )

        async for state in self.reachy_stub.StreamReachyState(stream_req):
            yield state

    # Send Commands (torque and cartesian targets)
    async def handle_commands(self, commands: webrtc_bridge_pb2.AnyCommands) -> None:
        print(f"Received commands: {commands}")
        pass

    async def set_arm_torque(self, on: bool) -> None:
        pass

    # async def send_command(self, message: bytes) -> None:
    #     cmd = any_joint_command_pb2.AnyJointsCommand()
    #     cmd.ParseFromString(message)

    #     if cmd.HasField("joints"):
    #         joints_command = cmd.joints
    #         await self.joint_stub.SendJointsCommands(joints_command)
