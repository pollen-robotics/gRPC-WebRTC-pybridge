# gRPC <--> WebRTC bridge

This is a simple bridge between gRPC and WebRTC. It allows you to call the gRPC services from the SDK server through WebRTC.

## Installation

* ```pip install -e .```

## Usage

* Launch the SDK server (gRPC)
```ros2 launch reachy_bringup reachy.launch.py fake:=true start_rviz:=true start_sdk_server:=true```

* Launch the signalling server
```gst-webrtc-signalling-server```

* Launch the bridge
```python -m grpc_webrtc_bridge.server```

* Launch any number of WebRTC client (see [example](./example) for simple examples)
For instamce, in three terminals, run:
```python simple_state_client.py```
```python simple_state_client.py```
```python simple_command_client.py```