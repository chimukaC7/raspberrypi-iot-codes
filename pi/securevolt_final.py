#!/usr/bin/env python3
import serial
import time
import re
import glob
import sys
import json
import paho.mqtt.client as mqtt
from datetime import datetime, timedelta
import requests
import cv2
import numpy as np
import os
from requests.auth import HTTPDigestAuth
import threading
import logging
import paramiko
from queue import Queue
import RPi.GPIO as GPIO
GPIO.setwarnings(False)  # Disable GPIO warnings

# Disable SSL verification warnings
requests.packages.urllib3.disable_warnings()

# Optional BME280 import with fallback
BME280_AVAILABLE = True  
try:
    import board
    import busio
    from adafruit_bme280 import basic as adafruit_bme280
    BME280_AVAILABLE = True
except ImportError:
    print("[WARNING] BME280 libraries not available - proceeding without environmental data")
except Exception as e:
    print(f"[WARNING] BME280 initialization failed: {e} - proceeding without environmental data")

# ======== Logging Setup ========
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("SecureVolt")

# ======== GPIO Setup ========
GPIO.setmode(GPIO.BOARD)  # Use physical pin numbering
ALARM_RELAY_PIN = 40      # Relay Channel 3 (CH3) - Change this if needed

# ==================== CONFIGURATION ====================
class Config:
    # Server Configuration
    HOST = "10.16.1.17"
    SERVER_URL = f"http://{HOST}/api/v1/events"

    # MQTT Configuration
    MQTT_BROKER = HOST
    MQTT_PORT = 1883
    CLIENT_ID = "sim7600/status"
    MQTT_IMAGE_TOPIC = "hikvision/images"

    # SIM7600 Configuration
    SERIAL_BAUDRATE = 115200
    COMMAND_DELAY = 0.1
    DEVICE_SERIAL = "SN77ea47d7b7b1e602"  # Added device serial number

    # Hikvision Camera Configuration
    CAMERA_IP = '192.168.1.64'
    CAMERA_USER = 'admin'
    CAMERA_PASS = 'Securevolt1'
    SNAPSHOT_URL = f"http://{CAMERA_IP}/ISAPI/Streaming/channels/101/picture"

    # Motion Detection Settings
    MOTION_THRESHOLD = 10000
    MIN_CONTOUR_AREA = 500
    CAPTURE_INTERVAL = 0.5
    SAVE_FOLDER = '/home/zesco/hikvision_captures'
    UNZA_CODE = 'UNZA_SUBSTATION'  #  UNZA_SUBSTATION
    MAX_STORAGE_HOURS = 2
    MIN_MOTION_FRAMES = 2
    MOTION_EVENT_TIMEOUT = 30  # Seconds to consider motion as ended

    # Alarm Settings
    ALARM_DURATION = 10  # Seconds to keep alarm on after motion stops
    MIN_ALARM_INTERVAL = 0.5  # Fastest beep interval (closest distance)
    MAX_ALARM_INTERVAL = 2.0  # Slowest beep interval (furthest distance)
    ALARM_DISTANCE_THRESHOLD = 300  # Pixel width threshold for close proximity

    # SFTP Configuration
    SFTP_HOST = HOST
    SFTP_PORT = 22
    SFTP_USER = "root"
    SFTP_PASS = "root123"
    SFTP_REMOTE_DIR = "/var/www/html/AntiVandalismSensorSystem/public/videos/"


# ======== Relay Control Functions ========
def setup_relay():
    GPIO.setup(ALARM_RELAY_PIN, GPIO.OUT)
    GPIO.output(ALARM_RELAY_PIN, GPIO.LOW)
    logger.info("Relay GPIO initialized")

def activate_alarm(state=True):
    GPIO.output(ALARM_RELAY_PIN, GPIO.HIGH if state else GPIO.LOW)
    logger.info(f"Alarm {'ACTIVATED' if state else 'DEACTIVATED'}")

def cleanup_gpio():
    GPIO.cleanup()
    logger.info("GPIO cleanup complete")

