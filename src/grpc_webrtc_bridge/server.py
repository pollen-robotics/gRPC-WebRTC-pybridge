import argparse
import asyncio
import logging
import queue
import sys
import time
from collections import deque
from queue import Queue

# from multiprocessing import Process, Queue,
from threading import Semaphore, Thread

# from queue import Queue
import aiortc
import grpc
from gst_signalling import GstSession, GstSignallingProducer
from reachy2_sdk_api import reachy_pb2, reachy_pb2_grpc
from reachy2_sdk_api.webrtc_bridge_pb2 import (
    AnyCommands,
    Connect,
    ConnectionStatus,
    ServiceRequest,
    ServiceResponse,
)

from .grpc_client import GRPCClient

last_freq_counter = {"neck": 0, "r_arm": 0, "l_arm": 0, "r_hand": 0, "l_hand": 0, "mobile_base": 0}
last_freq_update = {
    "neck": time.time(),
    "r_arm": time.time(),
    "l_arm": time.time(),
    "r_hand": time.time(),
    "l_hand": time.time(),
    "mobile_base": time.time(),
}
last_drop_counter = 0
freq_rates = []
drop_rates = []
drop_array = []
reentrancte_counter = 0
parse_time_arr = []
test_time_arr = []
init = False
# std_queue = deque(maxlen=1)
important_queue = Queue()
std_queue = {
    "neck": deque(maxlen=1),
    "r_arm": deque(maxlen=1),
    "l_arm": deque(maxlen=1),
    "r_hand": deque(maxlen=1),
    "l_hand": deque(maxlen=1),
    "mobile_base": deque(maxlen=1),
}
# important_queue = {
#     "neck": Queue(),
#     "r_arm": Queue(),
#     "l_arm": Queue(),
#     "r_hand": Queue(),
#     "l_hand": Queue(),
#     "mobile_base": Queue(),
# }


