# gRPC <--> WebRTC bridge

This is a simple bridge between gRPC and WebRTC. It allows you to call the gRPC services from the SDK server through WebRTC. It relies on the gstreamer webrtc implementation.

## Installation

```pip install -e .[dev, pollen, examples]```

*dev, pollen, examples are optional parameters for developers*

## Usage

Please see https://github.com/pollen-robotics/docker_webrtc for production use

### Requirements

* Launch the (fake) Robot using the [core docker image](https://github.com/pollen-robotics/docker_reachy2_core)
```ros2 launch reachy_bringup reachy.launch.py start_sdk_server:=true start_rviz:=true fake:=true```

* Launch a signalling server using [webrtc docker image](https://github.com/pollen-robotics/docker_webrtc)
```gst-webrtc-signalling-server```

* Launch the bridge from a webrtc docker image
```grpc_webrtc_bridge --webrtc-signaling-host signalling-server --grpc-host 172.17.0.1 --verbose```

### Fake teleop example

* Launch the fake teleop app:

  * ```python src/example/simulate_teleop_data.py```
