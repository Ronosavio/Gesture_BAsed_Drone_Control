import time
import cv2
import mediapipe as mp
from djitellopy import Tello

DRY_RUN = False      # True = only print gestures, drone will NOT fly. Set False to fly.
MIN_BATTERY = 7      # refuse to fly at or below this percent
MAX_TEMP = 75       # refuse to fly at or above this temperature (C)
SETTLE_SECONDS = 4    # ignore gestures this long after takeoff completes
TAKEOFF_RETRY_DELAY = 5   # seconds to wait after a refused takeoff before trying again

# Frames a gesture must be held before it fires. Land needs a longer hold
# so your hand leaving the thumbs-up can't land the drone by accident.
HOLD_FRAMES = {"land": 30}
DEFAULT_HOLD = 20

mp_hands = mp.solutions.hands
mp_draw = mp.solutions.drawing_utils
hands = mp_hands.Hands(max_num_hands=1,
                       min_detection_confidence=0.7,
                       min_tracking_confidence=0.7)

cap = cv2.VideoCapture(0)
if not cap.isOpened():
    raise RuntimeError("Could not open camera 0.")

drone = Tello()
print("Connecting to Tello...")
drone.connect()
print("Connected to Tello.")

battery = drone.get_battery()
temp = drone.get_temperature()
print(f"Battery: {battery}%  Temp: {temp}C")
if not DRY_RUN:
    if battery <= MIN_BATTERY:
        cap.release()
        raise SystemExit(f"Battery {battery}% is at or below {MIN_BATTERY}%. Not flying.")
    if temp >= MAX_TEMP:
        cap.release()
        raise SystemExit(f"Drone is {temp}C (limit {MAX_TEMP}C). "
                         "Power off, pull the battery and let it cool 10-15 min.")

last_takeoff_time = 0
last_takeoff_attempt = 0
running = True


def classify_gesture(lm):
    if len(lm) != 21:
        return "UNKNOWN"

    y = lambda i: 1 - lm[i][1]   # higher value = higher on screen

    def ext(tip, mid):
        return y(tip) > y(mid) + 0.02

    index_e = ext(8, 6)
    middle_e = ext(12, 10)
    ring_e = ext(16, 14)
    little_e = ext(20, 18)

    if (y(4) > y(8) + 0.05 and y(4) > y(5) + 0.05
            and not index_e and not middle_e and not ring_e and not little_e):
        return "takeoff"
    elif not index_e and middle_e and ring_e and little_e:
        return "land"
    elif index_e and middle_e and ring_e and little_e:
        return "move_up"
    elif not index_e and not middle_e and not ring_e and not little_e:
        return "move_down"
    elif index_e and not middle_e and not ring_e and not little_e:
        return "move_forward"
    elif little_e and not index_e and not middle_e and not ring_e:
        return "move_back"
    elif index_e and middle_e and not ring_e and not little_e:
        return "move_right"
    elif ring_e and little_e and not index_e and not middle_e:
        return "move_left"
    elif index_e and little_e and not ring_e and not middle_e:
        return "rotate_clockwise"
    elif middle_e and ring_e and not index_e and not little_e:
        return "rotate_counter_clockwise"
    return "UNKNOWN"


def execute_command(command):
    global running, last_takeoff_time, last_takeoff_attempt
    print("GESTURE:", command)
    if DRY_RUN:
        return
    try:
        if command == "takeoff":
            if drone.is_flying:
                return
            # don't spam takeoff after a refusal
            if time.time() - last_takeoff_attempt < TAKEOFF_RETRY_DELAY:
                return
            last_takeoff_attempt = time.time()
            try:
                drone.takeoff()
                last_takeoff_time = time.time()
            except Exception as e:
                try:
                    b = drone.get_battery()
                    t = drone.get_temperature()
                except Exception:
                    b, t = "?", "?"
                print(f"Takeoff refused. Battery={b}% Temp={t}C -> {e}")
                print("Check: drone cool? on flat surface? props clear? status light not red/yellow?")
            return
        if not drone.is_flying:
            return   # ignore everything else while on the ground
        if time.time() - last_takeoff_time < SETTLE_SECONDS:
            return   # still settling after takeoff
        if command == "land":
            drone.land()
            running = False
        elif command == "move_up":
            drone.move_up(50)
        elif command == "move_down":
            drone.move_down(50)
        elif command == "move_forward":
            drone.move_forward(50)
        elif command == "move_back":
            drone.move_back(50)
        elif command == "move_right":
            drone.move_right(50)
        elif command == "move_left":
            drone.move_left(50)
        elif command == "rotate_clockwise":
            drone.rotate_clockwise(45)
        elif command == "rotate_counter_clockwise":
            drone.rotate_counter_clockwise(45)
    except Exception as e:
        print("Command failed:", command, "->", e)
        if "Motor stop" in str(e):
            drone.is_flying = False   # motors are off, stop sending move commands
            print("Motors stopped. Power-cycle the drone and let it cool before retrying.")


streak_gesture = None
streak_count = 0
PREVIOUS_GESTURE = None
CONFIRMED_GESTURE = None

try:
    while running and cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        frame = cv2.flip(frame, 1)
        results = hands.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

        if results.multi_hand_landmarks:
            hand = results.multi_hand_landmarks[0]
            mp_draw.draw_landmarks(frame, hand, mp_hands.HAND_CONNECTIONS)
            lm = [(p.x, p.y) for p in hand.landmark]
            gesture = classify_gesture(lm)

            if gesture == "UNKNOWN":
                streak_gesture, streak_count = None, 0
            else:
                if gesture == streak_gesture:
                    streak_count += 1
                else:
                    streak_gesture, streak_count = gesture, 1

                if streak_count >= HOLD_FRAMES.get(gesture, DEFAULT_HOLD):
                    CONFIRMED_GESTURE = gesture
                    if gesture != PREVIOUS_GESTURE:
                        execute_command(gesture)
                        PREVIOUS_GESTURE = gesture
                    streak_gesture, streak_count = None, 0   # drop stale frames
        else:
            streak_gesture, streak_count = None, 0
            CONFIRMED_GESTURE = None
            PREVIOUS_GESTURE = None   # lets you repeat a gesture after dropping your hand

        mode = "DRY RUN" if DRY_RUN else "LIVE"
        cv2.putText(frame, f"[{mode}] Gesture: {CONFIRMED_GESTURE or 'None'}", (10, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        cv2.imshow("Hand Gesture Recognition", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
finally:
    cap.release()
    try:
        if drone.is_flying:
            drone.land()
    except Exception as e:
        print("land on exit failed:", e)
    try:
        drone.end()
    except Exception as e:
        print("drone.end() failed:", e)
    cv2.destroyAllWindows()