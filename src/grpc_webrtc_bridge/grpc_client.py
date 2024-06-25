import logging

import grpc
from google.protobuf.empty_pb2 import Empty
from reachy2_sdk_api import (
    arm_pb2_grpc,
    hand_pb2_grpc,
    head_pb2_grpc,
    mobile_base_mobility_pb2_grpc,
    mobile_base_utility_pb2_grpc,
    reachy_pb2,
    reachy_pb2_grpc,
    webrtc_bridge_pb2,
)

# sum_hand = pc.Summary('grpcwebrtc_client_hand_commands', 'Time spent during hand commands')
# sum_arm =  pc.Summary('grpcwebrtc_client_arm_commands', 'Time spent during arm commands')
# sum_neck = pc.Summary('grpcwebrtc_client_neck_commands', 'Time spent during neck commands')
# sum_base = pc.Summary('grpcwebrtc_client_base_commands', 'Time spent during base commands')


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
        self.synchro_channel = grpc.insecure_channel(f"{host}:{port}")
        # self.async_channel = grpc.aio.insecure_channel(f"{host}:{port}")

        self.reachy_stub_synchro = reachy_pb2_grpc.ReachyServiceStub(self.synchro_channel)
        # self.reachy_stub_async =
        self.arm_stub = arm_pb2_grpc.ArmServiceStub(self.synchro_channel)
        self.hand_stub = hand_pb2_grpc.HandServiceStub(self.synchro_channel)
        self.head_stub = head_pb2_grpc.HeadServiceStub(self.synchro_channel)
        self.mb_utility_stub = mobile_base_utility_pb2_grpc.MobileBaseUtilityServiceStub(self.synchro_channel)
        self.mb_mobility_stub = mobile_base_mobility_pb2_grpc.MobileBaseMobilityServiceStub(self.synchro_channel)

    # Got Reachy(s) description
    def get_reachy(self) -> reachy_pb2.Reachy:
        return self.reachy_stub_synchro.GetReachy(Empty())

    # # # # # # Retrieve Reachy entire state
    # # # # # async def get_reachy_state(
    # # # # #     self,
    # # # # #     reachy_id: reachy_pb2.ReachyId,
    # # # # #     publish_frequency: float,
    # # # # # ) -> AsyncGenerator[reachy_pb2.ReachyState, None]:
    # # # # #     stream_req = reachy_pb2.ReachyStreamStateRequest(
    # # # # #         id=reachy_id,
    # # # # #         publish_frequency=publish_frequency,
    # # # # #     )   
    # # # # #   async for state in self.reachy_stub_async.StreamReachyState(stream_req):
    # # # # #         yield state
    
    # # # # # async def audit(
    # # # # #     self,
    # # # # #     reachy_id: reachy_pb2.ReachyId,
    # # # # #     publish_frequency: float,
    # # # # # ) -> AsyncGenerator[reachy_pb2.ReachyStatus, None]:
    # # # # #     stream_req = reachy_pb2.ReachyStreamAuditRequest(
    # # # # #         id=reachy_id,
    # # # # #         publish_frequency=publish_frequency,
    # # # # #     )

    # # # # #     async for status in self.reachy_stub.StreamAudit(stream_req):
    # # # # #         yield status


    # Send Commands (torque and cartesian targets)
    def handle_commands(
        self,
        commands: webrtc_bridge_pb2.AnyCommands,
    ) -> None:
        # self.logger.info(f"Received message: {commands}")

        # TODO: Could this be done in parallel?
        for cmd in commands.commands:
            if cmd.HasField("arm_command"):
                self.handle_arm_command(cmd.arm_command)
            if cmd.HasField("hand_command"):
                self.handle_hand_command(cmd.hand_command)
            if cmd.HasField("neck_command"):
                self.handle_neck_command(cmd.neck_command)
            if cmd.HasField("mobile_base_command"):
                self.handle_mobile_base_command(cmd.mobile_base_command)

    def handle_arm_command(self, cmd: webrtc_bridge_pb2.ArmCommand) -> None:
        # TODO: Could this be done in parallel?
        if cmd.HasField("arm_cartesian_goal"):
            self.arm_stub.SendArmCartesianGoal(cmd.arm_cartesian_goal)
        if cmd.HasField("turn_on"):
            self.arm_stub.TurnOn(cmd.turn_on)
        if cmd.HasField("turn_off"):
            self.arm_stub.TurnOff(cmd.turn_off)
        if cmd.HasField("speed_limit"):
            self.arm_stub.SetSpeedLimit(cmd.speed_limit)
        if cmd.HasField("torque_limit"):
            self.arm_stub.SetTorqueLimit(cmd.torque_limit)

    def handle_hand_command(self, cmd: webrtc_bridge_pb2.HandCommand) -> None:
        # TODO: Could this be done in parallel?
        if cmd.HasField("hand_goal"):
            self.hand_stub.SetHandPosition(cmd.hand_goal)
        if cmd.HasField("turn_on"):
            self.hand_stub.TurnOn(cmd.turn_on)
        if cmd.HasField("turn_off"):
            self.hand_stub.TurnOff(cmd.turn_off)

    def handle_neck_command(self, cmd: webrtc_bridge_pb2.NeckCommand) -> None:
        # TODO: Could this be done in parallel?
        if cmd.HasField("neck_goal"):
            self.head_stub.SendNeckJointGoal(cmd.neck_goal)
        if cmd.HasField("turn_on"):
            self.head_stub.TurnOn(cmd.turn_on)
        if cmd.HasField("turn_off"):
            self.head_stub.TurnOff(cmd.turn_off)
        if cmd.HasField("speed_limit"):
            self.head_stub.SetSpeedLimit(cmd.speed_limit)
        if cmd.HasField("torque_limit"):
            self.head_stub.SetTorqueLimit(cmd.torque_limit)

    def handle_mobile_base_command(self, cmd: webrtc_bridge_pb2.MobileBaseCommand) -> None:
        # TODO: Could this be done in parallel?
        if cmd.HasField("target_direction"):
            self.mb_mobility_stub.SendDirection(cmd.target_direction)
        if cmd.HasField("mobile_base_mode"):
            self.mb_utility_stub.SetZuuuMode(cmd.mobile_base_mode)
