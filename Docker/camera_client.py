import argparse
import json
import socket
import struct
import sys

import cv2

from gesture_protocol import TYPE_FRAME, encode_message, receive_exact


MAX_WIDTH = 640
MAX_HEIGHT = 480
MAX_FRAME_SIZE = 2 * 1024 * 1024
MAX_RESPONSE_SIZE = 4096
SOCKET_TIMEOUT_SECONDS = 10


def encode_frame(frame):
    height, width = frame.shape[:2]
    scale = min(1.0, MAX_WIDTH / width, MAX_HEIGHT / height)
    if scale < 1.0:
        frame = cv2.resize(
            frame,
            (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )

    for quality in (80, 65, 50):
        encoded, jpeg = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality]
        )
        if not encoded:
            raise RuntimeError("OpenCV could not encode the camera frame.")
        if len(jpeg) <= MAX_FRAME_SIZE:
            return frame, jpeg.tobytes()

    raise ValueError("JPEG frame exceeds the 2 MiB limit.")


def receive_prediction(sock):
    header = receive_exact(sock, 4)
    if header is None:
        raise ConnectionError("Gesture server closed the connection.")

    response_size = struct.unpack("!I", header)[0]
    if not 0 < response_size <= MAX_RESPONSE_SIZE:
        raise ValueError("Server returned an invalid response size.")

    payload = receive_exact(sock, response_size)
    if payload is None:
        raise ConnectionError("Gesture server closed during its response.")

    response = json.loads(payload.decode("utf-8"))
    if not isinstance(response, dict) or (
        response.get("gesture") is not None
        and not isinstance(response.get("gesture"), str)
    ):
        raise ValueError("Server returned an invalid gesture response.")
    return response.get("gesture")


def main():
    parser = argparse.ArgumentParser(description="Send webcam frames to the gesture server.")
    parser.add_argument("--host", default="127.0.0.1", help="Docker host address")
    parser.add_argument("--port", type=int, default=5000, help="Gesture server TCP port")
    args = parser.parse_args()

    camera = cv2.VideoCapture(0)
    if not camera.isOpened():
        raise RuntimeError("Could not open webcam 0.")

    try:
        try:
            connection = socket.create_connection(
                (args.host, args.port), timeout=SOCKET_TIMEOUT_SECONDS
            )
        except OSError as exc:
            print(f"Could not connect to gesture server at {args.host}:{args.port}: {exc}")
            return 1

        with connection as sock:
            sock.settimeout(SOCKET_TIMEOUT_SECONDS)
            while True:
                ok, frame = camera.read()
                if not ok:
                    print("Could not read a frame from webcam 0.")
                    break

                frame = cv2.flip(frame, 1)
                try:
                    display_frame, jpeg = encode_frame(frame)
                except ValueError as exc:
                    print(f"Skipping frame: {exc}")
                    continue

                sock.sendall(encode_message(TYPE_FRAME, jpeg))
                gesture = receive_prediction(sock)

                cv2.putText(
                    display_frame,
                    f"Gesture: {gesture or 'None'}",
                    (10, 40),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1,
                    (0, 255, 0),
                    2,
                )
                cv2.imshow("Hand Gesture Recognition", display_frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    except (OSError, ValueError) as exc:
        print(f"Gesture connection stopped: {exc}")
        return 1
    finally:
        camera.release()
        cv2.destroyAllWindows()

    return 0


if __name__ == "__main__":
    sys.exit(main())
