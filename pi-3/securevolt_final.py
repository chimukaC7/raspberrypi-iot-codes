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

try:
    import lgpio
    from gpiozero import OutputDevice, PWMOutputDevice
    import board
    import busio
    from adafruit_bme280 import basic as adafruit_bme280
    HARDWARE_AVAILABLE = True
except ImportError as e:
    HARDWARE_AVAILABLE = False
    logging.basicConfig(level=logging.WARNING)
    logger = logging.getLogger("SecureVolt")
    logger.warning(f"Hardware components not available: {e}")

import paho.mqtt.client as mqtt
import requests
from requests.auth import HTTPDigestAuth
import paramiko
import cv2
import numpy as np
import serial

log_dir = os.path.join(os.path.expanduser("~"), "securevolt_logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "securevolt.log")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3)]
)
logger = logging.getLogger("SecureVolt")

# ===== Configuration =====
class Config:
    DEVICE_ID = "SecureVolt_Pi5"
    DEVICE_SERIAL = "b0dcdf97926b6c2e"
    LOCATION_CODE = "LEVY_SUBSTATION"

    ALARM_RELAY_PIN = 40
    BUZZER_PIN = 38

    MQTT_BROKER = "10.16.1.17"
    MQTT_PORT = 1883
    MQTT_TOPICS = {
        "status": "sim7600/status",
        "alarm": "sim7600/alarm",
        "sensor": "sim7600/sensor",
        "image": "sim7600/image",
        "command": "sim7600/command"
    }

    CAMERA_IP = "192.168.1.64"
    CAMERA_USER = "admin"
    CAMERA_PASS = "Securevolt1"
    CAMERA_TIMEOUT = 10
    # SNAPSHOT_URL removed; only RTSP used now

    MIN_CONTOUR_AREA = 1000
    CAPTURE_INTERVAL = 0.5
    SAVE_FOLDER = "/home/zesco/hikvision_captures"
    MAX_STORAGE_HOURS = 2

    ALARM_DURATION = 10
    ALARM_BUZZER_ENABLED = True
    ALARM_BUZZER_FREQ = 2000

    SERIAL_BAUDRATE = 115200
    COMMAND_DELAY = 0.1

    RTSP_URL = "rtsp://securevolt1:Securevolt1@10.42.0.132:554/stream1"
    RTSP_MIN_CONTOUR_AREA = MIN_CONTOUR_AREA
    RTSP_MOTION_COOLDOWN = 3

    # SFTP server configuration
    SFTP_HOST = MQTT_BROKER  # Change if needed
    SFTP_PORT = 22
    SFTP_USER = "root"
    SFTP_PASS = "root123"
    SFTP_UPLOAD_PATH = "/var/www/html/AntiVandalismSensorSystem/public/videos/"  # Folder on SFTP server

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
            self.relay = OutputDevice(Config.ALARM_RELAY_PIN, active_high=True, initial_value=False)
            self.buzzer = PWMOutputDevice(Config.BUZZER_PIN, frequency=Config.ALARM_BUZZER_FREQ)
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
                self.buzzer.value = 0.5
                time.sleep(0.1)
                self.buzzer.value = 0
            self.alarm_active = state
            logger.info(f"Alarm {'ACTIVATED' if state else 'DEACTIVATED'}")
        except Exception as e:
            logger.error(f"Alarm control error: {e}")

    def cleanup(self):
        if HARDWARE_AVAILABLE:
            try:
                if self.relay: self.relay.close()
                if self.buzzer: self.buzzer.close()
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
            time.sleep(2)
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
                    if 'OK' in ser.read(ser.in_waiting).decode(errors='ignore'):
                        logger.info(f"[SIM7600] Found on {port}")
                        return port
            except:
                continue
        return None

    def _list_serial_ports(self):
        logger.info("Available serial ports:")
        for p in sorted(glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*')):
            logger.info(f"  {p}")

    def send_at_command(self, cmd, expected="OK", timeout=5):
        time.sleep(max(0, Config.COMMAND_DELAY - (time.time() - self.last_command_time)))
        self.ser.write((cmd + '\r\n').encode())
        self.last_command_time = time.time()
        resp = ""
        end = time.time() + timeout
        while time.time() < end:
            if self.ser.in_waiting:
                line = self.ser.readline().decode(errors='ignore').strip()
                resp += line + "\n"
                if expected in line or "ERROR" in line:
                    break
            time.sleep(0.1)
        if "ERROR" in resp:
            raise RuntimeError(f"AT command failed: {resp.strip()}")
        return resp

    def get_network_status(self):
        r = self.send_at_command("AT+CREG?")
        m = re.search(r'\+CREG: (\d,\d)', r)
        return {
            '0,1':'Registered - home network','0,5':'Registered - roaming',
            '0,2':'Searching','0,3':'Denied','0,4':'Unknown'
        }.get(m.group(1), "Unknown")

    def get_signal_quality(self):
        r = self.send_at_command("AT+CSQ")
        m = re.search(r'\+CSQ: (\d+),', r)
        if not m or int(m.group(1))==99:
            return {"quality":"Unknown","dbm":"N/A","rssi":None}
        v=int(m.group(1)); return {"quality":f"{(v/31)*100:.1f}%","dbm":f"{-113+2*v} dBm","rssi":v}

    def get_gps_data(self, retries=3):
        for _ in range(retries):
            try:
                r = self.send_at_command("AT+CGPSINFO", timeout=10)
                m = re.search(r'\+CGPSINFO: ([^,]+),([NS]),([^,]+),([EW])', r)
                if m:
                    def conv(raw, dir):
                        d = float(raw[:2] if dir in 'NS' else raw[:3])
                        mpart = float(raw[2:] if dir in 'NS' else raw[3:])
                        d += mpart/60
                        return -d if dir in 'SW' else d
                    return {"latitude":conv(m.group(1),m.group(2)),"longitude":conv(m.group(3),m.group(4)),"status":"Fix acquired"}
            except Exception as e:
                logger.error(f"[GPS ERROR] {e}")
            time.sleep(5)
        return {"status":"No fix"}

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close(); logger.info("[SIM7600] Serial connection closed")

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
            self.ssh.connect(Config.SFTP_HOST, port=Config.SFTP_PORT, username=Config.SFTP_USER, password=Config.SFTP_PASS, timeout=10)
            self.sftp = self.ssh.open_sftp()
            logger.info("SFTP connected")
        except Exception as e:
            logger.error(f"SFTP failed: {e}")
            self.ssh = None
            self.sftp = None
    def upload_image(self, src, dst):
        try:
            if not self.sftp:
                self._connect()
            self.sftp.put(src, dst)
            logger.info(f"Uploaded {src} to {dst}")
            return True
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            return False
    def close(self):
        if self.sftp: self.sftp.close()
        if self.ssh: self.ssh.close()

# ===== MQTT Client =====
class SecureVoltMQTT:
    def __init__(self, hardware_controller):
        self.hardware = hardware_controller
        self.client = mqtt.Client(client_id=Config.DEVICE_ID)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.connected = False

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(Config.MQTT_TOPICS["command"])
            self.connected = True
            logger.info("MQTT connected")
        else:
            logger.error(f"MQTT connect failed: {rc}")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            cmd = payload.get("command")
            if cmd == "activate_alarm": self.hardware.activate_alarm(True)
            elif cmd == "deactivate_alarm": self.hardware.activate_alarm(False)
        except Exception as e:
            logger.error(f"MQTT msg error: {e}")

    def connect(self):
        try:
            self.client.connect(Config.MQTT_BROKER, Config.MQTT_PORT, 60)
            self.client.loop_start()
            return True
        except Exception as e:
            logger.error(f"MQTT err: {e}")
            return False

    def publish_status(self, data):
        self.client.publish(Config.MQTT_TOPICS["sensor"], json.dumps({"timestamp": datetime.now().isoformat(), "device_id": Config.DEVICE_ID, "status": data}), qos=1)
    def publish_alarm_event(self, state, reason=""):
        self.client.publish(Config.MQTT_TOPICS["alarm"], json.dumps({"timestamp": datetime.now().isoformat(), "device_id": Config.DEVICE_ID, "state": state, "reason": reason}), qos=1, retain=True)
    def publish_sensor_data(self, net, env, alarm, motion):
        msg = {"modem": net, "timestamp": datetime.now().isoformat(), "device_info": {"serial": Config.DEVICE_SERIAL, "location_code": Config.LOCATION_CODE}, "alarm_status": alarm, "environment": env, "motion_status": motion}
        self.client.publish(Config.MQTT_TOPICS["status"], json.dumps(msg), qos=0)
    def publish_image_alert(self, sftp_path, metadata):
        try:
            # sftp_path is server file path; just publish metadata
            self.client.publish(Config.MQTT_TOPICS["image"], json.dumps({
                "timestamp": datetime.now().isoformat(),
                "device_id": Config.DEVICE_ID,
                "file_path": sftp_path,
                "metadata": metadata
            }), qos=1)
            return True
        except Exception as e:
            logger.error(f"MQTT img err: {e}")
            return False
    def cleanup(self):
        self.client.loop_stop()
        self.client.disconnect()
        logger.info("MQTT shutdown")

# ===== RTSP Motion Detector (includes upload + MQTT) =====
class RTSPMotionDetector:
    def __init__(self, rtsp_url, save_folder, min_contour_area, motion_cooldown, uploader, mqtt_client, hardware):
        self.rtsp_url = rtsp_url
        self.save_folder = save_folder
        self.min_contour_area = min_contour_area
        self.motion_cooldown = motion_cooldown
        self.uploader = uploader
        self.mqtt_client = mqtt_client
        self.hardware = hardware
        os.makedirs(self.save_folder, exist_ok=True)

    def _cleanup_old_files(self):
        cutoff = time.time() - Config.MAX_STORAGE_HOURS * 3600
        for fn in os.listdir(self.save_folder):
            p = os.path.join(self.save_folder, fn)
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
                logger.info(f"Removed old file: {fn}")

    def run(self):
        logger.info(f"Starting RTSP motion detection on {self.rtsp_url}")
        cap = cv2.VideoCapture(self.rtsp_url)
        if not cap.isOpened():
            logger.error(f"Failed to connect to RTSP stream: {self.rtsp_url}")
            return
        ret, frame1 = cap.read()
        ret, frame2 = cap.read()
        last_capture = 0
        last_cleanup = time.time()
        while cap.isOpened():
            if time.time() - last_cleanup > 1800:
                self._cleanup_old_files()
                last_cleanup = time.time()
            diff = cv2.absdiff(frame1, frame2)
            gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
            blur = cv2.GaussianBlur(gray, (5, 5), 0)
            _, thresh = cv2.threshold(blur, 20, 255, cv2.THRESH_BINARY)
            dilated = cv2.dilate(thresh, None, iterations=3)
            contours, _ = cv2.findContours(dilated, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
            motion = False
            max_area = 0
            for cnt in contours:
                area = cv2.contourArea(cnt)
                if area < self.min_contour_area:
                    continue
                x, y, w, h = cv2.boundingRect(cnt)
                cv2.rectangle(frame1, (x, y), (x + w, y + h), (0, 255, 0), 2)
                motion = True
                max_area = max(max_area, area)
            if motion and (time.time() - last_capture > self.motion_cooldown):
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"{Config.LOCATION_CODE}_{Config.DEVICE_SERIAL}_{ts}.jpg"
                filepath = os.path.join(self.save_folder, filename)
                cv2.imwrite(filepath, frame1)
                logger.info(f"[RTSP] Motion detected! Saved: {filepath}")
                # Trigger alarm
                self.hardware.activate_alarm(True)
                # Upload
                sftp_dst = os.path.join(Config.SFTP_UPLOAD_PATH, filename)
                uploaded = self.uploader.upload_image(filepath, sftp_dst)
                if uploaded:
                    metadata = {
                        "location": Config.LOCATION_CODE,
                        "reason": "motion_detected",
                        "area": max_area,
                        "local_file": filepath,
                        "sftp_path": sftp_dst
                    }
                    self.mqtt_client.publish_image_alert(sftp_dst, metadata)
                else:
                    logger.error(f"Failed to upload {filepath}")
                last_capture = time.time()
            else:
                # Optionally deactivate alarm if no recent motion
                if self.hardware.alarm_active and (time.time() - last_capture > Config.ALARM_DURATION):
                    self.hardware.activate_alarm(False)
            frame1 = frame2
            ret, frame2 = cap.read()
            if not ret:
                logger.error("Failed to grab RTSP frame")
                break
            time.sleep(0.01)
        cap.release()

# ===== Main App =====
class SecureVoltApp:
    def __init__(self):
        global hardware, mqtt_client
        hardware = HardwareController()
        self.modem = SIM7600Controller()
        mqtt_client = SecureVoltMQTT(hardware)
        self.uploader = FileUploader()
        self.rtsp_detector = RTSPMotionDetector(
            Config.RTSP_URL, Config.SAVE_FOLDER,
            Config.RTSP_MIN_CONTOUR_AREA, Config.RTSP_MOTION_COOLDOWN,
            self.uploader, mqtt_client, hardware
        )
        self.running = False

    def run(self):
        self.running = True
        if not mqtt_client.connect():
            logger.error("MQTT connect fail")
            return
        threading.Thread(target=self.rtsp_detector.run, daemon=True).start()
        try:
            while self.running:
                status = {"alarm_active": hardware.alarm_active}
                env = hardware.env_sensor.read()
                if env.get("status") == "OK":
                    net = {
                        "network": self.modem.get_network_status(),
                        "signal": self.modem.get_signal_quality(),
                        "gps": self.modem.get_gps_data()
                    }
                    ms = {"alarm_active": hardware.alarm_active}
                    mqtt_client.publish_sensor_data(net, env, hardware.alarm_active, ms)
                mqtt_client.publish_status(status)
                time.sleep(30)
        except KeyboardInterrupt:
            logger.info("Shutdown requested")
        finally:
            self.shutdown()

    def shutdown(self):
        self.running = False
        hardware.activate_alarm(False)
        hardware.cleanup()
        self.modem.close()
        mqtt_client.cleanup()
        self.uploader.close()
        logger.info("SecureVolt shutdown complete")

if __name__ == "__main__":
    app = SecureVoltApp()
    app.run()
