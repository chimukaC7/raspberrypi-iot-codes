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
from typing import Optional, Tuple
import socket
import uuid
import subprocess
import sqlite3

import cv2
import numpy as np
from gpiozero import OutputDevice

# Optional/extra imports kept from original (safe if unused)
import torch  # noqa: F401
import requests  # noqa: F401
from requests.auth import HTTPDigestAuth  # noqa: F401

try:
    # Nudge OpenCV/FFmpeg to be more resilient for RTSP
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;7000000|max_delay;500000")
    import lgpio  # noqa: F401
    from gpiozero import OutputDevice, PWMOutputDevice  # noqa: F401
    import board
    import busio
    from adafruit_bme280 import basic as adafruit_bme280
    HARDWARE_AVAILABLE = True
except ImportError as e:
    HARDWARE_AVAILABLE = False
    logging.basicConfig(level=logging.WARNING)
    _early_logger = logging.getLogger("SecureVolt")
    _early_logger.warning(f"Hardware components not available: {e}")

import paho.mqtt.client as mqtt
import paramiko
import serial


# ===== Utilities: Programmatic identifiers =====
def _get_device_serial() -> str:
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

    # Fallbacks
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass

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

# ===== Logging =====
log_dir = os.path.join(os.path.expanduser("~"), "securevolt_logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "securevolt.log")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3),
    ],
)
logger = logging.getLogger("SecureVolt")


# ===== Configuration =====
class Config:
    DEVICE_ID = "SecureVolt_Pi5"
    # Programmatically generated values:
    DEVICE_SERIAL = GENERATED_DEVICE_SERIAL
    IP_ADDRESS = GENERATED_IP_ADDRESS  # list of IPv4 strings

    LOCATION_CODE = "UNZA_SUBSTATION"

    # MQTT
    MQTT_BROKER = "10.16.1.17"
    MQTT_PORT = 1883
    MQTT_TOPICS = {
        "status": "sim7600/status",  # ONLY telemetry snapshot will be published here
        "alarm": "sim7600/alarm",
        "sensor": "sim7600/sensor",
        "image": "sim7600/image",
        "command": "sim7600/" + DEVICE_SERIAL + "/alarm/control",
    }

    # Motion / camera
    CAMERA_IP = '192.168.1.64'
    CAMERA_USER = "admin"
    CAMERA_PASS = "Securevolt1"
    CAMERA_TIMEOUT = 10

    MIN_CONTOUR_AREA = 10000
    CAPTURE_INTERVAL = 0.5
    SAVE_FOLDER = "/home/zesco/hikvision_captures"
    MAX_STORAGE_HOURS = 2

    # Alarm
    ALARM_DURATION = 10

    # Modem serial
    SERIAL_BAUDRATE = 115200
    COMMAND_DELAY = 0.1

    # RTSP
    # RTSP_URL = f"rtsp://securevolt1:Securevolt1@{CAMERA_IP}:554/stream1"
    RTSP_URL = "rtsp://admin:Securevolt1@"+CAMERA_IP+":554/Streaming/Channels/101"
    RTSP_MIN_CONTOUR_AREA = MIN_CONTOUR_AREA
    RTSP_MOTION_COOLDOWN = 3

    # SFTP (image upload)
    SFTP_HOST = MQTT_BROKER
    SFTP_PORT = 22
    SFTP_USER = "root"
    SFTP_PASS = "root123"
    SFTP_UPLOAD_PATH = "/var/www/html/AntiVandalismSensorSystem/public/videos/"

    # SQLite DB
    DB_PATH = os.path.join(os.path.expanduser("~"), "securevolt_alarm.db")

    # Capture/upload images even when disarmed
    CAPTURE_WHEN_DISARMED = True

    # Status publish interval (seconds)
    STATUS_PERIOD = 30


