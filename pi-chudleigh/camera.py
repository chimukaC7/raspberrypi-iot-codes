import cv2
import os
import lgpio
import time
from datetime import datetime

#====================================================
# GPIO mapping (BCM numbers, not BOARD)
# BOARD 37 = GPIO 26
# BOARD 38 = GPIO 20
# BOARD 40 = GPIO 21

RELAY_PINS = [21, 20, 26]  # BCM GPIOs for CH3, CH2, CH1

# Open a handle to the GPIO chip
h = lgpio.gpiochip_open(0)

# Set each relay pin as output
for pin in RELAY_PINS:
    lgpio.gpio_claim_output(h, pin, 0)
#====================================================

# RTSP camera stream
rtsp_url = "rtsp://securevolt1:Securevolt1@10.42.0.45:554/stream1"

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

if not ret:
    print("Failed to read initial frames")
    cap.release()
    exit()

motion_cooldown = 3  # seconds between saves
last_capture_time = 0

while cap.isOpened():
    # Frame differencing for motion detection
    diff = cv2.absdiff(frame1, frame2)
    gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, thresh = cv2.threshold(blur, 20, 255, cv2.THRESH_BINARY)
    dilated = cv2.dilate(thresh, None, iterations=3)
    contours, _ = cv2.findContours(dilated, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

    motion_detected = False

    for contour in contours:
        if cv2.contourArea(contour) < 1000:
            continue
        motion_detected = True
        break  # Stop checking after first large motion

    # Save image if motion detected (with cooldown)
    if motion_detected and (time.time() - last_capture_time > motion_cooldown):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(save_dir, f"motion_{timestamp}.jpg")
        cv2.imwrite(filename, frame1)
        print(f"[INFO] Motion detected! Saved: {filename}")
        last_capture_time = time.time()

        try:
            while True:
                # CH3
                lgpio.gpio_write(h, 21, 1)
                time.sleep(1)
                lgpio.gpio_write(h, 21, 0)

                # CH2
                lgpio.gpio_write(h, 20, 1)
                time.sleep(1)
                lgpio.gpio_write(h, 20, 0)

                # CH1
                lgpio.gpio_write(h, 26, 1)
                time.sleep(1)
                lgpio.gpio_write(h, 26, 0)

        except KeyboardInterrupt:
            print("Program interrupted by user.")
        except Exception as e:
            print(f"An error occurred: {e}")
        finally:
            # Set all relays off
            for pin in RELAY_PINS:
                lgpio.gpio_write(h, pin, 0)

            # Close handle
            lgpio.gpiochip_close(h)
            print("GPIO cleaned up.")


    # Shift frames
    frame1 = frame2
    ret, frame2 = cap.read()
    if not ret:
        print("Failed to grab next frame")
        break

    # Optional: small sleep to reduce CPU usage
    time.sleep(0.1)

cap.release()


