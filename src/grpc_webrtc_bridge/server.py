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

last_freq_counter = 0
last_freq_update = time.time()
last_drop_counter = 0
freq_rates = []
drop_rates = []
drop_array = []
reentrancte_counter = 0
parse_time_arr = []
test_time_arr = []
init = False
std_queue = deque(maxlen=1)
important_queue = Queue()


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
            # Thread(target=send_joint_state).start()

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

            # self.logger.info("got a msg to depile")

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
                        important_log = f"Arm command: turn_on {cmd.arm_command.HasField('turn_on')} \
                                            turn_off {cmd.arm_command.HasField('turn_off')} \
                                            speed_limit {cmd.arm_command.HasField('speed_limit')} \
                                            torque_limit {cmd.arm_command.HasField('torque_limit')}"

                elif cmd.HasField("hand_command"):
                    if cmd.hand_command.HasField("turn_on") or cmd.hand_command.HasField("turn_off"):
                        important_msg = True
                        important_log = f"Hand command: turn_on {cmd.hand_command.HasField('turn_on')} \
                                            turn_off {cmd.hand_command.HasField('turn_off')}"
                elif cmd.HasField("neck_command"):
                    if cmd.neck_command.HasField("turn_on") or cmd.neck_command.HasField("turn_off"):
                        important_msg = True
                        important_log = f"Neck command: turn_on {cmd.neck_command.HasField('turn_on')} \
                                            turn_off {cmd.neck_command.HasField('turn_off')}"
                elif cmd.HasField("mobile_base_command"):
                    if cmd.mobile_base_command.HasField("mobile_base_mode"):
                        important_msg = True
                        important_log = f"Mobile base command: mobile_base_mode {str(cmd.mobile_base_command.mobile_base_mode)}"

            if not important_msg:
                std_queue.append(commands)
            else:
                important_queue.put(commands)

            reentrancte_counter -= 1
            # await asyncio.sleep(0.001)

        return ServiceResponse()

    async def handle_disconnect_request(self) -> ServiceResponse:
        # TODO: implement me
        self.logger.info("Disconnecting...")

        return ServiceResponse()


import threading


def msg_handling(message, grpc_client, logger):
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

    last_freq_counter += 1

    # asyncio.run(grpc_client.handle_commands(commands))

    # bridge.asloop.run_until_complete(bridge.grpc_client.handle_commands(commands))
    # future = asyncio.run_coroutine_threadsafe(grpc_client.handle_commands(commands), loop)
    # future.result()

    grpc_client.handle_commands(message)
    drop_array.append(0)
    # if important_msg:
    #     logger.info(f"Some important command has been allowed\n{important_log}")

    now = time.time()
    if now - last_freq_update > 1:
        current_freq_rate = int(last_freq_counter / (now - last_freq_update))
        current_drop_rate = int(last_drop_counter / (now - last_freq_update))

        logger.info(f"Freq {current_freq_rate} Hz\tDrop {current_drop_rate} Hz")
        # logger.info(str(drop_array))
        # self.logger.info(str(parse_time_arr))
        # self.logger.info(str(test_time_arr))
        drop_array = []
        parse_time_arr = []
        test_time_arr = []

        if init and False:
            freq_rates.append(current_freq_rate)
            drop_rates.append(current_drop_rate)
            if len(freq_rates) > 10000:
                freq_rates.pop(0)
                drop_rates.pop(0)
            mean_freq_rate = sum(freq_rates) / len(freq_rates)
            mean_drop_rate = sum(drop_rates) / len(drop_rates)
            logger.info(f"[MEAN] Freq {mean_freq_rate} Hz\tDrop {mean_drop_rate} Hz")
        else:
            init = True
        # Calculate mean values

        last_freq_counter = 0
        last_drop_counter = 0
        last_freq_update = now


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

    def print_reantrance(bridge):
        print(f"\n{reentrancte_counter}\n")
        try:
            while True:
                bridge.logger.info(f"\n{reentrancte_counter} || {msg_queue.qsize()}\n")
                time.sleep(1)
        except:
            print("\n\oh no le bio\n")

    x = threading.Thread(target=print_reantrance, args=(bridge,))
    x.start()

    def msg_handling_routine(std_queue, important_queue, grpc_host, grpc_port):
        grpc_client = GRPCClient(grpc_host, grpc_port)
        print("\n\_inside process handler\n\n\n ")
        logger = logging.getLogger(__name__)
        logger.info("\n\n\n \n\n\n \n\n\n hé ho ")
        # smart_lock = Lock()
        while True:

            while True:

                try:
                    msg = important_queue.get(block=False)
                    logger.info("Got an important message ! ")
                    msg_handling(msg, grpc_client, logger)
                except queue.Empty:
                    break
            try:
                msg = std_queue.pop()
                msg_handling(msg, grpc_client, logger)
            except IndexError:
                time.sleep(0.001)

    msg_handler_process = Thread(
        target=msg_handling_routine,
        args=(
            std_queue,
            important_queue,
            bridge.grpc_host,
            bridge.grpc_port,
        ),
    )
    msg_handler_process.start()
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
