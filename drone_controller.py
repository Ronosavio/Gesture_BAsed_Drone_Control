"""Windows-side Tello controller for sequenced gesture-server events."""

import argparse
import socket
import sys
import time

from Docker.gesture_protocol import (
    TYPE_SUBSCRIBE,
    encode_message,
    receive_json,
)


SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5000
SOCKET_TIMEOUT_SECONDS = 10

DRY_RUN = False
FLIGHT_COMMANDS_ENABLED = True

MIN_BATTERY = 7
MAX_TEMP = 100
SETTLE_SECONDS = 4
TAKEOFF_RETRY_DELAY = 5
MOVEMENT_COOLDOWN_SECONDS = 1.0
HOLD_FRAMES = {"land": 30}
DEFAULT_HOLD = 20

GESTURES = {
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
MOVEMENT_COMMANDS = GESTURES - {"takeoff", "land"}

COMMANDS = {
    "takeoff": ("takeoff", ()),
    "land": ("land", ()),
    "move_up": ("move_up", (50,)),
    "move_down": ("move_down", (50,)),
    "move_forward": ("move_forward", (50,)),
    "move_back": ("move_back", (50,)),
    "move_right": ("move_right", (50,)),
    "move_left": ("move_left", (50,)),
    "rotate_clockwise": ("rotate_clockwise", (45,)),
    "rotate_counter_clockwise": ("rotate_counter_clockwise", (45,)),
}


class ControllerEventGate:
    """Reject duplicate or stale sequenced events, including across reconnects."""

    def __init__(self, last_sequence=0):
        self.last_sequence = last_sequence

    def accept(self, event):
        if not isinstance(event, dict) or event.get("type") != "event":
            raise ValueError("Invalid gesture event.")
        sequence = event.get("sequence")
        gesture = event.get("gesture")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
            or gesture not in GESTURES | {"UNKNOWN"}
        ):
            raise ValueError("Gesture event has invalid fields.")
        if sequence <= self.last_sequence:
            return None
        self.last_sequence = sequence
        return gesture


class DroneController:
    def __init__(self, drone, clock=time.monotonic):
        self.drone = drone
        self.clock = clock
        self.last_takeoff_attempt = float("-inf")
        self.last_takeoff_time = float("-inf")
        self.last_movement_time = float("-inf")
        self.previous_gesture = None
        self.running = True

    def _flight_commands_allowed(self):
        return not DRY_RUN and FLIGHT_COMMANDS_ENABLED

    def _send_or_report(self, command):
        method_name, arguments = COMMANDS[command]
        if not self._flight_commands_allowed():
            print(f"DRY_RUN: would call drone.{method_name}{arguments}")
            return False
        getattr(self.drone, method_name)(*arguments)
        return True

    def _takeoff_preflight(self):
        try:
            battery = self.drone.get_battery()
            temperature = self.drone.get_temperature()
        except Exception as exc:
            print(f"Takeoff blocked: could not read battery/temperature: {exc}")
            return False

        print(f"Battery: {battery}%  Temp: {temperature}C")
        if battery <= MIN_BATTERY:
            print(f"Takeoff blocked: battery must be above {MIN_BATTERY}%.")
            return False
        if temperature >= MAX_TEMP:
            print(f"Takeoff blocked: temperature must be below {MAX_TEMP}C.")
            return False
        return True

    def execute_command(self, command):
        if command == "UNKNOWN":
            self.previous_gesture = None
            return False
        if command not in GESTURES:
            print(f"Ignoring unsupported gesture: {command!r}")
            return False
        if command == self.previous_gesture:
            print(f"Ignoring repeated gesture: {command}")
            return False
        self.previous_gesture = command
        print(f"GESTURE: {command}")

        now = self.clock()
        if command == "takeoff":
            if self.drone.is_flying:
                print("Takeoff ignored: drone is already flying.")
                return False
            if now - self.last_takeoff_attempt < TAKEOFF_RETRY_DELAY:
                print("Takeoff ignored: retry cooldown is active.")
                return False
            self.last_takeoff_attempt = now
            if not self._takeoff_preflight():
                return False
            sent = self._send_or_report(command)
            if not sent:
                self.last_takeoff_time = now
            else:
                self.last_takeoff_time = self.clock()
            return sent

        if command in MOVEMENT_COMMANDS:
            if not self.drone.is_flying:
                print(f"{command} ignored: drone is not flying.")
                return False
            if now - self.last_takeoff_time < SETTLE_SECONDS:
                print(f"{command} ignored: takeoff settling period is active.")
                return False
            if now - self.last_movement_time < MOVEMENT_COOLDOWN_SECONDS:
                print(f"{command} ignored: movement cooldown is active.")
                return False
            sent = self._send_or_report(command)
            self.last_movement_time = now
            return sent

        if command == "land":
            if not self.drone.is_flying:
                print("Land ignored: drone is not flying.")
                return False
            sent = self._send_or_report(command)
            if sent:
                self.running = False
            return sent

        return False

    def shutdown(self, reason, attempt_landing=True):
        if reason == "server_disconnect":
            print(
                "Gesture server disconnected. No landing command was sent. "
                "Use the DJI Tello app/controller to land manually."
            )
            return
        if attempt_landing and self.drone.is_flying:
            if self._flight_commands_allowed():
                try:
                    self.drone.land()
                except Exception as exc:
                    print(f"Shutdown landing attempt failed: {exc}")
            else:
                print("DRY_RUN: would call drone.land() during shutdown")


