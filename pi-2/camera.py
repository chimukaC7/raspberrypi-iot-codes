import cv2
import os
import time
from datetime import datetime

# RTSP camera stream
rtsp_url = "rtsp://securevolt1:Securevolt1@10.42.0.132:554/stream1"

# Save directory
save_dir = "/home/zesco/hikvision_captures"
os.makedirs(save_dir, exist_ok=True)  # Create folder if it doesn't exist

cap = cv2.VideoCapture(rtsp_url)

if not cap.isOpened():
    print("Failed to connect to camera")
    exit()

# Read initial frames
ret, frame1 = cap.read()
ret, frame2 = cap.read()

motion_cooldown = 3  # seconds between saves
last_capture_time = 0

while cap.isOpened():
    # Frame differencing for motion detection
    diff = cv2.absdiff(frame1, frame2)
    gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5,5), 0)
    _, thresh = cv2.threshold(blur, 20, 255, cv2.THRESH_BINARY)
    dilated = cv2.dilate(thresh, None, iterations=3)
    contours, _ = cv2.findContours(dilated, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

    motion_detected = False

    for contour in contours:
        if cv2.contourArea(contour) < 1000:  # Ignore small movements
            continue
        (x, y, w, h) = cv2.boundingRect(contour)
        cv2.rectangle(frame1, (x, y), (x+w, y+h), (0, 255, 0), 2)  # <-- FIXED: added closing parenthesis
        motion_detected = True

    # Save image if motion detected (with cooldown)
    if motion_detected and (time.time() - last_capture_time > motion_cooldown):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(save_dir, f"motion_{timestamp}.jpg")
        cv2.imwrite(filename, frame1)
        print(f"[INFO] Motion detected! Saved: {filename}")
        last_capture_time = time.time()

    # Show feed (optional - comment out if headless)
    cv2.imshow("Motion Detection", frame1)

    # Shift frames
    frame1 = frame2
    ret, frame2 = cap.read()
    if not ret:
        print("Failed to grab frame")
        break

    # Exit on 'q'
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
