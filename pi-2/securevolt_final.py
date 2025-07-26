#!/usr/bin/env python3
import os
import sys
import time
import re
import glob
import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
import threading
from queue import Queue

# ===== Hardware Imports with Error Handling =====
try:
    import lgpio
    from gpiozero import OutputDevice, PWMOutputDevice
    import board
    import busio
    from adafruit_bme280 import basic as adafruit_bme280
    HARDWARE_AVAILABLE = True
except ImportError as e:
    HARDWARE_AVAILABLE = False
    # Initialize logging first for error reporting
    logging.basicConfig(level=logging.WARNING)
    logger = logging.getLogger("SecureVolt")
    logger.warning(f"Hardware components not available: {e}")

# ===== Network Imports =====
import paho.mqtt.client as mqtt
import requests
from requests.auth import HTTPDigestAuth
import paramiko

# ===== Image Processing =====
import cv2
import numpy as np

# ===== Serial Communication =====
import serial

# ===== Logging Setup =====
log_dir = os.path.join(os.path.expanduser("~"), "securevolt_logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "securevolt.log")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            log_file,
            maxBytes=5*1024*1024,  # 5MB
            backupCount=3           # Keep 3 backup logs
        )
    ]
)
logger = logging.getLogger("SecureVolt")

# ===== Configuration =====
class Config:
    # Device Identity
    DEVICE_ID = "SecureVolt_Pi5"
    DEVICE_SERIAL = "b0dcdf97926b6c2e"
    LOCATION_CODE = "UNZA_SUBSTATION"
    
    # Hardware Pins
    ALARM_RELAY_PIN = 40
    BUZZER_PIN = 38
    
    # MQTT Configuration
    MQTT_BROKER = "10.16.1.17"
    MQTT_PORT = 1883
    MQTT_TOPICS = {
        "status": "securevolt/status",
        "alarm": "securevolt/alarm",
        "sensor": "securevolt/sensor",
        "image": "securevolt/image",
        "command": "securevolt/command"
    }
    
    # Camera Configuration
    CAMERA_IP = "192.168.1.64"
    CAMERA_USER = "admin"
    CAMERA_PASS = "Securevolt1"
    CAMERA_TIMEOUT = 10
    SNAPSHOT_URL = f"http://{CAMERA_IP}/ISAPI/Streaming/channels/101/picture"
    
    # Motion Detection
    MOTION_THRESHOLD = 10000
    MIN_CONTOUR_AREA = 1000
    CAPTURE_INTERVAL = 0.5
    SAVE_FOLDER = "/home/zesco/hikvision_captures"
    MAX_STORAGE_HOURS = 2
    
    # Alarm Settings
    ALARM_DURATION = 10
    ALARM_BUZZER_ENABLED = True
    ALARM_BUZZER_FREQ = 2000

    # SIM7600 Configuration
    SERIAL_BAUDRATE = 115200
    COMMAND_DELAY = 0.1

# ===== Environmental Sensor =====
class EnvironmentalSensor:
    def __init__(self):
        self.sensor = None
        if HARDWARE_AVAILABLE:
            self._initialize_sensor()

    def _initialize_sensor(self):
        try:
            i2c = busio.I2C(board.SCL, board.SDA)
            self.sensor = adafruit_bme280.Adafruit_BME280_I2C(i2c)
            self.sensor.sea_level_pressure = 1013.25
            logger.info("[BME280] Sensor initialized successfully")
        except Exception as e:
            logger.error(f"[BME280] Initialization failed: {e}")
            self.sensor = None

    def read(self):
        if not self.sensor:
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

