import cv2
import mediapipe as mp
import numpy as np
import time
from collections import deque
from djitellopy import Tello
# from opencv-python import cv2



# Initialize Mediapipe Hand Tracking
mp_hands = mp.solutions.hands
mp_draw = mp.solutions.drawing_utils
hands = mp_hands.Hands(min_detection_confidence=0.7, min_tracking_confidence=0.7)

# OpenCV Video Capture
cap = cv2.VideoCapture(0)

# Stability Parameters
STABILITY_THRESHOLD = 10  # Number of frames a gesture must be stable
GESTURE_HISTORY = deque(maxlen=STABILITY_THRESHOLD)
CONFIRMED_GESTURE = None
PREVIOUS_GESTURE = None  # Store last executed gesture
CONFIDENCE_THRESHOLD = 0.7  # Confidence threshold for stable detection





# Initialize Tello Drone
# drone = Tello()
# drone.connect()
# drone.streamon()

# print(f"Battery: {drone.get_battery()}%")  # Check battery level4
# take_off=0



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
    if thumb_tip > index_tip  and not index_extended and not middle_extended and not ring_extended and not little_extended:
        return "takeoff"

    # Middle, ring and little fingure extended, others folded
    elif not index_extended and middle_extended and ring_extended and little_extended :
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

    #  Little Fingure extended, others folded
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

    #  Middle and ring fingure extended, others folded
    elif ring_extended and  middle_extended and not index_extended and not little_extended        :
        return "rotate_counter_clockwise"   


    # Default Case
    else:
        return "UNKNOWN"















# def execute_command(command):
#     global running
#     global take_off
#     if command=="takeoff" and take_off==0:
#         take_off=1
#     elif command=="takeoff" and take_off==1:
#         command="move_down"

#     if command == "takeoff":
#         drone.takeoff()
#     elif command == "land":
#         drone.land()
#         running=False
#     elif command == "move_up":
#         drone.move_up(50)
#     elif command == "move_down":
#         drone.move_down(50)
#     elif command == "move_forward":
#         drone.move_forward(50)
#     elif command == "move_back":
#         drone.move_back(50)
#     elif command == "move_right":
#         drone.move_right(50)
#     elif command == "move_left":
#         drone.move_left(50)
#     elif command == "rotate_clockwise":
#         drone.rotate_clockwise(45)
#     elif command == "rotate_counter_clockwise":
#         drone.rotate_counter_clockwise(45)


running=True

while running and  cap.isOpened():
    ret, frame = cap.read()
    if not ret:
        break
    frame = cv2.flip(frame, 1)
    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    results = hands.process(rgb_frame)
    gesture = "UNKNOWN"
    if results.multi_hand_landmarks:
        for hand_landmarks in results.multi_hand_landmarks:
            mp_draw.draw_landmarks(frame, hand_landmarks, mp_hands.HAND_CONNECTIONS)
            landmarks = [(lm.x, lm.y) for lm in hand_landmarks.landmark]
            gesture = classify_gesture(landmarks)
            GESTURE_HISTORY.append(gesture)
            if GESTURE_HISTORY.count(gesture) == STABILITY_THRESHOLD:
                CONFIRMED_GESTURE = gesture
    else:
        if CONFIRMED_GESTURE and CONFIRMED_GESTURE != PREVIOUS_GESTURE:
            # execute_command(CONFIRMED_GESTURE)
            print(f"The command is : {CONFIRMED_GESTURE}")
            PREVIOUS_GESTURE = CONFIRMED_GESTURE
        CONFIRMED_GESTURE = None
    cv2.putText(frame, f"Gesture: {CONFIRMED_GESTURE or 'None'}", (10, 50),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
    cv2.imshow("Hand Gesture Recognition", frame)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break
cap.release()
# drone.streamoff()
# drone.end()
cv2.destroyAllWindows()