# ==================== FILE UPLOADER ====================
class FileUploader:
    def __init__(self):
        self.ssh = None
        self.sftp = None
        self.connect()

    def connect(self):
        try:
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh.connect(
                Config.SFTP_HOST,
                port=Config.SFTP_PORT,
                username=Config.SFTP_USER,
                password=Config.SFTP_PASS,
                timeout=10
            )
            self.sftp = self.ssh.open_sftp()
            logger.info("Successfully connected to SFTP server")
            return True
        except Exception as e:
            logger.error(f"SFTP connection failed: {e}")
            return False

    def ensure_remote_dir(self):
        try:
            self.sftp.stat(Config.SFTP_REMOTE_DIR)
        except IOError:
            try:
                self.sftp.mkdir(Config.SFTP_REMOTE_DIR)
                logger.info(f"Created remote directory: {Config.SFTP_REMOTE_DIR}")
            except Exception as e:
                logger.error(f"Failed to create remote directory: {e}")
                return False
        return True

    def upload_image(self, image_data, filename):
        if not self.sftp:
            if not self.connect():
                return False

        try:
            remote_path = os.path.join(Config.SFTP_REMOTE_DIR, filename)
            with self.sftp.file(remote_path, 'wb') as remote_file:
                remote_file.write(image_data)
            logger.info(f"Successfully uploaded {filename} to {remote_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to upload {filename}: {e}")
            return False

    def close(self):
        if self.sftp:
            self.sftp.close()
        if self.ssh:
            self.ssh.close()


# ==================== MQTT CLIENT ====================
class MQTTClient:
    def __init__(self):
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, Config.CLIENT_ID)
        self.connected = False
        self.client.on_message = self.on_message  # Attach message handler

    def connect(self):
        try:
            self.client.connect(Config.MQTT_BROKER, Config.MQTT_PORT, keepalive=60)
            self.client.loop_start()
            self.connected = True
            self.client.subscribe("sim7600/alarm/control")  # Subscribe to alarm control topic
            logger.info(f"Connected to MQTT broker at {Config.MQTT_BROKER}:{Config.MQTT_PORT}")
            return True
        except Exception as e:
            logger.error(f"MQTT connection failed: {e}")
            self.connected = False
            return False

    def publish(self, topic, payload, retain=False):
        if not self.connected and not self.connect():
            return False

        try:
            if not isinstance(payload, str):
                payload = json.dumps(payload, indent=2) if isinstance(payload, dict) else str(payload)
            self.client.publish(topic, payload, retain=retain)
            logger.debug(f"Published to {topic}")
            return True
        except Exception as e:
            logger.error(f"[MQTT PUBLISH ERROR] {e}")
            self.connected = False
            return False

    def on_message(self, client, userdata, message):
        try:
            payload = message.payload.decode()
            logger.info(f"[MQTT COMMAND] Received on {message.topic}: {payload}")
            if message.topic == "sim7600/alarm/control":
                if payload.strip().lower() == "off":
                    activate_alarm(False)
                elif payload.strip().lower() == "on":
                    activate_alarm(True)
        except Exception as e:
            logger.error(f"[MQTT MESSAGE ERROR] {e}")



