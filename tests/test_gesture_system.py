import json
import socket
import struct
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = ROOT / "Docker"
sys.path.insert(0, str(DOCKER_DIR))

import drone_controller
import gesture_server
from Docker.gesture_protocol import (
    MAGIC,
    TYPE_FRAME,
    TYPE_SUBSCRIBE,
    ProtocolError,
    encode_json,
    receive_json,
    receive_request,
)


class FakeDrone:
    def __init__(self, battery=80, temperature=30, flying=False):
        self.battery = battery
        self.temperature = temperature
        self.is_flying = flying
        self.calls = []

    def get_battery(self):
        return self.battery

    def get_temperature(self):
        return self.temperature

    def connect(self):
        self.calls.append(("connect", ()))

    def __getattr__(self, name):
        if name in drone_controller.COMMANDS:
            def record(*args):
                self.calls.append((name, args))

            return record
        raise AttributeError(name)

    def end(self):
        self.calls.append(("end", ()))


class FakeClock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


class ProtocolTests(unittest.TestCase):
    def test_typed_and_legacy_frames_are_distinguished_from_subscriptions(self):
        for request, expected_type, legacy in (
            (
                MAGIC + TYPE_FRAME + struct.pack("!I", 3) + b"jpg",
                TYPE_FRAME,
                False,
            ),
            (struct.pack("!I", 3) + b"jpg", TYPE_FRAME, True),
            (
                MAGIC
                + TYPE_SUBSCRIBE
                + struct.pack("!I", len(b'{"subscribe":"gestures"}'))
                + b'{"subscribe":"gestures"}',
                TYPE_SUBSCRIBE,
                False,
            ),
        ):
            with self.subTest(expected_type=expected_type, legacy=legacy):
                receiver, sender = socket.socketpair()
                try:
                    sender.sendall(request)
                    result = receive_request(receiver)
                    self.assertEqual(result[0], expected_type)
                    self.assertEqual(result[2], legacy)
                finally:
                    receiver.close()
                    sender.close()

    def test_malformed_typed_messages_are_rejected(self):
        receiver, sender = socket.socketpair()
        try:
            sender.sendall(MAGIC + b"X" + struct.pack("!I", 1) + b"x")
            with self.assertRaises(ProtocolError):
                receive_request(receiver)
        finally:
            receiver.close()
            sender.close()

        receiver, sender = socket.socketpair()
        try:
            sender.sendall(
                MAGIC
                + TYPE_SUBSCRIBE
                + struct.pack("!I", 2)
                + b"{}"
            )
            with self.assertRaises(ProtocolError):
                receive_request(receiver)
        finally:
            receiver.close()
            sender.close()


class ServerStateTests(unittest.TestCase):
    def setUp(self):
        self.state = gesture_server.ServerState()

    def test_all_gestures_emit_once_after_configured_hold(self):
        for gesture in gesture_server.VALID_GESTURES:
            with self.subTest(gesture=gesture):
                tracker = gesture_server.GestureTracker()
                required = gesture_server.HOLD_FRAMES.get(
                    gesture, gesture_server.DEFAULT_HOLD_FRAMES
                )
                events = []
                for _ in range(required):
                    _current, event = tracker.observe(gesture)
                    if event is not None:
                        events.append(event)
                _current, event = tracker.observe(gesture)
                self.assertEqual(events, [gesture])
                self.assertIsNone(event)

    def test_unknown_resets_hold_and_disconnect_publishes_unknown(self):
        tracker = gesture_server.GestureTracker()
        for _ in range(gesture_server.DEFAULT_HOLD_FRAMES - 1):
            _current, event = tracker.observe("move_up")
            self.assertIsNone(event)
        current, event = tracker.observe("UNKNOWN")
        self.assertIsNone(current)
        self.assertIsNone(event)

        self.state.publish("UNKNOWN")
        self.assertEqual(self.state.sequence, 1)

    def test_only_one_controller_can_subscribe_at_a_time(self):
        barrier = threading.Barrier(3)
        results = []

        def subscribe():
            barrier.wait()
            results.append(self.state.subscribe())

        threads = [threading.Thread(target=subscribe) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)

        accepted = [item for item in results if item is not None]
        self.assertEqual(len(accepted), 1)
        self.assertIsNone(self.state.subscribe())
        self.state.unsubscribe(accepted[0][0])
        self.assertIsNotNone(self.state.subscribe())

    def test_subscription_handler_rejects_second_controller_and_releases_on_drop(self):
        with mock.patch.object(gesture_server, "SERVER_STATE", self.state):
            server_one, client_one = socket.socketpair()
            thread = threading.Thread(
                target=gesture_server.handle_subscription,
                args=(server_one, ("local", 1)),
            )
            thread.start()
            try:
                acknowledgement = receive_json(client_one)
                self.assertEqual(acknowledgement["type"], "subscribed")

                server_two, client_two = socket.socketpair()
                try:
                    gesture_server.handle_subscription(
                        server_two, ("local", 2)
                    )
                    rejection = receive_json(client_two)
                    self.assertEqual(rejection["type"], "error")
                finally:
                    server_two.close()
                    client_two.close()
            finally:
                client_one.close()
                thread.join(timeout=3)
                server_one.close()

            self.assertFalse(thread.is_alive())
            self.assertIsNotNone(self.state.subscribe())

    def test_camera_disconnect_publishes_unknown_and_clears_state(self):
        subscriber, start_sequence = self.state.subscribe()
        self.assertEqual(start_sequence, 0)
        server_socket, client_socket = socket.socketpair()

        class NoHands:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch.object(gesture_server, "SERVER_STATE", self.state), mock.patch.object(
            gesture_server.mp_hands, "Hands", return_value=NoHands()
        ):
            thread = threading.Thread(
                target=gesture_server.handle_camera,
                args=(
                    server_socket,
                    ("local", 3),
                    (TYPE_FRAME, b"not-a-jpeg", False),
                ),
            )
            thread.start()
            try:
                response = receive_json(client_socket)
                self.assertEqual(response["gesture"], "UNKNOWN")
                client_socket.close()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                event = subscriber.get(timeout=1)
                self.assertEqual(event["gesture"], "UNKNOWN")
                self.assertEqual(event["sequence"], 1)
            finally:
                client_socket.close()
                server_socket.close()

    def test_reconnection_ack_starts_at_current_sequence_without_replay(self):
        self.state.publish("move_left")
        old_subscriber, old_sequence = self.state.subscribe()
        self.assertEqual(old_sequence, 1)
        self.state.unsubscribe(old_subscriber)

        new_subscriber, new_sequence = self.state.subscribe()
        self.assertEqual(new_sequence, 1)
        self.assertTrue(new_subscriber.empty())
        self.state.publish("UNKNOWN")
        event = new_subscriber.get(timeout=1)
        self.assertEqual(event["sequence"], 2)
        self.assertEqual(event["gesture"], "UNKNOWN")