class GRPCWebRTCBridge:
    def __init__(self, args: argparse.Namespace) -> None:
        self.logger = logging.getLogger(__name__)

        self.producer = GstSignallingProducer(
            host=args.webrtc_signaling_host,
            port=args.webrtc_signaling_port,
            name="grpc_webrtc_bridge",
        )
        # self.smart_lock = Lock()
        self.smart_lock = Semaphore(1)
        self.grpc_host = args.grpc_host
        self.grpc_port = args.grpc_port
        self.asloop = asyncio.get_event_loop()
        # asyncio.set_event_loop(asloop)

        @self.producer.on("new_session")  # type: ignore[misc]
        def on_new_session(session: GstSession) -> None:
            self.pc = session.pc

            self.grpc_client = GRPCClient(args.grpc_host, args.grpc_port)

            service_channel = self.pc.createDataChannel("service")

            @service_channel.on("message")  # type: ignore[misc]
            async def service_channel_message(message: bytes) -> None:
                request = ServiceRequest()
                request.ParseFromString(message)

                response = await self.handle_service_request(request, self.grpc_client, self.pc)

                service_channel.send(response.SerializeToString())

    async def serve4ever(self) -> None:
        await self.producer.serve4ever()

    async def close(self) -> None:
        await self.producer.close()

    # Handle service request
    async def handle_service_request(
        self,
        request: ServiceRequest,
        grpc_client: GRPCClient,
        pc: aiortc.RTCPeerConnection,
    ) -> ServiceResponse:
        self.logger.info(f"Received service request message: {request}")

        if request.HasField("get_reachy"):
            resp = await self.handle_get_reachy_request(grpc_client)

        elif request.HasField("connect"):
            resp = await self.handle_connect_request(request.connect, grpc_client, pc)

        elif request.HasField("disconnect"):
            resp = await self.handle_disconnect_request()

        self.logger.info(f"Sending service response message: {resp}")
        return resp

    async def handle_get_reachy_request(self, grpc_client: GRPCClient) -> ServiceResponse:
        reachy = grpc_client.get_reachy()

        resp = ServiceResponse(
            connection_status=ConnectionStatus(
                connected=True,
                state_channel=f"reachy_state_{reachy.id.id}",
                command_channel=f"reachy_command_{reachy.id.id}",
                reachy=reachy,
            ),
        )

        return resp

    async def handle_connect_request(  # noqa: C901
        self,
        request: Connect,
        grpc_client: GRPCClient,
        pc: aiortc.RTCPeerConnection,
    ) -> ServiceResponse:
        # Create state data channel and start sending state
        if request.update_frequency >= 1000:
            max_packet_lifetime = 1
        else:
            max_packet_lifetime = (int)(1000 // request.update_frequency)

        reachy_state_datachannel = pc.createDataChannel(
            f"reachy_state_{request.reachy_id.id}",
            maxPacketLifeTime=max_packet_lifetime,
        )

        @reachy_state_datachannel.on("open")  # type: ignore[misc]
        def on_reachy_state_datachannel_open() -> None:

            reachy_stub_async = reachy_pb2_grpc.ReachyServiceStub(
                grpc.aio.insecure_channel(f"{grpc_client.host}:{grpc_client.port}")
            )

            async def get_reachy_state(reachy_id, publish_frequency):
                stream_req = reachy_pb2.ReachyStreamStateRequest(
                    id=reachy_id,
                    publish_frequency=publish_frequency,
                )

                async for state in reachy_stub_async.StreamReachyState(stream_req):
                    yield state

            async def send_joint_state() -> None:
                try:
                    async for state in get_reachy_state(
                        request.reachy_id,
                        request.update_frequency,
                    ):
                        reachy_state_datachannel.send(state.SerializeToString())
                except aiortc.exceptions.InvalidStateError:
                    logging.info("Data channel closed.")

            asyncio.ensure_future(send_joint_state())

        # Create command data channel and start handling commands
        # assuming data sent by Unity in the FixedUpdate loop. frequency 50Hz / ev 20ms
        reachy_command_datachannel = pc.createDataChannel(f"reachy_command_{request.reachy_id.id}", maxPacketLifeTime=20)

        @reachy_command_datachannel.on("message")  # type: ignore[misc]
        async def on_reachy_command_datachannel_message(
            message: bytes,
        ) -> None:

            global last_freq_counter
            global last_freq_update
            global last_drop_counter
            global freq_rates
            global drop_rates
            global init
            global drop_array
            global reentrancte_counter
            global parse_time_arr
            global test_time_arr
            global msg_queue
            reentrancte_counter += 1

            commands = AnyCommands()
            commands.ParseFromString(message)

            if not commands.commands:
                self.logger.info("No command or incorrect message received {message}")
                return

            # TODO better, temporary message priority
            important_msg = False
            for cmd in commands.commands:
                if cmd.HasField("arm_command"):
                    if (
                        cmd.arm_command.HasField("turn_on")
                        or cmd.arm_command.HasField("turn_off")
                        or cmd.arm_command.HasField("speed_limit")
                        or cmd.arm_command.HasField("torque_limit")
                    ):
                        important_msg = True

                elif cmd.HasField("hand_command"):
                    if cmd.hand_command.HasField("turn_on") or cmd.hand_command.HasField("turn_off"):
                        important_msg = True

                elif cmd.HasField("neck_command"):
                    if cmd.neck_command.HasField("turn_on") or cmd.neck_command.HasField("turn_off"):
                        important_msg = True

                elif cmd.HasField("mobile_base_command"):
                    if cmd.mobile_base_command.HasField("mobile_base_mode"):
                        important_msg = True

            if not important_msg:
                for cmd in commands.commands:
                    if cmd.HasField("arm_command"):
                        std_queue[cmd.arm_command.arm_cartesian_goal.id.name].append(cmd.arm_command)
                    if cmd.HasField("hand_command"):
                        std_queue[cmd.hand_command.hand_goal.id.name].append(cmd.hand_command)
                    if cmd.HasField("neck_command"):
                        std_queue["neck"].append(cmd.neck_command)
                    if cmd.HasField("mobile_base_command"):
                        std_queue["mobile_base"].append(cmd.mobile_base_command)
            else:
                important_queue.put(commands)

            reentrancte_counter -= 1

        return ServiceResponse()

    async def handle_disconnect_request(self) -> ServiceResponse:
        # TODO: implement me
        self.logger.info("Disconnecting...")

        return ServiceResponse()


def msg_handling(message, logger, part_name, part_handler):
    global last_freq_counter
    global last_freq_update

    last_freq_counter[part_name] += 1

    part_handler(message)

    now = time.time()
    if now - last_freq_update[part_name] > 1:
        current_freq_rate = int(last_freq_counter[part_name] / (now - last_freq_update[part_name]))

        logger.info(f"Freq {part_name} {current_freq_rate} Hz")

        last_freq_counter[part_name] = 0
        last_freq_update[part_name] = now


###### Routines


def handle_std_queue_routine(std_queue, part_name, part_handler):
    logger = logging.getLogger(__name__)
    while True:

        try:
            msg = std_queue.pop()
            msg_handling(msg, logger, part_name, part_handler)
        except IndexError:
            time.sleep(0.001)


def handle_important_queue_routine(grpc_client, logger):
    while True:
        grpc_client.handle_commands(important_queue.get())


#### MAIN


def main() -> int:  # noqa: C901
    parser = argparse.ArgumentParser()

    # gRPC
    parser.add_argument(
        "--grpc-host",
        type=str,
        default="127.0.0.1",
        help="Host of the grpc server.",
    )
    parser.add_argument(
        "--grpc-port",
        type=int,
        default=50051,
        help="Port of the grpc server.",
    )

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

    bridge = GRPCWebRTCBridge(args)

    time.sleep(2)

    grpc_client = GRPCClient(bridge.grpc_host, bridge.grpc_port)
    part_handlers = {
        "neck": grpc_client.handle_neck_command,
        "r_arm": grpc_client.handle_arm_command,
        "l_arm": grpc_client.handle_arm_command,
        "r_hand": grpc_client.handle_hand_command,
        "l_hand": grpc_client.handle_hand_command,
        "mobile_base": grpc_client.handle_mobile_base_command,
    }

    for part in std_queue.keys():
        Thread(
            target=handle_std_queue_routine,
            args=(
                std_queue[part],
                part,
                part_handlers[part],
            ),
        ).start()

    Thread(
        target=handle_important_queue_routine,
        args=(
            grpc_client,
            bridge.logger,
        ),
    ).start()

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(bridge.serve4ever())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(bridge.close())

    return 0


if __name__ == "__main__":
    sys.exit(main())