# ==================== SIM7600 CONTROLLER ====================
class SIM7600Controller:
    def __init__(self, port=None):
        self.ser = None
        self.port = port or self.detect_port()
        self.last_command_time = time.time()

        if not self.port:
            self.list_serial_ports()
            sys.exit("Could not detect SIM7600 port.")

        try:
            self.ser = serial.Serial(self.port, Config.SERIAL_BAUDRATE, timeout=1)
            time.sleep(2)
            logger.info(f"[SIM7600] Connected to {self.port}")
        except Exception as e:
            logger.error(f"[SIM7600 ERROR] {e}")
            self.list_serial_ports()
            raise

    def detect_port(self):
        ports_to_try = sorted(glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*'))
        for port in ports_to_try:
            try:
                with serial.Serial(port, Config.SERIAL_BAUDRATE, timeout=1) as ser:
                    ser.write(b'AT\r\n')
                    time.sleep(0.5)
                    response = ser.read(ser.in_waiting).decode(errors='ignore')
                    if 'OK' in response:
                        logger.info(f"[SIM7600] Found on {port}")
                        return port
            except (serial.SerialException, OSError):
                continue
        return None

    def list_serial_ports(self):
        logger.info("[PORTS] Available serial ports:")
        for port in sorted(glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*')):
            logger.info(f"  {port}")

    def send_at_command(self, command, expected="OK", timeout=5):
        time.sleep(max(0, Config.COMMAND_DELAY - (time.time() - self.last_command_time)))
        self.ser.write((command + '\r\n').encode())
        self.last_command_time = time.time()

        end_time = time.time() + timeout
        response = ""
        while time.time() < end_time:
            if self.ser.in_waiting:
                line = self.ser.readline().decode(errors='ignore').strip()
                if line:
                    response += line + "\n"
                    if expected in line or "ERROR" in line:
                        break
            time.sleep(0.1)

        if "ERROR" in response:
            raise Exception(f"AT command failed: {response.strip()}")
        return response

    def get_network_status(self):
        response = self.send_at_command("AT+CREG?")
        match = re.search(r'\+CREG: (\d,\d)', response)
        codes = {
            '0,1': 'Registered - home network',
            '0,5': 'Registered - roaming',
            '0,2': 'Searching',
            '0,3': 'Denied',
            '0,4': 'Unknown',
        }
        return codes.get(match.group(1), "Unknown")

    def get_signal_quality(self):
        response = self.send_at_command("AT+CSQ")
        match = re.search(r'\+CSQ: (\d+),', response)
        if not match:
            return {"quality": "Unknown", "dbm": "N/A"}

        rssi = int(match.group(1))
        if rssi == 99:
            return {"quality": "Unknown", "dbm": "N/A"}

        quality = (rssi / 31) * 100
        dbm = -113 + (2 * rssi)
        return {
            "quality": f"{quality:.1f}%",
            "dbm": f"{dbm} dBm",
            "rssi": rssi
        }

    def get_gps_data(self, retries=3):
        for _ in range(retries):
            try:
                response = self.send_at_command("AT+CGPSINFO", timeout=10)
                match = re.search(r'\+CGPSINFO: ([^,]+),([NS]),([^,]+),([EW])', response)
                if match:
                    lat = self._convert_coordinate(match.group(1), match.group(2))
                    lon = self._convert_coordinate(match.group(3), match.group(4))
                    return {
                        "latitude": lat,
                        "longitude": lon,
                        "status": "Fix acquired"
                    }
                time.sleep(5)
            except Exception as e:
                logger.error(f"[GPS ERROR] {e}")
        return {"status": "No fix"}

    def _convert_coordinate(self, raw, direction):
        try:
            if direction in ['N', 'S']:
                degrees = float(raw[:2])
                minutes = float(raw[2:])
            else:
                degrees = float(raw[:3])
                minutes = float(raw[3:])
            decimal = degrees + (minutes / 60)
            return -decimal if direction in ['S', 'W'] else decimal
        except (ValueError, TypeError):
            return None

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
            logger.info("[SIM7600] Serial connection closed")


# ==================== ENVIRONMENTAL SENSOR ====================
class EnvironmentalSensor:
    def __init__(self):
        self.sensor = None
        self.connected = False
        if BME280_AVAILABLE:
            self._init_sensor()

    def _init_sensor(self):
        for _ in range(3):
            try:
                i2c = busio.I2C(board.SCL, board.SDA)
                self.sensor = adafruit_bme280.Adafruit_BME280_I2C(i2c)
                self.sensor.sea_level_pressure = 1013.25
                self.connected = True
                logger.info("[BME280] Sensor initialized")
                return
            except Exception as e:
                logger.error(f"[BME280 ERROR] {e}")
                time.sleep(1)

    def read(self):
        if not self.connected:
            return {"status": "Sensor not connected"}
        try:
            return {
                "temperature": round(self.sensor.temperature, 2),
                "humidity": round(self.sensor.humidity, 2),
                "pressure": round(self.sensor.pressure, 2),
                "altitude": round(self.sensor.altitude, 2),
                "status": "OK"
            }
        except Exception as e:
            return {"status": f"Error: {str(e)}"}


# ==================== MOTION DETECTOR ====================
class MotionDetector:
    def __init__(self):
        self.previous_frame = None
        self.motion_detected = False
        self.motion_counter = 0
        self.last_motion_time = 0
        self.last_capture_time = 0
        self.last_cleanup = time.time()
        self.auth = HTTPDigestAuth(Config.CAMERA_USER, Config.CAMERA_PASS)
        self.event_queue = Queue()
        self.uploader = FileUploader()
        self.alarm_active = False
        self.last_alarm_time = 0
        self.current_alarm_interval = Config.MAX_ALARM_INTERVAL
        os.makedirs(Config.SAVE_FOLDER, exist_ok=True)

        # Motion detection tuning parameters
        self.min_motion_frames = 3  # Minimum consecutive frames with motion to trigger
        self.motion_timeout = 5  # Seconds of no motion before considering it stopped
        self.capture_interval = 1  # Seconds between captures during continuous motion

        # Background subtractor for better motion detection
        self.fgbg = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=16, detectShadows=True)

    def get_camera_snapshot(self):
        try:
            response = requests.get(Config.SNAPSHOT_URL, auth=self.auth, stream=True, timeout=5, verify=False)
            if response.status_code == 200:
                image_data = bytes()
                for chunk in response.iter_content(chunk_size=1024):
                    image_data += chunk
                image_array = np.frombuffer(image_data, dtype=np.uint8)
                return cv2.imdecode(image_array, cv2.IMREAD_COLOR)
            logger.error(f"Snapshot failed with status: {response.status_code}")
        except Exception as e:
            logger.error(f"Error getting snapshot: {e}")
        return None

    def detect_motion(self, current_frame):
        # Convert to grayscale and apply Gaussian blur
        gray = cv2.cvtColor(current_frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        # Initialize background if first frame
        if self.previous_frame is None:
            self.previous_frame = gray
            return False, 0

        # Compute absolute difference between current and previous frame
        frame_delta = cv2.absdiff(self.previous_frame, gray)
        thresh = cv2.threshold(frame_delta, 25, 255, cv2.THRESH_BINARY)[1]

        # Dilate the thresholded image to fill in holes
        thresh = cv2.dilate(thresh, None, iterations=2)

        # Find contours on thresholded image
        contours, _ = cv2.findContours(thresh.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # Look for significant motion and track largest contour
        motion_found = False
        max_area = 0
        for contour in contours:
            area = cv2.contourArea(contour)
            if area > Config.MIN_CONTOUR_AREA:
                (x, y, w, h) = cv2.boundingRect(contour)
                # Filter small objects and very large objects (likely camera artifacts)
                if 50 < w < 500 and 50 < h < 500:
                    motion_found = True
                    if area > max_area:
                        max_area = area
                        largest_width = w

        self.previous_frame = gray
        return motion_found, largest_width if motion_found else 0

    def control_alarm(self, motion_detected, object_width=0):
        now = time.time()
        
        if motion_detected:
            # Calculate alarm interval based on object size (closer = faster beeping)
            if object_width > 0:
                # Scale the alarm interval based on object width (closer objects appear larger)
                normalized_width = min(object_width, Config.ALARM_DISTANCE_THRESHOLD)
                ratio = normalized_width / Config.ALARM_DISTANCE_THRESHOLD
                self.current_alarm_interval = Config.MAX_ALARM_INTERVAL - (
                    (Config.MAX_ALARM_INTERVAL - Config.MIN_ALARM_INTERVAL) * ratio
                )
            
            # If alarm isn't active or it's time for the next beep
            if not self.alarm_active or (now - self.last_alarm_time) >= self.current_alarm_interval:
                activate_alarm(True)  # Turn on alarm
                self.alarm_active = True
                self.last_alarm_time = now
                logger.info(f"Alarm activated (interval: {self.current_alarm_interval:.2f}s)")
        else:
            # If motion stopped but alarm is still active
            if self.alarm_active and (now - self.last_alarm_time) >= Config.ALARM_DURATION:
                activate_alarm(False)  # Turn off alarm
                self.alarm_active = False
                logger.info("Alarm deactivated (motion ended)")

    def save_and_upload_image(self, frame):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f'{Config.UNZA_CODE}_{Config.DEVICE_SERIAL}_motion_{timestamp}.jpg'  # Updated with UNZA_CODE and DEVICE_SERIAL

        # Draw timestamp on image
        cv2.putText(frame, timestamp, (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)

        # Save image to memory buffer
        _, buffer = cv2.imencode('.jpg', frame)
        image_data = buffer.tobytes()

        # Save locally
        local_path = os.path.join(Config.SAVE_FOLDER, filename)
        with open(local_path, 'wb') as f:
            f.write(image_data)
        logger.info(f"Saved motion capture: {local_path}")

        # Upload to remote server
        if self.uploader.upload_image(image_data, filename):
            try:
                os.remove(local_path)
                logger.info(f"Removed local file after successful upload: {local_path}")
            except Exception as e:
                logger.error(f"Failed to remove local file {local_path}: {e}")

        return local_path

    def cleanup_old_files(self):
        now = time.time()
        cutoff = now - (Config.MAX_STORAGE_HOURS * 3600)

        try:
            for filename in os.listdir(Config.SAVE_FOLDER):
                filepath = os.path.join(Config.SAVE_FOLDER, filename)
                if os.path.isfile(filepath):
                    file_time = os.path.getmtime(filepath)
                    if file_time < cutoff:
                        os.remove(filepath)
                        logger.info(f"Removed old file: {filename}")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

    def run(self):
        logger.info("Starting enhanced motion detection with alarm system...")
        logger.info(f"Settings: Min Area={Config.MIN_CONTOUR_AREA}, Interval={Config.CAPTURE_INTERVAL}s")

        while True:
            start_time = time.time()

            # Cleanup old files periodically
            if time.time() - self.last_cleanup > 1800:  # Every 30 minutes
                self.cleanup_old_files()
                self.last_cleanup = time.time()

            # Get current frame
            frame = self.get_camera_snapshot()
            if frame is None:
                time.sleep(1)
                continue

            # Detect motion and get object size
            current_motion, object_width = self.detect_motion(frame)

            # Control alarm based on motion and proximity
            self.control_alarm(current_motion, object_width)

            if current_motion:
                self.motion_counter += 1
                self.last_motion_time = time.time()

                # Initial motion detection
                if not self.motion_detected and self.motion_counter >= self.min_motion_frames:
                    self.motion_detected = True
                    logger.info("🚨 MOTION DETECTED - Starting continuous capture")
                    self.event_queue.put({
                        "type": "motion_start",
                        "timestamp": datetime.now().isoformat(),
                        "object_width": object_width
                    })

                # Continuous capture while motion is detected
                if self.motion_detected and (time.time() - self.last_capture_time >= self.capture_interval):
                    image_path = self.save_and_upload_image(frame)
                    self.event_queue.put({
                        "type": "motion_capture",
                        "timestamp": datetime.now().isoformat(),
                        "image_path": image_path,
                        "device_serial": Config.DEVICE_SERIAL,
                        "location_code": Config.UNZA_CODE,
                        "object_width": object_width
                    })
                    self.last_capture_time = time.time()
            else:
                self.motion_counter = 0

                # Motion ended
                if self.motion_detected and (time.time() - self.last_motion_time > self.motion_timeout):
                    self.motion_detected = False
                    logger.info("Motion ended")
                    self.event_queue.put({
                        "type": "motion_end",
                        "timestamp": datetime.now().isoformat(),
                        "device_serial": Config.DEVICE_SERIAL,
                        "location_code": Config.UNZA_CODE
                    })

            # Maintain processing rate
            processing_time = time.time() - start_time
            sleep_time = max(0, Config.CAPTURE_INTERVAL - processing_time)
            time.sleep(sleep_time)


# ==================== MAIN APPLICATION ====================
class Application:
    def __init__(self):
        setup_relay()  # Initialize GPIO for relay control
        self.mqtt_client = MQTTClient()
        self.modem = SIM7600Controller()
        self.env_sensor = EnvironmentalSensor()
        self.motion_detector = MotionDetector()
        self.last_status_time = time.time()
        self.status_interval = 30  # Seconds between status updates

    def collect_system_data(self):
        data = {
            "modem": {
                "network": self.modem.get_network_status(),
                "signal": self.modem.get_signal_quality(),
                "gps": self.modem.get_gps_data()
            },
            "timestamp": datetime.now().isoformat(),
            "device_info": {
                "serial": Config.DEVICE_SERIAL,
                "location_code": Config.UNZA_CODE
            },
            "alarm_status": self.motion_detector.alarm_active
        }

        # Only include environmental data if sensor is connected
        env_data = self.env_sensor.read()
        if env_data["status"] != "Sensor not connected":
            data["environment"] = env_data

        return data

    def process_motion_events(self):
        while not self.motion_detector.event_queue.empty():
            event = self.motion_detector.event_queue.get()
            data = self.collect_system_data()
            data.update({
                "motion_event": event,
                "event_type": "motion_detection"
            })
            self.mqtt_client.publish(Config.CLIENT_ID, data)

    def run(self):
        # Start motion detection in separate thread
        motion_thread = threading.Thread(target=self.motion_detector.run, daemon=True)
        motion_thread.start()

        try:
            while True:
                current_time = time.time()

                # Process any motion events first
                self.process_motion_events()

                # Send regular status updates
                if current_time - self.last_status_time >= self.status_interval:
                    data = self.collect_system_data()
                    data["motion_status"] = {
                        "active": self.motion_detector.motion_detected,
                        "last_update": self.motion_detector.last_motion_time,
                        "alarm_active": self.motion_detector.alarm_active,
                        "current_alarm_interval": self.motion_detector.current_alarm_interval
                    }
                    self.mqtt_client.publish(Config.CLIENT_ID, data)
                    self.last_status_time = current_time

                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("\nShutting down...")
        finally:
            activate_alarm(False)  # Ensure alarm is turned off
            self.modem.close()
            if self.mqtt_client.connected:
                self.mqtt_client.client.disconnect()
            self.motion_detector.uploader.close()
            cleanup_gpio()


if __name__ == "__main__":
    app = Application()
    app.run()