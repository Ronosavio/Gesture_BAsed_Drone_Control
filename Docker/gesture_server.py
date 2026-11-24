import select
import socket
import threading
import time
from collections import deque
from queue import Empty, Full, Queue

import cv2
import mediapipe as mp
import numpy as np
from gesture_protocol import (
    TYPE_FRAME,
    TYPE_SUBSCRIBE,
    ProtocolError,
    encode_json,
    receive_exact,
    receive_request,
)


HOST = "0.0.0.0"
PORT = 5000
STABILITY_THRESHOLD = 10
DEFAULT_HOLD_FRAMES = 20
HOLD_FRAMES = {"land": 30}

mp_hands = mp.solutions.hands


def classify_gesture(landmarks):
    """
    Classifies the hand gesture based on landmark positions.

    Arguments:
    - landmarks: List of 21 (x, y) coordinates representing the detected hand keypoints.

    Returns:
    - String indicating the detected gesture.
    """

    # Check if hand is detected (21 landmarks expected)
    if len(landmarks) != 21:
        return "UNKNOWN"  # No hand detected

    # Palm
    palm_base = 1-landmarks[0][1]  # Center of the palm (Wrist)

    # Thumb
    thumb_base = 1-landmarks[1][1]
    thumb_middle = 1-landmarks[2][1]
    thumb_top = 1-landmarks[3][1]
    thumb_tip = 1-landmarks[4][1]

    # Index Finger (Pointing finger)
    index_base = 1-landmarks[5][1]
    index_middle = 1-landmarks[6][1]
    index_top = 1-landmarks[7][1]
    index_tip = 1-landmarks[8][1]

    # Middle Finger (Tall finger)
    middle_base = 1-landmarks[9][1]
    middle_middle = 1-landmarks[10][1]
    middle_top = 1-landmarks[11][1]
    middle_tip = 1-landmarks[12][1]

    # Ring Finger (Ring finger)
    ring_base = 1-landmarks[13][1]
    ring_middle = 1-landmarks[14][1]
    ring_top = 1-landmarks[15][1]
    ring_tip = 1-landmarks[16][1]

    # Pinky Finger (Little finger)
    little_base = 1-landmarks[17][1]
    little_middle = 1-landmarks[18][1]
    little_top = 1-landmarks[19][1]
    little_tip = 1-landmarks[20][1]

    # Step 1: Compute finger states (Extended or Folded)
    def is_finger_extended(tip, base):
        return tip > base + 0.02  # Ensure tip is significantly higher

    index_extended = is_finger_extended(index_tip, index_middle)
    middle_extended = is_finger_extended(middle_tip, middle_middle)
    ring_extended = is_finger_extended(ring_tip, ring_middle)
    little_extended = is_finger_extended(little_tip, little_middle)

    # Step 2: Identify Gestures

    # 👍 Thumbs Up, Others Folded
    if thumb_tip > index_tip and not index_extended and not middle_extended and not ring_extended and not little_extended:
        return "takeoff"

    # Middle, ring and little fingure extended, others folded
    elif not index_extended and middle_extended and ring_extended and little_extended:
        return "land"

    # ✋ Open Palm - All Fingers Fully Extended
    elif index_extended and middle_extended and ring_extended and little_extended:
        return "move_up"

    # ✊ Fist - No Fingers Extended
    elif not index_extended and not middle_extended and not ring_extended and not little_extended:
        return "move_down"

    # ☝ Index Finger Up, Others Folded
    elif index_extended and not middle_extended and not ring_extended and not little_extended:
        return "move_forward"

    # Little Fingure extended, others folded
    elif little_extended and not index_extended and not middle_extended and not ring_extended:
        return "move_back"

    # Index and middle fingure extended, others folded
    elif index_extended and middle_extended and not ring_extended and not little_extended:
        return "move_right"

    # Ring and little fingure extended, others folded
    elif ring_extended and little_extended and not index_extended and not middle_extended:
        return "move_left"

    # Index and little fingure extended, others folded
    elif index_extended and little_extended and not ring_extended and not middle_extended:
        return "rotate_clockwise"

    # Middle and ring fingure extended, others folded
    elif ring_extended and middle_extended and not index_extended and not little_extended:
        return "rotate_counter_clockwise"

    # Default Case
    else:
        return "UNKNOWN"


class GestureTracker:
    """Apply the legacy frame holds and emit one event per confirmed gesture."""

    def __init__(self):
        self.history = deque(maxlen=max(HOLD_FRAMES.values()))
        self.current_gesture = None

    def observe(self, gesture):
        if gesture not in HOLD_FRAMES and gesture not in VALID_GESTURES:
            gesture = "UNKNOWN"

        if gesture == "UNKNOWN":
            had_gesture = self.current_gesture is not None
            self.history.clear()
            self.current_gesture = None
            return None, "UNKNOWN" if had_gesture else None

        self.history.append(gesture)
        required_frames = max(STABILITY_THRESHOLD, HOLD_FRAMES.get(
            gesture, DEFAULT_HOLD_FRAMES
        ))
        if (
            len(self.history) >= required_frames
            and all(item == gesture for item in list(self.history)[-required_frames:])
        ):
            if self.current_gesture != gesture:
                self.current_gesture = gesture
                return gesture, gesture
            return gesture, None

        return self.current_gesture, None

    def disconnect(self):
        self.history.clear()
        self.current_gesture = None


