# Gesture-Controlled DJI Tello

A Windows + Docker prototype for hand-gesture recognition and Tello control.
The webcam and display run on Windows; the Docker service processes camera
frames with MediaPipe and publishes confirmed gesture events over TCP. The
Windows controller subscribes to those events and uses `djitellopy`.

> **Safety status:** this project is not flight-ready. The Windows controller
> has `DRY_RUN = True` and `FLIGHT_COMMANDS_ENABLED = False`; its flight methods
> only print the commands they would send. The legacy flight experiment has
> `DRY_RUN = False` and can issue real flight commands if run. Do not run that
> experiment with a powered drone.

## Features

- Windows webcam capture, horizontal flip, JPEG compression and live display.
- Docker-hosted MediaPipe Hands processing at detection/tracking confidence
  `0.7`, with the existing gesture classifier and landmark output.
- Typed TCP messages for camera frames and controller subscriptions, plus
  compatibility with the earlier length-prefixed JPEG camera protocol.
- Sequenced gesture events, one controller subscriber at a time, and stale
  event rejection.
- Controller-side battery, temperature, takeoff retry, settling, movement
  cooldown and repeated-gesture protections.
- No automatic reconnect or landing command when the gesture server
  disconnects.

## Architecture

```text
Windows webcam
    │ JPEG frames over TCP
    ▼
Docker gesture server ── MediaPipe Hands + gesture classification
    │ confirmed, sequenced gesture events over TCP
    ▼
Windows drone_controller.py ── djitellopy ── DJI Tello
```

The server listens on `0.0.0.0:5000`; Docker publishes TCP port 5000 to the
Windows host. The camera client defaults to `127.0.0.1:5000`, and the controller
subscribes to that same address. Webcam capture and display stay on Windows;
the controller does not open a webcam or run MediaPipe.

### TCP messages

New messages start with `DGST`, followed by a one-byte message type (`F` for a
camera frame or `S` for a gesture subscription), a four-byte network-order
payload length and the payload. Camera frames are JPEG-compressed and limited
to 2 MiB. Legacy clients may still send a four-byte length followed by JPEG
bytes without the typed header.

Responses are four-byte length-prefixed JSON. Camera responses include the
gesture, landmarks, sequence and processing time. A successful subscription
starts with the current sequence number and then receives events containing a
sequence and gesture. The event queue retains only the newest pending event,
so clients should expect sequence gaps when events arrive faster than they are
consumed; the controller rejects duplicate or older sequence numbers.

## Technologies

- Python 3.10 in the Docker image
- OpenCV, MediaPipe 0.10.9 and NumPy in Docker
- OpenCV and `djitellopy` on Windows
- Docker Desktop using Linux containers
- Python `unittest` for protocol, event and mocked-controller tests

## Repository layout

```text
Drone/
├── Docker/
│   ├── .dockerignore
│   ├── Dockerfile
│   ├── camera_client.py
│   ├── gesture_protocol.py
│   ├── gesture_server.py
│   └── requirements.txt
├── experiments/
│   ├── Learn_Gesture.py
│   └── Learn_Gesture_fly_drone.py
├── tests/
│   └── test_gesture_system.py
├── .gitignore
├── CONTROLLER.md
├── drone_controller.py
└── README.md
```

The two `experiments/` scripts are retained as legacy examples and are not
required by the Docker image. They directly open a webcam and initialize
MediaPipe; the flight experiment also initializes a Tello and has live flight
mode enabled by default. Review it carefully and do not run it as part of this
prototype.

## Prerequisites

- Windows with a webcam available as OpenCV camera index `0`.
- Docker Desktop running Linux containers.
- Python installed on Windows (`py` launcher).
- A Tello and `djitellopy` are required for the controller to connect. The
  controller attempts a Tello connection, but this prototype does not send
  flight commands.

## Installation and startup

Run commands from the repository root (`D:\Drone`) in PowerShell.

### 1. Build and start the Docker gesture server

```powershell
docker build -t tello-gesture:local .\Docker
docker run --rm -p 5000:5000 tello-gesture:local
```

The server is now listening on TCP port 5000. Leave this terminal running.

### 2. Install Windows-side packages, if needed

```powershell
py -m pip install opencv-python djitellopy
```

The Docker `requirements.txt` is for the AI server only; `djitellopy` is not
installed in the container.

### 3. Start the Windows webcam client

In another PowerShell window:

```powershell
py .\Docker\camera_client.py
```

The client uses camera index `0`. Press **Q** in the video window to exit.
Frames are scaled to at most 640×480 and JPEG-encoded before transmission.

### 4. Start the Windows controller

In a third PowerShell window, with the Tello available on the network:

```powershell
py .\drone_controller.py
```

The controller defaults to `127.0.0.1:5000`. It exits if the subscription is
rejected or the connection drops; it does not reconnect automatically. Press
**Ctrl+C** to stop it. In this development configuration, takeoff, landing,
movement, rotation and shutdown landing commands are not sent.

## Gesture stability

The server retains the existing gesture meanings. Although its stability
threshold constant is 10 frames, the default hold requires 20 frames before a
gesture is published; `land` requires 30 frames. A transition to no hand or an
unknown gesture clears the history. When the camera client disconnects, the
server publishes `UNKNOWN` and clears the tracking state.

## Tests

Run the complete test suite using the Docker image, which supplies the server
dependencies:

```powershell
docker run --rm -v "D:\Drone:/workspace" -w /workspace tello-gesture:local python -m unittest discover -s tests -v
```

Tests cover message framing and validation, legacy camera compatibility,
concurrent controller subscriptions, disconnect/reconnect sequence behavior,
simulated responses for each gesture, and controller safety logic using a
fake Tello. They do not test a physical webcam, actual hand classification for
every gesture, Tello connectivity, or flight.

## Safety and limitations

- Keep `DRY_RUN = True` and `FLIGHT_COMMANDS_ENABLED = False` in
  `drone_controller.py`. Do not change these settings without a separate safety
  review and explicit authorization.
- The legacy `experiments/Learn_Gesture_fly_drone.py` has `DRY_RUN = False`.
  It is not integrated into the Docker workflow and may send real commands.
- A gesture-server or TCP disconnect does **not** make this program send a
  landing command. If the Tello is airborne and control becomes unresponsive,
  use the DJI Tello app or a paired controller to land manually. Do not rely on
  assumed behavior after a lost control link.
- The unauthenticated TCP service is intended for local development. Do not
  expose port 5000 to untrusted networks.
- Camera access, lighting, gesture recognition accuracy, timing and actual
  drone behavior have not been validated as a complete flight system.

For the detailed controller notes, see [CONTROLLER.md](CONTROLLER.md).

## Development status and next steps

The hybrid transport, server event flow and dry-run controller are implemented.
Unit tests and a Docker build are available, but no real Tello flight or
full hardware acceptance test has been performed. Possible next steps include
landmark-driven classifier tests, authenticated transport, configurable
settings, structured logging and a supervised hardware test plan.
