import asyncio
import logging
from typing import Any, AsyncGenerator

import grpc
import reachy2_monitoring as rm
from google.protobuf.empty_pb2 import Empty
from google.protobuf.wrappers_pb2 import BoolValue
from reachy2_sdk_api import (
    arm_pb2_grpc,
    component_pb2,
    dynamixel_motor_pb2,
    dynamixel_motor_pb2_grpc,
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
        tracer: Any = None,
    ) -> None:
        self.logger = logging.getLogger(__name__)
        if tracer is None:
            tracer = rm.tracer(f"grpc-webrtc_bridge_{port}", grpc_type="client")
        self.tracer = tracer

        self.host = host
        self.port = port
        # Prepare channel for states/commands
        self.synchro_channel = grpc.insecure_channel(f"{host}:{port}")
        self.async_channel = grpc.aio.insecure_channel(f"{host}:{port}")

        # self.reachy_stub_synchro = reachy_pb2_grpc.ReachyServiceStub(self.synchro_channel)
        self.reachy_stub_async = reachy_pb2_grpc.ReachyServiceStub(self.async_channel)

        self.arm_stub = arm_pb2_grpc.ArmServiceStub(self.synchro_channel)
        self.hand_stub = hand_pb2_grpc.HandServiceStub(self.synchro_channel)
        self.head_stub = head_pb2_grpc.HeadServiceStub(self.synchro_channel)
        self.mb_utility_stub = mobile_base_utility_pb2_grpc.MobileBaseUtilityServiceStub(self.synchro_channel)
        self.mb_mobility_stub = mobile_base_mobility_pb2_grpc.MobileBaseMobilityServiceStub(self.synchro_channel)
        self.dxl_motor_stub = dynamixel_motor_pb2_grpc.DynamixelMotorServiceStub(self.synchro_channel)

        self._event_streams = asyncio.Event()

    def __del__(self) -> None:
        self.logger.debug("Deleting GRPC Client")

    async def close(self) -> None:
        self.logger.debug("Closing GRPC Client")

        self._event_streams.set()
        self.synchro_channel.close()
        await self.async_channel.close()

        self.logger.debug("GRPC Client closed")

    # Got Reachy(s) description
    async def get_reachy(self) -> reachy_pb2.Reachy:
        self.logger.info("Getting Reachy ...")
        try:
            # this is the first call to the grpc server. Core may be still booting up
            return await self.reachy_stub_async.GetReachy(Empty(), timeout=20, wait_for_ready=True)
        except grpc.RpcError as e:
            self.logger.error(f"Error while getting Reachy: {e}")

    # Retrieve Reachy entire state
    async def get_reachy_state(
        self,
        reachy_id: reachy_pb2.ReachyId,
        publish_frequency: float,
    ) -> AsyncGenerator[reachy_pb2.ReachyState, None]:
        stream_req_state = reachy_pb2.ReachyStreamStateRequest(
            id=reachy_id,
            publish_frequency=publish_frequency,
        )

        try:
            async for state in self.reachy_stub_async.StreamReachyState(stream_req_state):
                if self._event_streams.is_set():
                    self.logger.debug("Stream state interrupted")
                    break
                yield state
        except grpc.RpcError as e:
            self.logger.error(f"Error while streaming state: {e}")

    # Retrieve Reachy entire audit status
    async def get_reachy_audit_status(
        self,
        reachy_id: reachy_pb2.ReachyId,
        publish_frequency: float,
    ) -> AsyncGenerator[reachy_pb2.ReachyStatus, None]:
        stream_req_audit = reachy_pb2.ReachyStreamAuditRequest(
            id=reachy_id,
            publish_frequency=publish_frequency,
        )
        try:
            async for state in self.reachy_stub_async.StreamAudit(stream_req_audit):
                if self._event_streams.is_set():
                    self.logger.debug("Stream state audit interrupted")
                    break
                yield state
        except grpc.RpcError as e:
            self.logger.error(f"Error while streaming audit state: {e}")

    # Send Commands (torque and cartesian targets)
    def handle_commands(
        self,
        commands: webrtc_bridge_pb2.AnyCommands,
    ) -> None:
        # self.logger.info(f"Received message: {commands}")
        with rm.PollenSpan(tracer=self.tracer, trace_name="handle_commands"):
            for cmd in commands.commands:
                if cmd.HasField("arm_command"):
                    self.handle_arm_command(cmd.arm_command)
                elif cmd.HasField("hand_command"):
                    self.handle_hand_command(cmd.hand_command)
                elif cmd.HasField("neck_command"):
                    self.handle_neck_command(cmd.neck_command)
                elif cmd.HasField("mobile_base_command"):
                    self.handle_mobile_base_command(cmd.mobile_base_command)
                elif cmd.HasField("antennas_command"):
                    self.handle_antennas_command(cmd.antennas_command)
                else:
                    self.logger.warning(f"Unknown command : {cmd}")

    def handle_arm_command(self, cmd: webrtc_bridge_pb2.ArmCommand) -> None:
        with rm.PollenSpan(tracer=self.tracer, trace_name="handle_arm_command"):
            if cmd.HasField("arm_cartesian_goal"):
                with rm.PollenSpan(tracer=self.tracer, trace_name="SendArmCartesianGoal"):
                    self.arm_stub.SendArmCartesianGoal(cmd.arm_cartesian_goal, timeout=5)
            elif cmd.HasField("turn_on"):
                with rm.PollenSpan(tracer=self.tracer, trace_name="arm_turn_on"):
                    self.arm_stub.TurnOn(cmd.turn_on)
            elif cmd.HasField("turn_off"):
                with rm.PollenSpan(tracer=self.tracer, trace_name="arm_turn_off"):
                    self.arm_stub.TurnOff(cmd.turn_off)
            elif cmd.HasField("speed_limit"):
                with rm.PollenSpan(tracer=self.tracer, trace_name="arm_speed_limit"):
                    self.arm_stub.SetSpeedLimit(cmd.speed_limit)
            elif cmd.HasField("torque_limit"):
                with rm.PollenSpan(tracer=self.tracer, trace_name="arm_torque_limit"):
                    self.arm_stub.SetTorqueLimit(cmd.torque_limit)
            else:
                self.logger.warning(f"Unknown arm command : {cmd}")

    def handle_hand_command(self, cmd: webrtc_bridge_pb2.HandCommand) -> None:
        with rm.PollenSpan(tracer=self.tracer, trace_name="handle_hand_command"):
            if cmd.HasField("hand_goal"):
                with rm.PollenSpan(tracer=self.tracer, trace_name="hand_goal"):
                    self.hand_stub.SetHandPosition(cmd.hand_goal, timeout=5)
            elif cmd.HasField("turn_on"):
                with rm.PollenSpan(tracer=self.tracer, trace_name="hand_turn_on"):
                    self.hand_stub.TurnOn(cmd.turn_on)
            elif cmd.HasField("turn_off"):
                with rm.PollenSpan(tracer=self.tracer, trace_name="hand_turn_off"):
                    self.hand_stub.TurnOff(cmd.turn_off)
            else:
                self.logger.warning(f"Unknown hand command : {cmd}")

    def handle_neck_command(self, cmd: webrtc_bridge_pb2.NeckCommand) -> None:
        with rm.PollenSpan(tracer=self.tracer, trace_name="handle_neck_command"):
            if cmd.HasField("neck_goal"):
                with rm.PollenSpan(tracer=self.tracer, trace_name="neck_goal"):
                    self.head_stub.SendNeckJointGoal(cmd.neck_goal, timeout=5)
            elif cmd.HasField("turn_on"):
                with rm.PollenSpan(tracer=self.tracer, trace_name="neck_turn_on"):
                    self.head_stub.TurnOn(cmd.turn_on)
                command_antennas = dynamixel_motor_pb2.DynamixelMotorsCommand(
                    cmd=[
                        dynamixel_motor_pb2.DynamixelMotorCommand(
                            id=component_pb2.ComponentId(name="antenna_right"),
                            compliant=BoolValue(value=False),
                        ),
                        dynamixel_motor_pb2.DynamixelMotorCommand(
                            id=component_pb2.ComponentId(name="antenna_left"),
                            compliant=BoolValue(value=False),
                        ),
                    ]
                )
                self.handle_antennas_command(command_antennas)
            elif cmd.HasField("turn_off"):
                with rm.PollenSpan(tracer=self.tracer, trace_name="neck_turn_off"):
                    self.head_stub.TurnOff(cmd.turn_off)
                command_antennas = dynamixel_motor_pb2.DynamixelMotorsCommand(
                    cmd=[
                        dynamixel_motor_pb2.DynamixelMotorCommand(
                            id=component_pb2.ComponentId(name="antenna_right"),
                            compliant=BoolValue(value=True),
                        ),
                        dynamixel_motor_pb2.DynamixelMotorCommand(
                            id=component_pb2.ComponentId(name="antenna_left"),
                            compliant=BoolValue(value=True),
                        ),
                    ]
                )
                self.handle_antennas_command(command_antennas)
            elif cmd.HasField("speed_limit"):
                with rm.PollenSpan(tracer=self.tracer, trace_name="neck_speed_limit"):
                    self.head_stub.SetSpeedLimit(cmd.speed_limit)
            elif cmd.HasField("torque_limit"):
                with rm.PollenSpan(tracer=self.tracer, trace_name="neck_torque_limit"):
                    self.head_stub.SetTorqueLimit(cmd.torque_limit)
            else:
                self.logger.warning(f"Unknown neck command : {cmd}")

    def handle_mobile_base_command(self, cmd: webrtc_bridge_pb2.MobileBaseCommand) -> None:
        with rm.PollenSpan(tracer=self.tracer, trace_name="handle_mobile_base_command"):
            if cmd.HasField("target_direction"):
                with rm.PollenSpan(tracer=self.tracer, trace_name="mobile_base_target_direction"):
                    self.mb_mobility_stub.SendDirection(cmd.target_direction, timeout=10)
            elif cmd.HasField("mobile_base_mode"):
                with rm.PollenSpan(tracer=self.tracer, trace_name="mobile_base_mode"):
                    self.mb_utility_stub.SetZuuuMode(cmd.mobile_base_mode)
            else:
                self.logger.warning(f"Unknown mobile base command : {cmd}")

    def handle_antennas_command(self, cmd: dynamixel_motor_pb2.DynamixelMotorsCommand) -> None:
        self.logger.error(f"ANTENNAS COMMAND : {cmd}")
        with rm.PollenSpan(tracer=self.tracer, trace_name="handle_antennas_command"):
            self.dxl_motor_stub.SendCommand(cmd)