# ===== Alarm State Store (SQLite) =====
class AlarmStateStore:
    """
    Persists 'armed' (whether motion may trigger alarm) and 'relay_state' (actual relay ON/OFF).
    One row per device_serial.
    """
    def __init__(self, db_path: str, device_serial: str):
        self.db_path = db_path
        self.device_serial = device_serial
        self._ensure_schema()
        self._ensure_row()

    def _conn(self):
        return sqlite3.connect(self.db_path, timeout=5)

    def _ensure_schema(self):
        with self._conn() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS alarm_state(
                    device_serial TEXT PRIMARY KEY,
                    armed INTEGER NOT NULL,
                    relay_state INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """
            )

    def _ensure_row(self):
        with self._conn() as con:
            cur = con.execute(
                "SELECT 1 FROM alarm_state WHERE device_serial=?", (self.device_serial,)
            )
            if cur.fetchone() is None:
                con.execute(
                    """
                    INSERT INTO alarm_state(device_serial, armed, relay_state, updated_at)
                    VALUES(?, ?, ?, datetime('now'))
                """,
                    (self.device_serial, 1, 0),
                )

    def get(self) -> Tuple[bool, bool]:
        with self._conn() as con:
            row = con.execute(
                "SELECT armed, relay_state FROM alarm_state WHERE device_serial=?",
                (self.device_serial,),
            ).fetchone()
            if not row:
                return (True, False)
            armed, relay_state = row
            return (bool(armed), bool(relay_state))

    def set(self, armed: Optional[bool] = None, relay_state: Optional[bool] = None):
        if armed is None and relay_state is None:
            return
        with self._conn() as con:
            if armed is not None and relay_state is not None:
                con.execute(
                    """
                    UPDATE alarm_state
                    SET armed=?, relay_state=?, updated_at=datetime('now')
                    WHERE device_serial=?
                """,
                    (1 if armed else 0, 1 if relay_state else 0, self.device_serial),
                )
            elif armed is not None:
                con.execute(
                    """
                    UPDATE alarm_state
                    SET armed=?, updated_at=datetime('now')
                    WHERE device_serial=?
                """,
                    (1 if armed else 0, self.device_serial),
                )
            else:
                con.execute(
                    """
                    UPDATE alarm_state
                    SET relay_state=?, updated_at=datetime('now')
                    WHERE device_serial=?
                """,
                    (1 if relay_state else 0, self.device_serial),
                )


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
            return {
                "temperature": round(0, 2),
                "humidity": round(0, 2),
                "pressure": round(0, 2),
                "altitude": round(0, 2),
                "status": "Sensor not connected",
            }
        try:
            return {
                "temperature": round(self.sensor.temperature, 2),
                "humidity": round(self.sensor.humidity, 2),
                "pressure": round(self.sensor.pressure, 2),
                "altitude": round(self.sensor.altitude, 2),
                "status": "OK",
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
        self.env_sensor = EnvironmentalSensor()
        self._init_gpiozero()

    def _init_gpiozero(self):
        try:
            self.relay_ch3 = OutputDevice(
                self.RELAY_CH3, active_high=False, initial_value=False
            )
            self.relay_ch2 = OutputDevice(
                self.RELAY_CH2, active_high=False, initial_value=False
            )
            self.relay_ch1 = OutputDevice(
                self.RELAY_CH1, active_high=False, initial_value=False
            )
            logger.info("gpiozero: Relays initialized")
        except Exception as e:
            logger.error(f"gpiozero init failed: {e}")

    def activate_alarm(self, state: bool = True):
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
            logger.info(
                f"Alarm relay (GPIO {self.RELAY_CH3}) {'ON' if state else 'OFF'}"
            )
        except Exception as e:
            logger.error(f"Alarm relay control error: {e}")

    def cleanup(self):
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

        # Ensure GNSS powered (non-blocking, allow warm start)
        try:
            self.ensure_gnss_on(cold_start=False)
        except Exception as e:
            logger.warning(f"GNSS power-on attempt failed (will retry later): {e}")

    def _detect_port(self):
        ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
        for port in ports:
            try:
                with serial.Serial(port, Config.SERIAL_BAUDRATE, timeout=1) as ser:
                    ser.write(b"AT\r\n")
                    time.sleep(0.5)
                    if "OK" in ser.read(ser.in_waiting).decode(errors="ignore"):
                        logger.info(f"[SIM7600] Found on {port}")
                        return port
            except Exception:
                continue
        return None

    def _list_serial_ports(self):
        logger.info("Available serial ports:")
        for p in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
            logger.info(f"  {p}")

    def send_at_command(self, cmd, expected="OK", timeout=5):
        time.sleep(max(0, Config.COMMAND_DELAY - (time.time() - self.last_command_time)))
        self.ser.write((cmd + "\r\n").encode())
        self.last_command_time = time.time()
        resp = ""
        end = time.time() + timeout
        while time.time() < end:
            if self.ser.in_waiting:
                line = self.ser.readline().decode(errors="ignore").strip()
                resp += line + "\n"
                if expected in line or "ERROR" in line:
                    break
            time.sleep(0.1)
        if "ERROR" in resp:
            raise RuntimeError(f"AT command failed: {resp.strip()}")
        return resp

    # ---------- GNSS POWER/STATUS ----------
    def ensure_gnss_on(self, cold_start: bool = False) -> bool:
        """
        Turn on GNSS using whichever command family the firmware supports.
        cold_start=True tries a fresh start (slower but more reliable after moves).
        """
        # Prefer newer CGNS… family
        try:
            r = self.send_at_command("AT+CGNSPWR?")
            if "CGNSPWR:" in r and ",1" in r:
                return True
            self.send_at_command("AT+CGNSPWR=1")
            time.sleep(0.5)
            return True
        except Exception:
            pass

        # Fallback to legacy CGPS…
        try:
            if cold_start:
                self.send_at_command("AT+CGPS=0")
                time.sleep(0.3)
                self.send_at_command("AT+CGPS=1,1")  # cold start mode on some firmwares
            else:
                self.send_at_command("AT+CGPS=1")
            time.sleep(0.5)
            return True
        except Exception as e:
            logger.error(f"ensure_gnss_on failed: {e}")
            return False

    def gnss_powered(self) -> bool:
        try:
            r = self.send_at_command("AT+CGNSPWR?")
            if "CGNSPWR:" in r and ",1" in r:
                return True
        except Exception:
            pass
        try:
            r = self.send_at_command("AT+CGPS?")
            if "CGPS:" in r and ",1" in r:
                return True
        except Exception:
            pass
        return False

    # ---------- SIGNAL / RSSI ----------
    def get_network_status(self):
        r = self.send_at_command("AT+CREG?")
        m = re.search(r"\+CREG:\s*(\d,\d)", r)
        return {
            "0,1": "Registered - home network",
            "0,5": "Registered - roaming",
            "0,2": "Searching",
            "0,3": "Denied",
            "0,4": "Unknown",
        }.get(m.group(1) if m else "", "Unknown")

    def get_signal_quality(self):
        """
        Returns dict with mapped CSQ to percentage and dBm.
        """
        r = self.send_at_command("AT+CSQ")
        m = re.search(r"\+CSQ:\s*(\d+),", r)
        if not m:
            return {"quality": "Unknown", "dbm": "N/A", "rssi": None}
        val = int(m.group(1))
        if val == 99:
            return {"quality": "Unknown", "dbm": "N/A", "rssi": None}
        pct = f"{(val / 31) * 100:.1f}%"
        dbm = -113 + 2 * val
        return {"quality": pct, "dbm": f"{dbm} dBm", "rssi": val}

    # ---------- GPS / LOCATION ----------
    def get_gps_data(self, max_wait_sec: int = 10):
        """
        Try CGNSINF (new) then CGPSINFO (legacy). Wait up to max_wait_sec for a fix.
        Returns:
            {"latitude": float, "longitude": float, "status": "Fix acquired"} or {"status": "No fix"}
        """
        # Ensure GNSS is on (non-fatal if fails)
        self.ensure_gnss_on(cold_start=False)

        start = time.time()
        last_err = None
        while time.time() - start < max_wait_sec:
            # Prefer CGNSINF if available
            try:
                r = self.send_at_command("AT+CGNSINF", expected="OK", timeout=3)
                # Example: +CGNSINF: 1,1,YYYYMMDDhhmmss.sss,lat,lon,alt,...
                line = next((ln for ln in r.splitlines() if "+CGNSINF:" in ln), "")
                if line:
                    fields = line.split(":")[-1].strip().split(",")
                    # fields[0]=run(1), fields[1]=fix(1,2,3), fields[3]=lat, fields[4]=lon
                    if len(fields) >= 5 and fields[1] in ("1", "2", "3"):
                        try:
                            lat = float(fields[3])
                            lon = float(fields[4])
                            if lat != 0.0 or lon != 0.0:
                                return {
                                    "latitude": lat,
                                    "longitude": lon,
                                    "status": "Fix acquired",
                                }
                        except Exception:
                            pass
            except Exception as e:
                last_err = e

            # Fallback to CGPSINFO (DMS)
            try:
                r = self.send_at_command("AT+CGPSINFO", expected="OK", timeout=3)
                m = re.search(r"\+CGPSINFO:\s*([^,]*),([NS]),([^,]*),([EW])", r)
                if m:
                    def _conv(raw, dirc):
                        # DDDMM.MMMM
                        if not raw:
                            return 0.0
                        if dirc in ("N", "S"):
                            deg = float(raw[:2])
                            mins = float(raw[2:])
                        else:
                            deg = float(raw[:3])
                            mins = float(raw[3:])
                        val = deg + mins / 60.0
                        return -val if dirc in ("S", "W") else val

                    lat = _conv(m.group(1), m.group(2))
                    lon = _conv(m.group(3), m.group(4))
                    if lat != 0.0 or lon != 0.0:
                        return {
                            "latitude": lat,
                            "longitude": lon,
                            "status": "Fix acquired",
                        }
            except Exception as e:
                last_err = e

            time.sleep(2)  # short backoff

        if last_err:
            logger.warning(f"GPS query retries exhausted: {last_err}")
        return {"status": "No fix"}

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
            logger.info("[SIM7600] Serial connection closed")


# ===== File Uploader =====
class FileUploader:
    """
    Resilient SFTP uploader with:
      - SSH keepalive pings (every 30s)
      - Connection health checks before each upload
      - Automatic reconnect + exponential backoff on EOF/SSH/socket errors
    """
    def __init__(self):
        self.ssh = None
        self.sftp = None
        self._connect()

    def _connect(self):
        # Close any existing handles cleanly
        try:
            if self.sftp:
                self.sftp.close()
        except Exception:
            pass
        try:
            if self.ssh:
                self.ssh.close()
        except Exception:
            pass

        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        # Initial connect
        self.ssh.connect(
            Config.SFTP_HOST,
            port=Config.SFTP_PORT,
            username=Config.SFTP_USER,
            password=Config.SFTP_PASS,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
        )
        # Keep the TCP alive at SSH layer (server-friendly)
        try:
            self.ssh.get_transport().set_keepalive(30)
        except Exception:
            pass

        self.sftp = self.ssh.open_sftp()
        logger.info("SFTP connected (with keepalive)")

    def _healthy(self) -> bool:
        """Return True if SSH/SFTP look usable."""
        try:
            tr = self.ssh.get_transport() if self.ssh else None
            if not tr or not tr.is_active():
                return False
            # Probe SFTP by doing a cheap stat on the remote root.
            self.sftp.listdir(".")
            return True
        except Exception:
            return False

    def _ensure_connected(self):
        if not self._healthy():
            logger.warning("SFTP unhealthy → reconnecting")
            self._connect()

    def _mkdir_p(self, remote_path: str):
        """Recursively create remote directories if they do not exist."""
        parts = []
        path = remote_path
        # Normalize to POSIX pieces (Paramiko expects /)
        while True:
            head, tail = os.path.split(path)
            if tail:
                parts.append(tail)
            if head in ("", "/"):
                if head:
                    parts.append("/")
                break
            path = head
        parts = list(reversed(parts))

        cur = ""
        for part in parts[:-1]:  # all but the leaf file/dir
            if part == "/":
                cur = "/"
                continue
            cur = (cur + "/" + part) if cur != "/" else ("/" + part)
            try:
                self.sftp.stat(cur)
            except IOError:
                self.sftp.mkdir(cur)

    def upload_image(self, src: str, dst: str) -> bool:
        """
        Robust upload with retries. On known transient errors, reconnect + retry.
        """
        max_attempts = 5
        delay = 1.0
        for attempt in range(1, max_attempts + 1):
            try:
                self._ensure_connected()
                remote_dir = os.path.dirname(dst)
                if remote_dir:
                    self._mkdir_p(os.path.join(remote_dir, ""))  # ensure dir ends with '/'
                self.sftp.put(src, dst)
                logger.info(f"Uploaded {src} to {dst}")
                return True
            except (paramiko.SSHException, EOFError, OSError, socket.timeout) as e:
                msg = str(e) or e.__class__.__name__
                logger.warning(f"Upload attempt {attempt}/{max_attempts} failed: {msg}")
                # Reconnect then backoff and retry
                try:
                    self._connect()
                except Exception as re:
                    logger.error(f"SFTP reconnect error: {re}")
                time.sleep(delay)
                delay = min(delay * 2, 10.0)
            except Exception as e:
                logger.error(f"Upload failed (non-retryable): {e}")
                return False
        logger.error(f"Upload failed after {max_attempts} attempts (giving up)")
        return False

    def close(self):
        try:
            if self.sftp:
                self.sftp.close()
        except Exception:
            pass
        try:
            if self.ssh:
                self.ssh.close()
        except Exception:
            pass


# ===== MQTT Client =====
class SecureVoltMQTT:
    """
    Handles MQTT connection, subscribes to command topic, updates SQLite state,
    and toggles the relay. **Does not** publish to sim7600/status.
    """

    def __init__(self, hardware_controller: HardwareController, alarm_store: AlarmStateStore):
        self.hardware = hardware_controller
        self.alarm_store = alarm_store
        self.client = mqtt.Client(client_id=Config.DEVICE_ID)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.connected = False

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(Config.MQTT_TOPICS["command"])
            self.connected = True
            logger.info("MQTT connected & subscribed to command topic")
            # NOTE: No publishes to 'status' here
        else:
            logger.error(f"MQTT connect failed: {rc}")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            cmd = (payload.get("command") or "").strip().lower()
            logger.info(f"received command: {cmd}")

            if cmd in ("on", "arm"):
                self.alarm_store.set(armed=True)
                if cmd == "on":
                    self.hardware.activate_alarm(True)
                    self.alarm_store.set(relay_state=True)

            elif cmd in ("off", "disarm"):
                self.hardware.activate_alarm(False)
                self.alarm_store.set(armed=False, relay_state=False)

            else:
                logger.warning(f"Unknown command: {cmd}")
                return

            # NOTE: Do not publish any ack to 'status'
        except Exception as e:
            logger.error(f"MQTT msg error: {e}")

    def connect(self) -> bool:
        try:
            self.client.connect(Config.MQTT_BROKER, Config.MQTT_PORT, 60)
            self.client.loop_start()
            return True
        except Exception as e:
            logger.error(f"MQTT err: {e}")
            return False

    def publish_image_alert(self, sftp_path, metadata) -> bool:
        try:
            self.client.publish(
                Config.MQTT_TOPICS["image"],
                json.dumps(
                    {
                        "timestamp": datetime.now().isoformat(),
                        "device_id": Config.DEVICE_ID,
                        "file_path": sftp_path,
                        "metadata": metadata,
                    }
                ),
                qos=1,
            )
            return True
        except Exception as e:
            logger.error(f"MQTT img err: {e}")
            return False

    def cleanup(self):
        self.client.loop_stop()
        self.client.disconnect()
        logger.info("MQTT shutdown")


# ===== RTSP Motion Detector (includes upload + MQTT image alert) =====
class RTSPMotionDetector:
    FROZEN_HASH_REPEAT_MAX = 60         # ~60 iterations of identical frames → reopen
    STALE_FRAME_TIMEOUT_SEC = 12        # no *changed* frame for this long → reopen
    FORCE_REOPEN_EVERY_SEC = 15 * 60    # periodic safety reopen
    READ_FAILS_BEFORE_REOPEN = 5

    def __init__(
        self,
        rtsp_url: str,
        save_folder: str,
        min_contour_area: int,
        motion_cooldown: float,
        uploader: FileUploader,
        mqtt_client: SecureVoltMQTT,
        hardware: HardwareController,
        alarm_store: AlarmStateStore,
    ):
        self.rtsp_url = rtsp_url
        self.save_folder = save_folder
        self.min_contour_area = min_contour_area
        self.motion_cooldown = motion_cooldown
        self.uploader = uploader
        self.mqtt_client = mqtt_client
        self.hardware = hardware
        self.alarm_store = alarm_store
        os.makedirs(self.save_folder, exist_ok=True)

        self.cap = None
        self.timestamp_mask = None

        # Watchdogs
        self._last_frame_hash = None
        self._same_hash_count = 0
        self._last_change_ts = time.time()
        self._last_forced_reopen = 0.0

    # ---- small helpers ----
    @staticmethod
    def _quick_hash(frame: np.ndarray) -> int:
        """Cheap perceptual-ish hash: resize → grayscale → bytes → hash()."""
        try:
            small = cv2.resize(frame, (32, 32))   # tiny + fast
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            # Using Python's hash on bytes is enough to detect 'no change'
            return hash(gray.tobytes())
        except Exception:
            return 0

    def _cleanup_old_files(self):
        cutoff = time.time() - Config.MAX_STORAGE_HOURS * 3600
        for fn in os.listdir(self.save_folder):
            p = os.path.join(self.save_folder, fn)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    logger.info(f"Removed old file: {fn}")
            except Exception as e:
                logger.warning(f"Failed to remove {fn}: {e}")

    def _init_timestamp_mask(self, frame):
        # same as yours, but guard against empty frames
        if frame is None or frame.size == 0:
            self.timestamp_mask = None
            return

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (21, 21), 0)
        h, w = blurred.shape
        roi = blurred[0 : int(h * 0.15), 0 : int(w * 0.4)]
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
            ignore_x_start = 0
            ignore_x_end = 250
            ignore_y_start = 0
            ignore_y_end = 60

        mask = np.ones_like(gray, dtype=np.uint8) * 255
        mask[ignore_y_start:ignore_y_end, ignore_x_start:ignore_x_end] = 0
        self.timestamp_mask = mask

    def _open_capture(self, attempts: int = 6, base_delay: float = 1.0) -> bool:
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

        delay = base_delay
        for i in range(1, attempts + 1):
            try:
                logger.info(f"[RTSP] Opening stream (attempt {i}/{attempts}) → {self.rtsp_url}")
                cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)

                # Aggressive buffering control (keeps frames fresh)
                try:
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception:
                    pass

                ok, frame = cap.read()
                if ok and frame is not None and frame.size > 0:
                    self.cap = cap
                    self._same_hash_count = 0
                    self._last_frame_hash = None
                    self._last_change_ts = time.time()
                    self._last_forced_reopen = time.time()
                    self._init_timestamp_mask(frame)
                    logger.info("[RTSP] Stream opened successfully")
                    return True
                else:
                    cap.release()
            except Exception as e:
                logger.warning(f"[RTSP] Open attempt {i} failed: {e}")
            time.sleep(delay)
            delay = min(delay * 2, 8.0)

        logger.error("[RTSP] Failed to open stream after retries")
        return False

    def _maybe_force_reopen(self):
        now = time.time()

        # Periodic forced reopen
        if now - self._last_forced_reopen > self.FORCE_REOPEN_EVERY_SEC:
            logger.info("[RTSP] Periodic forced reopen")
            self._open_capture()
            return

        # Stale frame protection: no content change for too long
        if now - self._last_change_ts > self.STALE_FRAME_TIMEOUT_SEC:
            logger.warning("[RTSP] Stream appears stale (no change) → reopening")
            self._open_capture()

    def run(self):
        logger.info(f"Starting RTSP motion detection on {self.rtsp_url}")

        if not self._open_capture():
            logger.error(f"Failed to connect to RTSP stream: {self.rtsp_url}")
            return

        # Prime a second frame after open
        read_failures = 0
        last_capture = 0.0
        last_cleanup = time.time()

        while True:
            # Housekeeping
            if time.time() - last_cleanup > 1800:
                self._cleanup_old_files()
                last_cleanup = time.time()

            # Safety: bail if cap missing
            if self.cap is None:
                logger.warning("[RTSP] cap is None → reopen")
                if not self._open_capture():
                    time.sleep(2)
                    continue

            # Read a frame (guard for black/empty)
            ok, frame = (False, None)
            try:
                ok, frame = self.cap.read()
            except Exception as e:
                ok, frame = False, None
                logger.warning(f"[RTSP] read() threw: {e}")

            if not ok or frame is None or frame.size == 0:
                read_failures += 1
                logger.warning(f"[RTSP] Failed to grab frame ({read_failures}/{self.READ_FAILS_BEFORE_REOPEN})")
                if read_failures >= self.READ_FAILS_BEFORE_REOPEN:
                    logger.warning("[RTSP] Reopening stream after repeated failures")
                    if not self._open_capture():
                        logger.error("[RTSP] Stream reopen failed; sleeping before next retry")
                        time.sleep(3)
                    read_failures = 0
                else:
                    time.sleep(0.05)
                # On failure, attempt stale check too
                self._maybe_force_reopen()
                continue
            else:
                read_failures = 0

            # Frozen-frame detection
            h = self._quick_hash(frame)
            if h == self._last_frame_hash:
                self._same_hash_count += 1
            else:
                self._same_hash_count = 0
                self._last_change_ts = time.time()
                self._last_frame_hash = h

            if self._same_hash_count >= self.FROZEN_HASH_REPEAT_MAX:
                logger.warning("[RTSP] Frozen frame detected → reopening stream")
                self._open_capture()
                self._same_hash_count = 0
                time.sleep(0.1)
                continue

            # Motion detection pipeline (masked diff)
            try:
                # (Re)create mask if missing (e.g., after reopen)
                if self.timestamp_mask is None:
                    self._init_timestamp_mask(frame)

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                blurred = cv2.GaussianBlur(gray, (21, 21), 0)
                masked = cv2.bitwise_and(blurred, blurred, mask=self.timestamp_mask)

                # Use previous masked frame from short rolling memory
                if not hasattr(self, "_prev_masked") or self._prev_masked is None:
                    self._prev_masked = masked
                    time.sleep(0.01)
                    self._maybe_force_reopen()
                    continue

                diff = cv2.absdiff(self._prev_masked, masked)
                _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
                dilated = cv2.dilate(thresh, None, iterations=2)
                contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

                motion = False
                max_area = 0
                for cnt in contours:
                    area = cv2.contourArea(cnt)
                    if area < self.min_contour_area:
                        continue
                    motion = True
                    max_area = max(max_area, area)

                if motion and (time.time() - last_capture > self.motion_cooldown):
                    armed, relay_state = self.alarm_store.get()
                    should_capture = True if Config.CAPTURE_WHEN_DISARMED else armed

                    if should_capture:
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        filename = f"{Config.LOCATION_CODE}_{Config.DEVICE_SERIAL}_{ts}.jpg"
                        filepath = os.path.join(self.save_folder, filename)
                        try:
                            cv2.imwrite(filepath, frame)
                            logger.info(f"[RTSP] Motion detected! Saved: {filepath}")
                        except Exception as e:
                            logger.error(f"[RTSP] Failed to write image {filepath}: {e}")
                            self._prev_masked = masked
                            time.sleep(0.01)
                            self._maybe_force_reopen()
                            continue

                        if armed:
                            self.hardware.activate_alarm(True)
                            self.alarm_store.set(relay_state=True)

                        sftp_dst = os.path.join(Config.SFTP_UPLOAD_PATH, filename)
                        uploaded = self.uploader.upload_image(filepath, sftp_dst)
                        if uploaded:
                            metadata = {
                                "location": Config.LOCATION_CODE,
                                "reason": "motion_detected" if armed else "motion_detected_disarmed",
                                "area": max_area,
                                "local_file": filepath,
                                "sftp_path": sftp_dst,
                                "device_serial": Config.DEVICE_SERIAL,
                                "ip_address": Config.IP_ADDRESS,
                                "armed": armed,
                                "relay_state": relay_state,
                            }
                            self.mqtt_client.publish_image_alert(sftp_dst, metadata)
                        else:
                            logger.error(f"[RTSP] Upload failed for {filepath}")

                        last_capture = time.time()
                    else:
                        logger.info("[RTSP] Motion but disarmed & capture disabled; skipping")

                # Auto clear alarm relay after ALARM_DURATION
                armed, relay_state = self.alarm_store.get()
                if relay_state and armed and (time.time() - last_capture > Config.ALARM_DURATION):
                    self.hardware.activate_alarm(False)
                    self.alarm_store.set(relay_state=False)

                self._prev_masked = masked

            except Exception as e:
                logger.error(f"[RTSP] Motion pipeline error: {e}")

            # Reopen checks that don’t depend on errors
            self._maybe_force_reopen()

            time.sleep(0.01)


# ===== Main App =====
class SecureVoltApp:
    def __init__(self):
        global hardware, mqtt_client
        hardware = HardwareController()

        # alarm state store
        self.alarm_store = AlarmStateStore(Config.DB_PATH, Config.DEVICE_SERIAL)
        armed, relay_state = self.alarm_store.get()

        # Ensure physical relay matches persisted relay_state on boot
        hardware.activate_alarm(relay_state)

        # Modem, MQTT, uploader, motion detector
        self.modem = SIM7600Controller()
        mqtt_client = SecureVoltMQTT(hardware, self.alarm_store)
        self.mqtt = mqtt_client
        self.mqtt_client = mqtt_client.client  # raw paho client for publishing
        self.uploader = FileUploader()
        self.rtsp_detector = RTSPMotionDetector(
            Config.RTSP_URL,
            Config.SAVE_FOLDER,
            Config.RTSP_MIN_CONTOUR_AREA,
            Config.RTSP_MOTION_COOLDOWN,
            self.uploader,
            mqtt_client,
            hardware,
            self.alarm_store,
        )
        self.running = False

        logger.info(
            f"Device identity: serial={Config.DEVICE_SERIAL}, ip_address={Config.IP_ADDRESS}, "
            f"armed={armed}, relay={relay_state}"
        )

    # ---- Status snapshot (ONLY thing allowed on sim7600/status) ----
    def build_status_snapshot(self) -> dict:
        # Ensure GNSS is on (non-blocking)
        try:
            self.modem.ensure_gnss_on(cold_start=False)
        except Exception:
            pass

        # modem info
        net_status = self.modem.get_network_status()
        sig = self.modem.get_signal_quality()
        gps = self.modem.get_gps_data(max_wait_sec=10)  # small wait for fix

        # device info
        serial = Config.DEVICE_SERIAL
        ips = Config.IP_ADDRESS

        # alarm state
        armed, relay_state = self.alarm_store.get()

        # environment
        env = hardware.env_sensor.read()

        # motion mirror
        motion = {"alarm_active": hardware.alarm_active}

        # EXACT structure requested
        payload = {
            "modem": {"network": net_status, "signal": sig, "gps": gps},
            "timestamp": datetime.now().isoformat(),
            "device_info": {
                "serial": serial,
                "location_code": Config.LOCATION_CODE,
                "ip_address": ips,
            },
            "armed": armed,
            "alarm_status": relay_state,
            "environment": env,
            "motion_status": motion,
        }
        return payload
    
    def publish_status_snapshot(self):
        payload = self.build_status_snapshot()
        try:
            self.mqtt_client.publish(Config.MQTT_TOPICS["status"], json.dumps(payload), qos=1)
            logger.info(f"[STATUS] published → {Config.MQTT_TOPICS['status']}")
        except Exception as e:
            logger.warning(f"[STATUS] publish failed: {e}")

    def run(self):
        if not self.mqtt.connect():
            logger.error("MQTT connect fail")
            return
        self.running = True
        threading.Thread(target=self.rtsp_detector.run, daemon=True).start()
        try:
            while self.running:
                self.publish_status_snapshot()  # ONLY status publisher
                time.sleep(Config.STATUS_PERIOD)
        except KeyboardInterrupt:
            logger.info("Shutdown requested")
        finally:
            self.shutdown()

    def shutdown(self):
        self.running = False
        hardware.activate_alarm(False)
        hardware.cleanup()
        try:
            self.modem.close()
        except Exception:
            pass
        try:
            self.mqtt.cleanup()
        except Exception:
            pass
        try:
            self.uploader.close()
        except Exception:
            pass
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
