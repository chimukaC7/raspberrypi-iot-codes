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
import cv2
import numpy as np
from collections import deque
import torch
from gpiozero import OutputDevice

import socket
import uuid
import subprocess

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


# ===== Utilities: Programmatic identifiers =====
def _get_device_serial():
    """
    Best-effort unique device id (stable across reboots):
    1) /proc/cpuinfo Serial (Pi)
    2) /etc/machine-id
    3) MAC (uuid.getnode)
    """
    # 1) Raspberry Pi CPU serial
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.lower().startswith("serial"):
                    val = line.split(":")[1].strip()
                    if val and val != "0000000000000000":
                        return val
    except Exception:
        pass

    # 2) machine-id
    try:
        with open("/etc/machine-id", "r") as f:
            mid = f.read().strip()
            if mid:
                return mid
    except Exception:
        pass

    # 3) MAC address fallback
    mac = uuid.getnode()
    return f"{mac:012x}"


def _get_ip_addresses():
    """
    Returns a list of IPv4 addresses for the host (no loopback).
    Prefers `hostname -I` and falls back to socket.
    """
    # Try hostname -I (space-separated)
    try:
        out = subprocess.check_output(["hostname", "-I"], stderr=subprocess.DEVNULL, text=True).strip()
        if out:
            ips = [ip for ip in out.split() if ip and not ip.startswith("127.")]
            if ips:
                return ips
    except Exception:
        pass

    # Fallback: socket trick (may only give one IP)
    ips = set()
    try:
        # Common trick to discover outbound IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    # Also try gethostbyname_ex
    try:
        host_ips = socket.gethostbyname_ex(socket.gethostname())[2]
        for ip in host_ips:
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass

    return sorted(list(ips))


# Precompute generated identifiers early so Config can reference them
GENERATED_DEVICE_SERIAL = _get_device_serial()
GENERATED_IP_ADDRESS = _get_ip_addresses()

log_dir = os.path.join(os.path.expanduser("~"), "securevolt_logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "securevolt.log")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3)]
)
logger = logging.getLogger("SecureVolt")