# ===== Hardware Controller =====
class HardwareController:
    def __init__(self):
        self.alarm_active = False
        self.relay = None
        self.buzzer = None
        self.env_sensor = EnvironmentalSensor()
        
        if HARDWARE_AVAILABLE:
            self._initialize_gpio()

    def _initialize_gpio(self):
        try:
            self.relay = OutputDevice(
                Config.ALARM_RELAY_PIN, 
                active_high=True, 
                initial_value=False
            )
            self.buzzer = PWMOutputDevice(
                Config.BUZZER_PIN,
                initial_value=0,
                frequency=Config.ALARM_BUZZER_FREQ
            )
            logger.info("GPIO devices initialized successfully")
        except Exception as e:
            logger.error(f"GPIO initialization failed: {e}")

    def activate_alarm(self, state=True):
        if not HARDWARE_AVAILABLE:
            logger.info(f"Mock alarm {'ACTIVATED' if state else 'DEACTIVATED'}")
            return
            
        try:
            self.relay.value = state
            if state and Config.ALARM_BUZZER_ENABLED:
                self.buzzer.value = 0.5  # 50% duty cycle
                time.sleep(0.1)  # Short beep
                self.buzzer.value = 0
            
            self.alarm_active = state
            logger.info(f"Alarm {'ACTIVATED' if state else 'DEACTIVATED'}")
        except Exception as e:
            logger.error(f"Alarm control error: {e}")

    def cleanup(self):
        if HARDWARE_AVAILABLE:
            try:
                if self.relay:
                    self.relay.close()
                if self.buzzer:
                    self.buzzer.close()
                logger.info("GPIO resources released")
            except Exception as e:
                logger.error(f"GPIO cleanup error: {e}")

# ===== SIM7600 Controller =====
class SIM7600Controller:
    def __init__(self):
        self.ser = None
        self.port = self._detect_port()
        self.last_command_time = time.time()
        
        if not self.port:
            self._list_serial_ports()
            raise RuntimeError("Could not detect SIM7600 port")

        try:
            self.ser = serial.Serial(self.port, Config.SERIAL_BAUDRATE, timeout=1)
            time.sleep(2)  # Allow time for initialization
            logger.info(f"[SIM7600] Connected to {self.port}")
        except Exception as e:
            logger.error(f"[SIM7600] Connection failed: {e}")
            raise

    def _detect_port(self):
        ports = sorted(glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*'))
        for port in ports:
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

    def _list_serial_ports(self):
        logger.info("Available serial ports:")
        for port in sorted(glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*')):
            logger.info(f"  {port}")

    def send_at_command(self, command, expected="OK", timeout=5):
        time.sleep(max(0, Config.COMMAND_DELAY - (time.time() - self.last_command_time)))
        self.ser.write((command + '\r\n').encode())
        self.last_command_time = time.time()

        response = ""
        end_time = time.time() + timeout
        while time.time() < end_time:
            if self.ser.in_waiting:
                line = self.ser.readline().decode(errors='ignore').strip()
                if line:
                    response += line + "\n"
                    if expected in line or "ERROR" in line:
                        break
            time.sleep(0.1)

        if "ERROR" in response:
            raise RuntimeError(f"AT command failed: {response.strip()}")
        return response

    def get_network_status(self):
        response = self.send_at_command("AT+CREG?")
        match = re.search(r'\+CREG: (\d,\d)', response)
        status_codes = {
            '0,1': 'Registered - home network',
            '0,5': 'Registered - roaming',
            '0,2': 'Searching',
            '0,3': 'Denied',
            '0,4': 'Unknown',
        }
        return status_codes.get(match.group(1), "Unknown")

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

# ===== MQTT Client =====
class SecureVoltMQTT:
    def __init__(self):
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, Config.DEVICE_ID)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.connected = False

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            self.connected = True
            client.subscribe(Config.MQTT_TOPICS["command"])
            logger.info("Connected to MQTT broker")
        else:
            logger.error(f"Connection failed with code {reason_code}")

    def _on_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload.decode())
            logger.info(f"Received command: {payload}")
            
            if message.topic == Config.MQTT_TOPICS["command"]:
                if payload.get("command") == "activate_alarm":
                    hardware.activate_alarm(True)
                elif payload.get("command") == "deactivate_alarm":
                    hardware.activate_alarm(False)
        except Exception as e:
            logger.error(f"Command processing error: {e}")

    def connect(self):
        try:
            self.client.connect(Config.MQTT_BROKER, Config.MQTT_PORT, 60)
            self.client.loop_start()
            return True
        except Exception as e:
            logger.error(f"MQTT connection error: {e}")
            return False

    def publish_status(self, status_data):
        payload = {
            "timestamp": datetime.now().isoformat(),
            "device_id": Config.DEVICE_ID,
            "status": status_data
        }
        self.client.publish(
            Config.MQTT_TOPICS["status"],
            json.dumps(payload),
            qos=1
        )

    def publish_alarm_event(self, alarm_state, reason=""):
        payload = {
            "timestamp": datetime.now().isoformat(),
            "device_id": Config.DEVICE_ID,
            "state": alarm_state,
            "reason": reason
        }
        self.client.publish(
            Config.MQTT_TOPICS["alarm"],
            json.dumps(payload),
            qos=1,
            retain=True
        )

    def publish_sensor_data(self, modem_data, env_data, alarm_status, motion_status):
        payload = {
            "modem": modem_data,
            "timestamp": datetime.now().isoformat(),
            "device_info": {
                "serial": Config.DEVICE_SERIAL,
                "location_code": Config.LOCATION_CODE
            },
            "alarm_status": alarm_status,
            "environment": env_data,
            "motion_status": motion_status
        }
        self.client.publish(
            Config.MQTT_TOPICS["sensor"],
            json.dumps(payload),
            qos=0
        )

    def publish_image_alert(self, image_path, metadata):
        try:
            with open(image_path, "rb") as f:
                image_data = f.read()
            
            payload = {
                "timestamp": datetime.now().isoformat(),
                "device_id": Config.DEVICE_ID,
                "image": image_data.hex(),
                "metadata": metadata
            }
            self.client.publish(
                Config.MQTT_TOPICS["image"],
                json.dumps(payload),
                qos=1
            )
            return True
        except Exception as e:
            logger.error(f"Image publish error: {e}")
            return False

    def cleanup(self):
        self.client.loop_stop()
        self.client.disconnect()
        logger.info("MQTT client shutdown")

