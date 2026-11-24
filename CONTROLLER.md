# Windows Tello controller and Docker gesture server

The Windows controller subscribes to gesture events from the server at
`127.0.0.1:5000`; it does not open a webcam or run MediaPipe. The Docker server
accepts explicitly typed camera-frame (`F`) and controller-subscription (`S`)
messages. Legacy 4-byte-length-prefixed JPEG camera clients are also accepted.
Gesture notifications contain a monotonically increasing sequence number and
are sent only when a new held gesture is confirmed. A new controller receives
the server's current sequence in its subscription acknowledgement and does not
replay prior gestures.

## Initial testing mode

`DRY_RUN` is `True` and `FLIGHT_COMMANDS_ENABLED` is `False` in
`drone_controller.py`. The controller connects to the Tello to read status, but
takeoff, land, movement, rotation, and shutdown landing calls are not sent.
Do not treat successful tests as flight-readiness; real-world testing,
independent failsafe planning, and explicit code review are still required.

## Run

From PowerShell in `D:\Drone`:

```powershell
docker build -t drone-gesture-server .\Docker
docker run --rm -p 5000:5000 drone-gesture-server
```

In another PowerShell window, start the webcam client:

```powershell
py .\Docker\camera_client.py
```

Start the controller in a third window. Install `djitellopy` into the selected
Windows Python environment first if it is not already available. The webcam
client also needs OpenCV if it is not already installed:

```powershell
py -m pip install opencv-python djitellopy
py .\drone_controller.py
```

Run the test suite with the Docker image (it supplies the server-side
MediaPipe/OpenCV dependencies):

```powershell
docker run --rm -v "D:\Drone:/workspace" -w /workspace drone-gesture-server python -m unittest discover -s tests -v
```

Press **Q** in the camera window to stop video capture. Press **Ctrl+C** in the
controller window to stop the controller. A server disconnection ends the
controller session; it does not automatically reconnect or send a landing
command. Restarting the controller begins at the server's current sequence, so
it will not execute a gesture confirmed before the new subscription.

## Emergency landing

If gesture control, the controller process, Wi-Fi, or the Docker server becomes
unresponsive while the Tello is airborne, use the DJI Tello mobile app or a
paired/manual Tello controller to command landing immediately, and keep the
aircraft in clear space. Do not rely on this program to land after a network
failure. If the Tello loses its control link, its own firmware behavior applies;
it may hover temporarily and then initiate its built-in landing behavior, but
that is not a guaranteed substitute for a manual landing command. Keep people
and obstacles clear and consult DJI's current product guidance.