def run_subscription(controller, host=SERVER_HOST, port=SERVER_PORT):
    """Connect once; a dropped subscription is never automatically resumed."""
    with socket.create_connection(
        (host, port), timeout=SOCKET_TIMEOUT_SECONDS
    ) as connection:
        connection.settimeout(None)
        request = b'{"subscribe":"gestures"}'
        connection.sendall(encode_message(TYPE_SUBSCRIBE, request))

        acknowledgement = receive_json(connection)
        if (
            not isinstance(acknowledgement, dict)
            or acknowledgement.get("type") != "subscribed"
            or isinstance(acknowledgement.get("sequence"), bool)
            or not isinstance(acknowledgement.get("sequence"), int)
            or acknowledgement.get("sequence") < 0
        ):
            message = (
                acknowledgement.get("message", "subscription rejected")
                if isinstance(acknowledgement, dict)
                else "invalid subscription response"
            )
            raise ConnectionError(message)

        gate = ControllerEventGate(acknowledgement["sequence"])
        print(
            "Subscribed to gesture events at sequence "
            f"{gate.last_sequence}; DRY_RUN={DRY_RUN}."
        )
        while True:
            event = receive_json(connection)
            gesture = gate.accept(event)
            if gesture is not None:
                controller.execute_command(gesture)
                if not controller.running:
                    return


def main():
    parser = argparse.ArgumentParser(description="Control Tello from gesture events.")
    parser.add_argument("--host", default=SERVER_HOST)
    parser.add_argument("--port", type=int, default=SERVER_PORT)
    args = parser.parse_args()

    drone = None
    controller = None
    shutdown_reason = "normal"
    return_code = 0
    try:
        from djitellopy import Tello

        drone = Tello()
        print("Connecting to Tello (flight commands remain disabled)...")
        drone.connect()
        print("Connected to Tello.")
        controller = DroneController(drone)
        run_subscription(controller, args.host, args.port)
    except KeyboardInterrupt:
        shutdown_reason = "user_shutdown"
        print("\nController stopped by user.")
    except (ConnectionError, OSError, ValueError) as exc:
        shutdown_reason = "server_disconnect"
        print(f"Gesture subscription stopped: {exc}")
    except Exception as exc:
        shutdown_reason = "error"
        print(f"Controller failed: {exc}")
        return_code = 1
    finally:
        if controller is not None:
            controller.shutdown(
                shutdown_reason,
                attempt_landing=shutdown_reason != "server_disconnect",
            )
        if drone is not None:
            try:
                drone.end()
                print("Tello connection closed.")
            except Exception as exc:
                print(f"Tello cleanup failed: {exc}")

    return return_code


if __name__ == "__main__":
    sys.exit(main())
