import asyncio
import logging
from typing import AsyncGenerator

import grpc
from google.protobuf.empty_pb2 import Empty
from reachy2_sdk_api import (
    arm_pb2_grpc,
    hand_pb2_grpc,
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

        self.q_hand: asyncio.Queue[webrtc_bridge_pb2.HandCommand] = asyncio.Queue(maxsize=100)
        self.q_hand_position: asyncio.Queue[hand_pb2_grpc.HandPositionRequest] = asyncio.Queue(maxsize=100)
        self.q_arm: asyncio.Queue[webrtc_bridge_pb2.ArmCommand] = asyncio.Queue(maxsize=100)
        self.q_neck: asyncio.Queue[webrtc_bridge_pb2.NeckCommand] = asyncio.Queue(maxsize=100)

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
        publish_frequency: float,
    ) -> AsyncGenerator[reachy_pb2.ReachyState, None]:
        stream_req = reachy_pb2.ReachyStreamStateRequest(
            id=reachy_id,
            publish_frequency=publish_frequency,
        )

        async for state in self.reachy_stub.StreamReachyState(stream_req):
            yield state

    # Send Commands (torque and cartesian targets)
    async def handle_commands(self, commands: webrtc_bridge_pb2.AnyCommands) -> None:
        self.logger.info(f"Received message: {commands}")

        # TODO: Could this be done in parallel?
        for cmd in commands.commands:
            if cmd.HasField("arm_command"):
                await self.q_arm.put(cmd.arm_command)
            if cmd.HasField("hand_command"):
                if cmd.hand_command.HasField("hand_goal"):
                    await self.q_hand_position.put(cmd.hand_command.hand_goal)
                else:
                    await self.q_hand.put(cmd.hand_command)
            if cmd.HasField("neck_command"):
                await self.q_neck.put(cmd.neck_command)

    async def consume_hand_command(self) -> None:
        while True:
            cmd = await self.q_hand.get()
            if cmd.HasField("hand_goal"):
                await self.hand_stub.SetHandPosition(cmd.hand_goal)
            if cmd.HasField("turn_on"):
                await self.hand_stub.TurnOn(cmd.turn_on)
            if cmd.HasField("turn_off"):
                await self.hand_stub.TurnOff(cmd.turn_off)
            self.q_hand.task_done()

    async def consume_hand_position_command(self) -> None:
        # def hand_position_generator() -> AsyncGenerator[hand_pb2_grpc.HandPositionRequest, None]: hangs???
        async def hand_position_generator():  # type: ignore[no-untyped-def]
            while True:
                hand_position_command = await self.q_hand_position.get()
                yield hand_position_command

        await self.hand_stub.SetHandPositions(hand_position_generator())  # type: ignore[no-untyped-call]

    async def consume_arm_command(self) -> None:
        while True:
            cmd = await self.q_arm.get()
            if cmd.HasField("arm_cartesian_goal"):
                await self.arm_stub.GoToCartesianPosition(cmd.arm_cartesian_goal)
            if cmd.HasField("turn_on"):
                await self.arm_stub.TurnOn(cmd.turn_on)
            if cmd.HasField("turn_off"):
                await self.arm_stub.TurnOff(cmd.turn_off)
            self.q_arm.task_done()

    async def consume_neck_command(self) -> None:
        while True:
            cmd = await self.q_neck.get()
            if cmd.HasField("neck_goal"):
                await self.head_stub.GoToOrientation(cmd.neck_goal)
            if cmd.HasField("turn_on"):
                await self.head_stub.TurnOn(cmd.turn_on)
            if cmd.HasField("turn_off"):
                await self.head_stub.TurnOff(cmd.turn_off)
            self.q_neck.task_done()

    def start_consume_commands(self) -> None:
        self.logger.debug("Create consumers")
        self.task_consume_hand_commands = asyncio.create_task(self.consume_hand_command())
        self.task_consume_hand_position_commands = asyncio.create_task(self.consume_hand_position_command())
        self.task_consume_arm_commands = asyncio.create_task(self.consume_arm_command())
        self.task_consume_neck_commands = asyncio.create_task(self.consume_neck_command())

    async def stop_consume_commands(self) -> None:
        self.logger.debug("Close consumers")
        self.task_consume_hand_commands.cancel()
        self.task_consume_hand_position_commands.cancel()
        self.task_consume_arm_commands.cancel()
        self.task_consume_neck_commands.cancel()
        await self.q_hand.join()
        await self.q_hand_position.join()
        await self.q_arm.join()
        await self.q_neck.join()
