import logging
from typing import AsyncGenerator
import grpc

from google.protobuf.empty_pb2 import Empty
from reachy_sdk_api import joint_pb2_grpc, joint_pb2


class GRPCClient:
    def __init__(
        self,
        host: str,
        port: int,
    ) -> None:
        self.logger = logging.getLogger(__name__)

        self.host = host
        self.port = port

        channel = grpc.insecure_channel(f"{host}:{port}")
        joint_stub = joint_pb2_grpc.JointServiceStub(channel)
        self.joint_ids = joint_stub.GetAllJointsId(Empty())

        self.logger.info(
            f"Connected to grpc {host}:{port} with joints: {self.joint_ids.names}"
        )

    async def get_state(
        self,
    ) -> AsyncGenerator[joint_pb2.JointsState, None]:
        channel = grpc.aio.insecure_channel(f"{self.host}:{self.port}")
        joint_stub = joint_pb2_grpc.JointServiceStub(channel)

        stream_req = joint_pb2.StreamJointsRequest(
            request=joint_pb2.JointsStateRequest(
                ids=[joint_pb2.JointId(uid=uid) for uid in self.joint_ids.uids],
                requested_fields=[
                    joint_pb2.JointField.PRESENT_POSITION,
                    joint_pb2.JointField.TEMPERATURE,
                ],
            ),
            publish_frequency=100,
        )

        async for state in joint_stub.StreamJointsState(stream_req):
            yield state
