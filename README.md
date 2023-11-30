# gRPC <--> WebRTC bridge

This is a simple bridge between gRPC and WebRTC. It allows you to call the gRPC services from the SDK server through WebRTC.

## Installation

* ```pip install -e .```

## Usage

* Launch the Robot
```ros2 launch reachy_bringup reachy.launch.py fake:=true```

* Launch the SDK server
```ros2 run reachy_sdk_server reachy_grpc_joint_sdk_server ./src/reachy2_sdk_server/config/reachy_full_kit.yaml```

* Launch the signalling server
```gst-webrtc-signalling-server```

* Launch the bridge
```python -m grpc_webrtc_bridge.server```

* Launch the tele-op fake app:

  * ```cd src/examples```
  * ```python teleop.py```