# ===== Configuration =====
class Config:
    DEVICE_ID = "SecureVolt_Pi5"
    # Programmatically generated values:
    DEVICE_SERIAL = GENERATED_DEVICE_SERIAL
    IP_ADDRESS = GENERATED_IP_ADDRESS  # <-- list of IPv4 strings

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
        # NOTE: command topic includes dynamically-determined DEVICE_SERIAL
        "command": "sim7600/" + DEVICE_SERIAL + "/alarm/control",
    }

    CAMERA_IP = "10.42.0.132"  # IP of the camera
    CAMERA_USER = "admin"
    CAMERA_PASS = "Securevolt1"
    CAMERA_TIMEOUT = 10
    # SNAPSHOT_URL removed; only RTSP used now

    MIN_CONTOUR_AREA = 10000
    CAPTURE_INTERVAL = 0.5
    SAVE_FOLDER = "/home/zesco/hikvision_captures"
    MAX_STORAGE_HOURS = 2

    ALARM_DURATION = 10
    ALARM_BUZZER_ENABLED = True
    ALARM_BUZZER_FREQ = 2000

    SERIAL_BAUDRATE = 115200
    COMMAND_DELAY = 0.1

    RTSP_URL = "rtsp://securevolt1:Securevolt1@" + CAMERA_IP + ":554/stream1"
    RTSP_MIN_CONTOUR_AREA = MIN_CONTOUR_AREA
    RTSP_MOTION_COOLDOWN = 3

    # SFTP server configuration
    SFTP_HOST = MQTT_BROKER  # Change if needed
    SFTP_PORT = 22
    SFTP_USER = "root"
    SFTP_PASS = "root123"
    SFTP_UPLOAD_PATH = "/var/www/html/AntiVandalismSensorSystem/public/videos/"  # Folder on SFTP server
    # SFTP_UPLOAD_PATH = "/var/www/html/AntiVandalismSensorSystem/public/videos/" + DEVICE_SERIAL + "/"# Folder on SFTP server


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
    # BCM pin mapping for relays
    RELAY_CH3 = 21  # (BOARD 40)
    RELAY_CH2 = 20  # (BOARD 38) - optional
    RELAY_CH1 = 26  # (BOARD 37) - optional

    def __init__(self):
        self.alarm_active = False
        self.relay_ch3 = None
        self.relay_ch2 = None
        self.relay_ch1 = None
        self.env_sensor = EnvironmentalSensor()  # Assuming this class is defined elsewhere
        self._init_gpiozero()

    def _init_gpiozero(self):
        try:
            # Initialize relays with initial value OFF (False)
            self.relay_ch3 = OutputDevice(self.RELAY_CH3, active_high=False, initial_value=False)
            self.relay_ch2 = OutputDevice(self.RELAY_CH2, active_high=False, initial_value=False)
            self.relay_ch1 = OutputDevice(self.RELAY_CH1, active_high=False, initial_value=False)
            logger.info("gpiozero: Relays initialized")
        except Exception as e:
            logger.error(f"gpiozero init failed: {e}")

    def activate_alarm(self, state=True):
        """
        Activate or deactivate alarm relay using gpiozero.
        """
        if not self.relay_ch3:
            logger.warning("Relay CH3 not initialized! (Mock alarm state)")
            self.alarm_active = state
            return

        try:
            if state:
                self.relay_ch3.on()
            else:
                self.relay_ch3.off()
            self.alarm_active = state
            logger.info(f"Alarm relay (GPIO {self.RELAY_CH3}) {'ON' if state else 'OFF'}")
        except Exception as e:
            logger.error(f"Alarm relay control error: {e}")

    def cleanup(self):
        """Turn off relays and release resources."""
        try:
            if self.relay_ch3:
                self.relay_ch3.off()
            if self.relay_ch2:
                self.relay_ch2.off()
            if self.relay_ch1:
                self.relay_ch1.off()
            logger.info("gpiozero: relays set to OFF")
        except Exception as e:
            logger.error(f"gpiozero cleanup error: {e}")


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
            '0,1': 'Registered - home network', '0,5': 'Registered - roaming',
            '0,2': 'Searching', '0,3': 'Denied', '0,4': 'Unknown'
        }.get(m.group(1), "Unknown")

    def get_signal_quality(self):
        r = self.send_at_command("AT+CSQ")
        m = re.search(r'\+CSQ: (\d+),', r)
        if not m or int(m.group(1)) == 99:
            return {"quality": "Unknown", "dbm": "N/A", "rssi": None}
        v = int(m.group(1));
        return {"quality": f"{(v / 31) * 100:.1f}%", "dbm": f"{-113 + 2 * v} dBm", "rssi": v}

    def get_gps_data(self, retries=3):
        for _ in range(retries):
            try:
                r = self.send_at_command("AT+CGPSINFO", timeout=10)
                m = re.search(r'\+CGPSINFO: ([^,]+),([NS]),([^,]+),([EW])', r)
                if m:
                    def conv(raw, dir):
                        d = float(raw[:2] if dir in 'NS' else raw[:3])
                        mpart = float(raw[2:] if dir in 'NS' else raw[3:])
                        d += mpart / 60
                        return -d if dir in 'SW' else d

                    return {"latitude": conv(m.group(1), m.group(2)), "longitude": conv(m.group(3), m.group(4)), "status": "Fix acquired"}
            except Exception as e:
                logger.error(f"[GPS ERROR] {e}")
            time.sleep(5)
        return {"status": "No fix"}

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close();
            logger.info("[SIM7600] Serial connection closed")


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

    def _mkdir_p(self, remote_path):
        """Recursively create remote directories if they do not exist"""
        dirs = []
        while True:
            head, tail = os.path.split(remote_path)
            if head == remote_path:  # Reached root
                if head and not self._exists(head):
                    dirs.insert(0, head)
                break
            if tail:
                dirs.insert(0, tail)
            remote_path = head
        path = ""
        for dir in dirs:
            path = os.path.join(path, dir)
            try:
                self.sftp.stat(path)
            except IOError:
                self.sftp.mkdir(path)

    def _exists(self, path):
        """Check if a remote path exists"""
        try:
            self.sftp.stat(path)
            return True
        except IOError:
            return False

    def upload_image(self, src, dst):
        try:
            if not self.sftp:
                self._connect()

            remote_dir = os.path.dirname(dst)
            self._mkdir_p(remote_dir)

            self.sftp.put(src, dst)
            logger.info(f"Uploaded {src} to {dst}")
            return True
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            return False

    def close(self):
        if self.sftp:
            self.sftp.close()
        if self.ssh:
            self.ssh.close()


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
            # Publish identity immediately upon successful connection
            ident_msg = {
                "timestamp": datetime.now().isoformat(),
                "device_id": Config.DEVICE_ID,
                "device_info": {
                    "serial": Config.DEVICE_SERIAL,
                    "location_code": Config.LOCATION_CODE,
                    # publish IP_ADDRESS explicitly as requested
                    "ip_address": Config.IP_ADDRESS,
                },
                "event": "device_identity"
            }
            self.client.publish(Config.MQTT_TOPICS["status"], json.dumps(ident_msg), qos=1, retain=False)
        else:
            logger.error(f"MQTT connect failed: {rc}")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            cmd = payload.get("command")
            print("received command:", cmd)
            if cmd == "on":
                self.hardware.activate_alarm(True)
            elif cmd == "off":
                self.hardware.activate_alarm(False)
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
        self.client.publish(
            Config.MQTT_TOPICS["sensor"],
            json.dumps({"timestamp": datetime.now().isoformat(), "device_id": Config.DEVICE_ID, "status": data}),
            qos=1
        )

    def publish_alarm_event(self, state, reason=""):
        self.client.publish(
            Config.MQTT_TOPICS["alarm"],
            json.dumps({"timestamp": datetime.now().isoformat(), "device_id": Config.DEVICE_ID, "state": state, "reason": reason}),
            qos=1,
            retain=True
        )

    def publish_sensor_data(self, net, env, alarm, motion):
        msg = {
            "modem": net,
            "timestamp": datetime.now().isoformat(),
            "device_info": {
                "serial": Config.DEVICE_SERIAL,
                "location_code": Config.LOCATION_CODE,
                # include IP_ADDRESS in routine status payloads
                "ip_address": Config.IP_ADDRESS,
            },
            "alarm_status": alarm,
            "environment": env,
            "motion_status": motion
        }
        self.client.publish(Config.MQTT_TOPICS["status"], json.dumps(msg), qos=0)

    def publish_image_alert(self, sftp_path, metadata):
        try:
            # sftp_path is server file path; just publish metadata
            self.client.publish(
                Config.MQTT_TOPICS["image"],
                json.dumps({
                    "timestamp": datetime.now().isoformat(),
                    "device_id": Config.DEVICE_ID,
                    "file_path": sftp_path,
                    "metadata": metadata
                }),
                qos=1
            )
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
        self.timestamp_mask = None

    def _cleanup_old_files(self):
        cutoff = time.time() - Config.MAX_STORAGE_HOURS * 3600
        for fn in os.listdir(self.save_folder):
            p = os.path.join(self.save_folder, fn)
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
                logger.info(f"Removed old file: {fn}")

    def _init_timestamp_mask(self, frame):
        """Auto-detect and mask out bright overlay in top-left corner."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (21, 21), 0)
        h, w = blurred.shape
        roi = blurred[0:int(h * 0.15), 0:int(w * 0.4)]  # Top-left region

        # Threshold to highlight bright overlay (e.g. white timestamp)
        _, thresh = cv2.threshold(roi, 200, 255, cv2.THRESH_BINARY)

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            largest = max(contours, key=cv2.contourArea)
            x, y, tw, th = cv2.boundingRect(largest)
            ignore_x_start = x
            ignore_x_end = x + tw
            ignore_y_start = y
            ignore_y_end = y + th
        else:
            # Fallback: small box in top-left
            ignore_x_start = 0
            ignore_x_end = 250
            ignore_y_start = 0
            ignore_y_end = 60

        # Create mask (255 = use, 0 = ignore)
        timestamp_mask = np.ones_like(gray, dtype=np.uint8) * 255
        timestamp_mask[ignore_y_start:ignore_y_end, ignore_x_start:ignore_x_end] = 0
        self.timestamp_mask = timestamp_mask

    def run(self):
        logger.info(f"Starting RTSP motion detection on {self.rtsp_url}")
        cap = cv2.VideoCapture(self.rtsp_url)
        if not cap.isOpened():
            logger.error(f"Failed to connect to RTSP stream: {self.rtsp_url}")
            return

        ret, frame1 = cap.read()
        if not ret:
            logger.error("Failed to grab initial frame")
            cap.release()
            return

        # Initialize timestamp mask with first frame
        self._init_timestamp_mask(frame1)

        # Apply mask and preprocess first frame
        gray1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY)
        blurred1 = cv2.GaussianBlur(gray1, (21, 21), 0)
        masked1 = cv2.bitwise_and(blurred1, blurred1, mask=self.timestamp_mask)

        ret, frame2 = cap.read()
        if not ret:
            logger.error("Failed to grab second frame")
            cap.release()
            return

        last_capture = 0
        last_cleanup = time.time()
        while cap.isOpened():
            if time.time() - last_cleanup > 1800:
                self._cleanup_old_files()
                last_cleanup = time.time()

            # Preprocess next frame
            gray2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY)
            blurred2 = cv2.GaussianBlur(gray2, (21, 21), 0)
            masked2 = cv2.bitwise_and(blurred2, blurred2, mask=self.timestamp_mask)

            # Frame differencing on masked frames
            diff = cv2.absdiff(masked1, masked2)
            _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
            dilated = cv2.dilate(thresh, None, iterations=2)
            contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            motion = False
            max_area = 0
            for cnt in contours:
                area = cv2.contourArea(cnt)
                if area < self.min_contour_area:
                    continue
                x, y, w, h = cv2.boundingRect(cnt)
                cv2.rectangle(frame2, (x, y), (x + w, y + h), (0, 255, 0), 2)
                motion = True
                max_area = max(max_area, area)

            if motion and (time.time() - last_capture > self.motion_cooldown):
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"{Config.LOCATION_CODE}_{Config.DEVICE_SERIAL}_{ts}.jpg"
                filepath = os.path.join(self.save_folder, filename)
                cv2.imwrite(filepath, frame2)
                logger.info(f"[RTSP] Motion detected! Saved: {filepath}")
                self.hardware.activate_alarm(True)
                sftp_dst = os.path.join(Config.SFTP_UPLOAD_PATH, filename)
                uploaded = self.uploader.upload_image(filepath, sftp_dst)
                if uploaded:
                    metadata = {
                        "location": Config.LOCATION_CODE,
                        "reason": "motion_detected",
                        "area": max_area,
                        "local_file": filepath,
                        "sftp_path": sftp_dst,
                        # Include identifiers for convenience in downstream consumers
                        "device_serial": Config.DEVICE_SERIAL,
                        "ip_address": Config.IP_ADDRESS,
                    }
                    self.mqtt_client.publish_image_alert(sftp_dst, metadata)
                else:
                    logger.error(f"Failed to upload {filepath}")
                last_capture = time.time()
            else:
                if self.hardware.alarm_active and (time.time() - last_capture > Config.ALARM_DURATION):
                    self.hardware.activate_alarm(False)

            masked1 = masked2  # move to next pair
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

        logger.info(f"Device identity: serial={Config.DEVICE_SERIAL}, ip_address={Config.IP_ADDRESS}")

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
    try:
        app.run()
    except KeyboardInterrupt:
        logger.info("Interrupted by user (KeyboardInterrupt)")
        app.shutdown()
    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
        app.shutdown()