class ControllerTests(unittest.TestCase):
    def test_stale_and_duplicate_events_are_ignored(self):
        gate = drone_controller.ControllerEventGate(last_sequence=12)
        self.assertIsNone(
            gate.accept(
                {"type": "event", "sequence": 12, "gesture": "takeoff"}
            )
        )
        self.assertIsNone(
            gate.accept(
                {"type": "event", "sequence": 11, "gesture": "land"}
            )
        )
        self.assertEqual(
            gate.accept(
                {"type": "event", "sequence": 13, "gesture": "move_up"}
            ),
            "move_up",
        )
        with self.assertRaises(ValueError):
            gate.accept({"type": "event", "sequence": True, "gesture": "takeoff"})

    def test_dry_run_never_calls_any_flight_method(self):
        drone = FakeDrone(flying=False)
        clock = FakeClock()
        controller = drone_controller.DroneController(drone, clock=clock)
        output = StringIO()
        with redirect_stdout(output):
            controller.execute_command("takeoff")
            controller.execute_command("UNKNOWN")
            drone.is_flying = True
            controller.last_takeoff_time = clock.value - 5
            for gesture in sorted(drone_controller.MOVEMENT_COMMANDS):
                controller.execute_command(gesture)
                controller.execute_command("UNKNOWN")
                clock.value += drone_controller.MOVEMENT_COOLDOWN_SECONDS + 0.1
            controller.execute_command("land")

        flight_methods = set(drone_controller.COMMANDS)
        self.assertFalse(any(name in flight_methods for name, _ in drone.calls))
        self.assertIn("DRY_RUN: would call drone.takeoff()", output.getvalue())
        self.assertIn("DRY_RUN: would call drone.land()", output.getvalue())

    def test_takeoff_thresholds_and_movement_gates(self):
        low_battery = FakeDrone(battery=drone_controller.MIN_BATTERY)
        controller = drone_controller.DroneController(low_battery)
        output = StringIO()
        with redirect_stdout(output):
            controller.execute_command("takeoff")
        self.assertIn("battery must be above", output.getvalue())
        self.assertNotIn(("takeoff", ()), low_battery.calls)

        hot_drone = FakeDrone(temperature=drone_controller.MAX_TEMP)
        controller = drone_controller.DroneController(hot_drone)
        with redirect_stdout(output):
            controller.execute_command("takeoff")
        self.assertIn("temperature must be below", output.getvalue())
        self.assertNotIn(("takeoff", ()), hot_drone.calls)

        grounded_drone = FakeDrone(flying=False)
        controller = drone_controller.DroneController(grounded_drone)
        with redirect_stdout(output):
            controller.execute_command("move_forward")
        self.assertIn("drone is not flying", output.getvalue())

        flying_drone = FakeDrone(flying=True)
        clock = FakeClock(value=3)
        controller = drone_controller.DroneController(flying_drone, clock=clock)
        controller.last_takeoff_time = 0
        with redirect_stdout(output):
            controller.execute_command("move_up")
        self.assertIn("settling period", output.getvalue())

    def test_fake_drone_checks_takeoff_cooldown_settling_and_command_args(self):
        drone = FakeDrone()
        clock = FakeClock(value=100)
        controller = drone_controller.DroneController(drone, clock=clock)
        output = StringIO()
        with mock.patch.object(drone_controller, "DRY_RUN", False), mock.patch.object(
            drone_controller, "FLIGHT_COMMANDS_ENABLED", True
        ), redirect_stdout(output):
            self.assertTrue(controller.execute_command("takeoff"))
            self.assertEqual(drone.calls, [("takeoff", ())])
            self.assertFalse(controller.execute_command("takeoff"))
            self.assertEqual(drone.calls, [("takeoff", ())])

            controller.execute_command("UNKNOWN")
            self.assertFalse(controller.execute_command("takeoff"))
            self.assertIn("retry cooldown", output.getvalue())

            drone.is_flying = True
            controller.execute_command("UNKNOWN")
            self.assertFalse(controller.execute_command("move_forward"))
            self.assertIn("settling period", output.getvalue())

            clock.value += drone_controller.SETTLE_SECONDS + 0.1
            controller.execute_command("UNKNOWN")
            self.assertTrue(controller.execute_command("move_forward"))
            self.assertEqual(drone.calls[-1], ("move_forward", (50,)))

    def test_movement_cooldown_and_server_disconnect_do_not_land(self):
        drone = FakeDrone(flying=True)
        clock = FakeClock(value=10)
        controller = drone_controller.DroneController(drone, clock=clock)
        controller.last_takeoff_time = 0
        output = StringIO()
        with redirect_stdout(output):
            controller.execute_command("move_up")
            controller.execute_command("UNKNOWN")
            controller.execute_command("move_down")
        self.assertIn("movement cooldown", output.getvalue())
        controller.shutdown("server_disconnect")
        self.assertNotIn(("land", ()), drone.calls)

    def test_simulated_socket_events_cover_each_gesture_without_reclassification(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        host, port = listener.getsockname()
        gestures = sorted(drone_controller.GESTURES)
        received_request = []

        def serve_events():
            connection, _address = listener.accept()
            with connection:
                received_request.append(receive_request(connection))
                connection.sendall(encode_json({"type": "subscribed", "sequence": 0}))
                for sequence, gesture in enumerate(gestures, start=1):
                    connection.sendall(
                        encode_json(
                            {
                                "type": "event",
                                "sequence": sequence,
                                "gesture": gesture,
                            }
                        )
                    )

        thread = threading.Thread(target=serve_events)
        thread.start()
        drone = FakeDrone()
        controller = drone_controller.DroneController(drone)
        output = StringIO()
        try:
            with redirect_stdout(output):
                with self.assertRaises(ConnectionError):
                    drone_controller.run_subscription(controller, host, port)
        finally:
            thread.join(timeout=3)
            listener.close()

        self.assertFalse(thread.is_alive())
        self.assertEqual(received_request[0][0], TYPE_SUBSCRIBE)
        for gesture in gestures:
            self.assertIn(f"GESTURE: {gesture}", output.getvalue())
        self.assertEqual(drone.calls, [])

    def test_subscription_disconnect_does_not_reconnect(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        host, port = listener.getsockname()
        accepted = []

        def serve_one_connection():
            connection, _address = listener.accept()
            accepted.append(True)
            with connection:
                connection.sendall(
                    encode_json({"type": "subscribed", "sequence": 5})
                )

        thread = threading.Thread(target=serve_one_connection)
        thread.start()
        controller = drone_controller.DroneController(FakeDrone())
        try:
            with self.assertRaises(ConnectionError):
                drone_controller.run_subscription(controller, host, port)
        finally:
            thread.join(timeout=3)
            listener.close()

        time.sleep(0.1)
        self.assertEqual(len(accepted), 1)

    def test_shutdown_disconnect_does_not_send_landing(self):
        drone = FakeDrone(flying=True)
        controller = drone_controller.DroneController(drone)
        output = StringIO()
        with redirect_stdout(output):
            controller.shutdown("server_disconnect")
        self.assertNotIn(("land", ()), drone.calls)
        self.assertIn("Use the DJI Tello app/controller to land manually", output.getvalue())

    def test_main_closes_tello_after_server_disconnect_without_landing(self):
        drone = FakeDrone(flying=True)
        tello_module = mock.Mock(Tello=mock.Mock(return_value=drone))
        output = StringIO()
        with mock.patch.dict(sys.modules, {"djitellopy": tello_module}), mock.patch.object(
            sys, "argv", ["drone_controller.py"]
        ), mock.patch.object(
            drone_controller,
            "run_subscription",
            side_effect=ConnectionError("server disconnected"),
        ), redirect_stdout(output):
            self.assertEqual(drone_controller.main(), 0)

        self.assertIn(("end", ()), drone.calls)
        self.assertNotIn(("land", ()), drone.calls)
        self.assertIn("Tello connection closed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