VALID_GESTURES = {
    "takeoff",
    "land",
    "move_up",
    "move_down",
    "move_forward",
    "move_back",
    "move_right",
    "move_left",
    "rotate_clockwise",
    "rotate_counter_clockwise",
}


class ServerState:
    """Owns the exclusive controller subscription and event sequence."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sequence = 0
        self.controller_queue = None
        self.camera_lock = threading.Lock()

    def subscribe(self):
        with self.lock:
            if self.controller_queue is not None:
                return None
            self.controller_queue = Queue(maxsize=1)
            return self.controller_queue, self.sequence

    def unsubscribe(self, subscriber):
        with self.lock:
            if self.controller_queue is subscriber:
                self.controller_queue = None

    def publish(self, gesture):
        with self.lock:
            self.sequence += 1
            event = {
                "type": "event",
                "sequence": self.sequence,
                "gesture": gesture,
            }
            subscriber = self.controller_queue

        if subscriber is not None:
            try:
                subscriber.put_nowait(event)
            except Full:
                try:
                    subscriber.get_nowait()
                except Empty:
                    pass
                try:
                    subscriber.put_nowait(event)
                except Full:
                    pass
        return self.sequence


SERVER_STATE = ServerState()


def send_json(connection, response):
    connection.sendall(encode_json(response))


def handle_subscription(connection, address):
    subscription = SERVER_STATE.subscribe()
    if subscription is None:
        send_json(
            connection,
            {"type": "error", "message": "A controller is already subscribed."},
        )
        return

    subscriber, starting_sequence = subscription
    try:
        send_json(
            connection,
            {"type": "subscribed", "sequence": starting_sequence},
        )
        connection.settimeout(0.5)
        while True:
            try:
                event = subscriber.get(timeout=0.5)
            except Empty:
                readable, _, _ = select.select([connection], [], [], 0)
                if readable:
                    if not connection.recv(1, socket.MSG_PEEK):
                        break
                    raise ProtocolError(
                        "Controller subscriptions do not accept client data."
                    )
                continue
            send_json(connection, event)
    finally:
        SERVER_STATE.unsubscribe(subscriber)
    print(f"Controller disconnected: {address}")


def handle_camera(connection, address, first_message):
    if not SERVER_STATE.camera_lock.acquire(blocking=False):
        send_json(connection, {"type": "error", "message": "Camera already connected."})
        return

    tracker = GestureTracker()
    print(f"Camera connected: {address}")
    try:
        with mp_hands.Hands(
            max_num_hands=1,
            min_detection_confidence=0.7,
            min_tracking_confidence=0.7,
        ) as hands:
            request = first_message
            while request is not None:
                message_type, encoded_frame, _legacy = request
                if message_type != TYPE_FRAME:
                    raise ProtocolError("Only camera-frame messages are valid here.")

                started = time.perf_counter()
                try:
                    frame = cv2.imdecode(
                        np.frombuffer(encoded_frame, dtype=np.uint8),
                        cv2.IMREAD_COLOR,
                    )
                except cv2.error:
                    frame = None

                if frame is None:
                    current, event = tracker.observe("UNKNOWN")
                    if event is not None:
                        SERVER_STATE.publish(event)
                    landmarks = []
                    send_json(
                        connection,
                        {
                            "gesture": current or "UNKNOWN",
                            "landmarks": landmarks,
                            "sequence": SERVER_STATE.sequence,
                            "processing_ms": round(
                                (time.perf_counter() - started) * 1000, 2
                            ),
                        },
                    )
                else:
                    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    results = hands.process(rgb_frame)
                    gesture = "UNKNOWN"
                    landmarks = []
                    if results.multi_hand_landmarks:
                        landmarks = [
                            (point.x, point.y)
                            for point in results.multi_hand_landmarks[0].landmark
                        ]
                        gesture = classify_gesture(landmarks)

                    current, event = tracker.observe(gesture)
                    if event is not None:
                        SERVER_STATE.publish(event)
                    send_json(
                        connection,
                        {
                            "gesture": current or "UNKNOWN",
                            "landmarks": landmarks,
                            "sequence": SERVER_STATE.sequence,
                            "processing_ms": round(
                                (time.perf_counter() - started) * 1000, 2
                            ),
                        },
                    )

                request = receive_request(connection)
    finally:
        tracker.disconnect()
        SERVER_STATE.publish("UNKNOWN")
        SERVER_STATE.camera_lock.release()
        print(f"Camera disconnected: {address}; published UNKNOWN.")


def handle_client(connection, address):
    try:
        first_header = receive_exact(connection, 4)
        if first_header is None:
            return
        first_message = receive_request(connection, first_header)
        if first_message is None:
            return
        message_type, _payload, _legacy = first_message
        if message_type == TYPE_SUBSCRIBE:
            handle_subscription(connection, address)
        else:
            handle_camera(connection, address, first_message)
    except (ConnectionError, OSError, ProtocolError) as exc:
        print(f"Client {address} disconnected: {exc}")


def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen()
        print(f"Gesture server listening on {HOST}:{PORT}")

        while True:
            connection, address = server.accept()
            thread = threading.Thread(
                target=handle_connection,
                args=(connection, address),
                daemon=True,
            )
            thread.start()


def handle_connection(connection, address):
    with connection:
        handle_client(connection, address)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nGesture server stopped.") 
