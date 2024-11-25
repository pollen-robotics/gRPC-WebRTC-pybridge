import argparse
import asyncio
import logging
import sys
import time


import numpy as np

from reachy2_sdk import ReachySDK
from reachy2_sdk.parts.joints_based_part import JointsBasedPart
import numpy.typing as npt


# gi.require_version("Gst", "1.0")

# from gi.repository import GLib, Gst, GstWebRTC  # noqa : E402

# These are integer values between 0 and 100
TORQUE_LIMIT=100
SPEED_LIMIT=100

RADIUS = 0.2  # Circle radius
FIXED_X = 0.5  # Fixed x-coordinate
CENTER_Y, CENTER_Z = 0, 0.1  # Center of the circle in y-z plane
Y_OFFSET = 0.2

INIT_ANGLE = 2*np.pi*0.7 # Important that the first pose is reachable



def build_pose_matrix(x: float, y: float, z: float) -> npt.NDArray[np.float64]:
    """Build a 4x4 pose matrix for a given position in 3D space, with the effector at a fixed orientation.

    Args:
        x: The x-coordinate of the position.
        y: The y-coordinate of the position.
        z: The z-coordinate of the position.

    Returns:
        A 4x4 NumPy array representing the pose matrix.
    """
    # The effector is always at the same orientation in the world frame
    return np.array(
        [
            [0, 0, -1, x], # it should be -1 to point forward, but doing this creates more unreachable poses that are good for debug
            [0, 1, 0, y],
            [1, 0, 0, z],
            [0, 0, 0, 1],
        ]
    )

def set_speed_and_torque_limits(reachy, torque_limit=100, speed_limit=25) -> None:
    """Set back speed and torque limits of all parts to given value."""
    if not reachy.info:
        reachy._logger.warning("Reachy is not connected!")
        return

    for part in reachy.info._enabled_parts.values():
        if issubclass(type(part), JointsBasedPart):
            part.set_speed_limits(speed_limit)
            part.set_torque_limits(torque_limit)
    time.sleep(0.5)

def smooth_goto_init(args) -> None:
    print(f"Connecting to Reachy at {args.webrtc_signalling_host}")
    reachy = ReachySDK(host=args.webrtc_signalling_host)

    if not reachy.is_connected:
        exit("Reachy is not connected.")

    print("Turning on Reachy")
    reachy.turn_on()
    
    set_speed_and_torque_limits(reachy, torque_limit=TORQUE_LIMIT, speed_limit=SPEED_LIMIT)
    time.sleep(0.2)
    angle = INIT_ANGLE
    i = 0
    while True :
        # Calculate y and z coordinates
        angle = INIT_ANGLE + 2*np.pi/20*1
        y = CENTER_Y + RADIUS * np.cos(angle)
        z = CENTER_Z + RADIUS * np.sin(angle)

        r_target_pose = build_pose_matrix(FIXED_X, y - Y_OFFSET, z)
        l_target_pose = build_pose_matrix(FIXED_X, y + Y_OFFSET, z)

        print(f"Moving to initial pose: {r_target_pose} and {l_target_pose}")
        try :
            r_ik = reachy.r_arm.inverse_kinematics(r_target_pose)
            l_ik = reachy.l_arm.inverse_kinematics(l_target_pose)
        except Exception as e:
            print(e)
            print("init pose is not reachable, not moving")
            # raise e
            continue
        reachy.r_arm.goto(r_ik, duration=3.0, degrees=True)
        reachy.l_arm.goto(l_ik, duration=3.0, degrees=True, wait=True)
    
def emergency_stop(args) -> None:
    print(f"Connecting to Reachy at {args.webrtc_signalling_host}")
    reachy = ReachySDK(host=args.webrtc_signalling_host)

    if not reachy.is_connected:
        exit("Reachy is not connected.")

    print("Turning off Reachy")
    reachy.turn_off_smoothly()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    # WebRTC
    parser.add_argument(
        "--webrtc-signalling-host",
        type=str,
        default="localhost",
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

    try :
        smooth_goto_init(args)
    except Exception as e:
        print(e)
        raise e
    finally :
        emergency_stop(args)
    