# ===== Motion Detection =====
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
        os.makedirs(Config.SAVE_FOLDER, exist_ok=True)

    def get_camera_snapshot(self):
        try:
            response = requests.get(
                Config.SNAPSHOT_URL,
                auth=self.auth,
                stream=True,
                timeout=Config.CAMERA_TIMEOUT,
                verify=False
            )
            if response.status_code == 200:
                image_data = bytes()
                for chunk in response.iter_content(1024):
                    image_data += chunk
                return cv2.imdecode(np.frombuffer(image_data, np.uint8), cv2.IMREAD_COLOR)
            logger.error(f"Camera error: {response.status_code}")
        except Exception as e:
            logger.error(f"Camera error: {e}")
        return None

    def detect_motion(self, frame):
        if frame is None:
            return False, 0

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        if self.previous_frame is None:
            self.previous_frame = gray
            return False, 0

        frame_delta = cv2.absdiff(self.previous_frame, gray)
        thresh = cv2.threshold(frame_delta, 25, 255, cv2.THRESH_BINARY)[1]
        thresh = cv2.dilate(thresh, None, iterations=2)
        
        contours, _ = cv2.findContours(thresh.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        motion_found = False
        max_area = 0
        for contour in contours:
            area = cv2.contourArea(contour)
            if area > Config.MIN_CONTOUR_AREA:
                motion_found = True
                if area > max_area:
                    max_area = area

        self.previous_frame = gray
        return motion_found, max_area

    def save_image(self, frame):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{Config.LOCATION_CODE}_{Config.DEVICE_SERIAL}_{timestamp}.jpg"
        path = os.path.join(Config.SAVE_FOLDER, filename)
        
        try:
            cv2.imwrite(path, frame)
            return path
        except Exception as e:
            logger.error(f"Error saving image: {e}")
            return None

    def run_detection(self):
        logger.info("Starting motion detection system")
        
        while True:
            start_time = time.time()

            # Cleanup old files periodically
            if time.time() - self.last_cleanup > 1800:  # Every 30 minutes
                self._cleanup_old_files()
                self.last_cleanup = time.time()

            # Get current frame
            frame = self.get_camera_snapshot()
            if frame is None:
                time.sleep(1)
                continue

            # Detect motion and get object size
            current_motion, area = self.detect_motion(frame)

            if current_motion:
                self.motion_counter += 1
                self.last_motion_time = time.time()

                # Initial motion detection
                if not self.motion_detected and self.motion_counter >= 3:
                    self.motion_detected = True
                    hardware.activate_alarm(True)
                    image_path = self.save_image(frame)
                    if image_path:
                        metadata = {
                            "location": Config.LOCATION_CODE,
                            "reason": "motion_detected",
                            "area": area
                        }
                        mqtt_client.publish_image_alert(image_path, metadata)
            else:
                self.motion_counter = 0

                # Motion ended
                if self.motion_detected and (time.time() - self.last_motion_time > Config.ALARM_DURATION):
                    self.motion_detected = False
                    hardware.activate_alarm(False)

            # Maintain processing rate
            processing_time = time.time() - start_time
            sleep_time = max(0, Config.CAPTURE_INTERVAL - processing_time)
            time.sleep(sleep_time)

    def _cleanup_old_files(self):
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
            logger.error(f"Cleanup error: {e}")

# ===== File Uploader =====
class FileUploader:
    def __init__(self):
        self.ssh = None
        self.sftp = None
        self._connect()

    def _connect(self):
        try:
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh.connect(
                Config.MQTT_BROKER,  # Using same host as MQTT broker
                port=22,
                username="root",
                password="root123",
                timeout=10
            )
            self.sftp = self.ssh.open_sftp()
            logger.info("SFTP connection established")
        except Exception as e:
            logger.error(f"SFTP connection failed: {e}")

    def upload_image(self, image_path, remote_path):
        if not self.sftp:
            self._connect()
            if not self.sftp:
                return False

        try:
            self.sftp.put(image_path, remote_path)
            logger.info(f"Uploaded {image_path} to {remote_path}")
            return True
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            return False

    def close(self):
        if self.sftp:
            self.sftp.close()
        if self.ssh:
            self.ssh.close()

# ===== Main Application =====
class SecureVoltApp:
    def  __init__(self):
        self.hardware = HardwareController()
        self.modem = SIM7600Controller()
        self.mqtt = SecureVoltMQTT()
        self.detector = MotionDetector()
        self.uploader = FileUploader()
        self.running = False

    def run(self):
        self.running = True
        
        # Initialize connections
        if not self.mqtt.connect():
            logger.error("Failed to connect to MQTT broker")
            return

        # Start motion detection in separate thread
        detection_thread = threading.Thread(
            target=self.detector.run_detection,
            daemon=True
        )
        detection_thread.start()

        # Main system monitoring loop
        try:
            while self.running:
                # Publish system status periodically
                status = {
                    "alarm_active": self.hardware.alarm_active,
                    "motion_detected": self.detector.motion_detected,
                    "modem": {
                        "network": self.modem.get_network_status(),
                        "signal": self.modem.get_signal_quality(),
                        "gps": self.modem.get_gps_data()
                    }
                }
                
                env_data = self.hardware.env_sensor.read()
                if env_data and env_data.get("status") == "OK":
                    status["environment"] = env_data
                    self.mqtt.publish_sensor_data(env_data)
                
                self.mqtt.publish_status(status)
                time.sleep(30)
                
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            self.shutdown()

    def shutdown(self):
        self.running = False
        self.hardware.activate_alarm(False)
        self.hardware.cleanup()
        self.modem.close()
        self.mqtt.cleanup()
        self.uploader.close()
        logger.info("SecureVolt shutdown complete")

# ===== Initialization and Startup =====
if __name__ == "__main__":
    # Create log directory if not exists
    log_dir = os.path.join(os.path.expanduser("~"), "securevolt_logs")
    os.makedirs(log_dir, exist_ok=True)

    # Verify hardware components
    try:
        test_sensor = EnvironmentalSensor()
        if test_sensor.sensor:
            print("BME280 Sensor Test:")
            print(f"Temperature: {test_sensor.sensor.temperature:.1f}°C")
            print(f"Humidity: {test_sensor.sensor.humidity:.1f}%")
            print(f"Pressure: {test_sensor.sensor.pressure:.1f}hPa")
        else:
            print("BME280 not available, running without environmental data")
    except Exception as e:
        print(f"Hardware test failed: {e}")

    # Run main application
    app = SecureVoltApp()
    app.run()