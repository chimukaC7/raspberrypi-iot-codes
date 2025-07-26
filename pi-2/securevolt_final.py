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
    SNAPSHOT_URL = f"http://{CAMERA_IP}/ISAPI/Streaming/channels/101/picture"

    MIN_CONTOUR_AREA = 1000
    CAPTURE_INTERVAL = 0.5
    SAVE_FOLDER = "/home/zesco/hikvision_captures"
    MAX_STORAGE_HOURS = 2

    ALARM_DURATION = 10
    ALARM_BUZZER_ENABLED = True
    ALARM_BUZZER_FREQ = 2000

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
        v=int(m.group(1));return{"quality":f"{(v/31)*100:.1f}%","dbm":f"{-113+2*v} dBm","rssi":v}

    def get_gps_data(self,retries=3):
        for _ in range(retries):
            try:
                r=self.send_at_command("AT+CGPSINFO",timeout=10)
                m=re.search(r'\+CGPSINFO: ([^,]+),([NS]),([^,]+),([EW])',r)
                if m:
                    def conv(raw,dir):d=float(raw[:2] if dir in 'NS' else raw[:3]);m=float(raw[2:] if dir in 'NS' else raw[3:]);d+=m/60;return- d if dir in 'SW' else d
                    return{"latitude":conv(m.group(1),m.group(2)),"longitude":conv(m.group(3),m.group(4)),"status":"Fix acquired"}
            except Exception as e:
                logger.error(f"[GPS ERROR] {e}")
            time.sleep(5)
        return{"status":"No fix"}

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close();logger.info("[SIM7600] Serial connection closed")

# ===== Motion Detector =====
class MotionDetector:
    def __init__(self):
        self.previous_frame=None;self.motion_detected=False;self.motion_counter=0
        self.last_motion_time=0;self.last_cleanup=time.time()
        self.auth=HTTPDigestAuth(Config.CAMERA_USER,Config.CAMERA_PASS)
        self.event_queue=Queue()
        os.makedirs(Config.SAVE_FOLDER,exist_ok=True)

    def get_camera_snapshot(self):
        try:
            r=requests.get(Config.SNAPSHOT_URL,auth=self.auth,timeout=Config.CAMERA_TIMEOUT,verify=False)
            if r.status_code==200:
                data=bytes()
                for c in r.iter_content(1024):data+=c
                return cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
        except Exception as e:logger.error(f"Camera error: {e}")
        return None

    def detect_motion(self,frame):
        if frame is None:return False,0
        g=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY);g=cv2.GaussianBlur(g,(21,21),0)
        if self.previous_frame is None:self.previous_frame=g;return False,0
        d=cv2.absdiff(self.previous_frame,g)
        t=cv2.threshold(d,25,255,cv2.THRESH_BINARY)[1]
        t=cv2.dilate(t,None,iterations=2)
        cnts,_=cv2.findContours(t.copy(),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        m=False;max_area=0
        for c in cnts:
            a=cv2.contourArea(c)
            if a>Config.MIN_CONTOUR_AREA:m=True;max_area=max(max_area,a)
        self.previous_frame=g;return m,max_area

    def save_image(self,frame):
        ts=datetime.now().strftime("%Y%m%d_%H%M%S")
        fn=f"{Config.LOCATION_CODE}_{Config.DEVICE_SERIAL}_{ts}.jpg"
        p=os.path.join(Config.SAVE_FOLDER,fn)
        try:cv2.imwrite(p,frame);return p
        except Exception as e:logger.error(f"Error saving image: {e}");return None

    def run_detection(self):
        logger.info("Starting motion detection")
        while True:
            if time.time()-self.last_cleanup>1800: self._cleanup_old_files();self.last_cleanup=time.time()
            f=self.get_camera_snapshot()
            cur,area=self.detect_motion(f)
            if cur:
                self.motion_counter+=1;self.last_motion_time=time.time()
                if not self.motion_detected and self.motion_counter>=3:
                    self.motion_detected=True;hardware.activate_alarm(True)
                    ipath=self.save_image(f)
                    if ipath:
                        md={"location":Config.LOCATION_CODE,"reason":"motion_detected","area":area}
                        mqtt_client.publish_image_alert(ipath,md)
            else:
                self.motion_counter=0
                if self.motion_detected and time.time()-self.last_motion_time>Config.ALARM_DURATION:
                    self.motion_detected=False;hardware.activate_alarm(False)
            t=time.time()-self.last_cleanup;time.sleep(max(0,Config.CAPTURE_INTERVAL-t))

    def _cleanup_old_files(self):
        now=time.time();cut=now-Config.MAX_STORAGE_HOURS*3600
        for fn in os.listdir(Config.SAVE_FOLDER):
            p=os.path.join(Config.SAVE_FOLDER,fn)
            if os.path.isfile(p) and os.path.getmtime(p)<cut:os.remove(p);logger.info(f"Removed old file: {fn}")

# ===== File Uploader =====
class FileUploader:
    def __init__(self):
        self.ssh=None;self.sftp=None;self._connect()
    def _connect(self):
        try:
            self.ssh=paramiko.SSHClient();self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh.connect(Config.MQTT_BROKER,port=22,username="root",password="root123",timeout=10)
            self.sftp=self.ssh.open_sftp();logger.info("SFTP connected")
        except Exception as e:logger.error(f"SFTP failed: {e}")
    def upload_image(self,src,dst):
        try:
            if not self.sftp:self._connect()
            self.sftp.put(src,dst);logger.info(f"Uploaded {src} to {dst}");return True
        except Exception as e:logger.error(f"Upload failed: {e}");return False
    def close(self):
        if self.sftp:self.sftp.close()
        if self.ssh:self.ssh.close()

# ===== MQTT Client =====
class SecureVoltMQTT:
    def __init__(self,hardware_controller):
        self.hardware=hardware_controller
        self.client=mqtt.Client(client_id=Config.DEVICE_ID)
        self.client.on_connect=self._on_connect;self.client.on_message=self._on_message
        self.connected=False

    def _on_connect(self,client,user,flags,reason):
        if reason==0:client.subscribe(Config.MQTT_TOPICS["command"]);self.connected=True;logger.info("MQTT connected")
        else:logger.error(f"MQTT connect failed: {reason}")
    def _on_message(self,client,user,msg):
        try:
            p=json.loads(msg.payload.decode());cmd=p.get("command")
            if cmd=="activate_alarm":self.hardware.activate_alarm(True)
            elif cmd=="deactivate_alarm":self.hardware.activate_alarm(False)
        except Exception as e:logger.error(f"MQTT msg error: {e}")

    def connect(self):
        try:self.client.connect(Config.MQTT_BROKER,Config.MQTT_PORT,60);self.client.loop_start();return True
        except Exception as e:logger.error(f"MQTT err: {e}");return False

    def publish_status(self,data):self.client.publish(Config.MQTT_TOPICS["sensor"],json.dumps({"timestamp":datetime.now().isoformat(),"device_id":Config.DEVICE_ID,"status":data}),qos=1)
    def publish_alarm_event(self,s,r=""):self.client.publish(Config.MQTT_TOPICS["alarm"],json.dumps({"timestamp":datetime.now().isoformat(),"device_id":Config.DEVICE_ID,"state":s,"reason":r}),qos=1,retain=True)
    def publish_sensor_data(self,md,ed,as_,ms):self.client.publish(Config.MQTT_TOPICS["status"],json.dumps({"modem":md,"timestamp":datetime.now().isoformat(),"device_info":{"serial":Config.DEVICE_SERIAL,"location_code":Config.LOCATION_CODE},"alarm_status":as_,"environment":ed,"motion_status":ms}),qos=0)
    def publish_image_alert(self,p,md):
        try:
            d=open(p,"rb").read();self.client.publish(Config.MQTT_TOPICS["image"],json.dumps({"timestamp":datetime.now().isoformat(),"device_id":Config.DEVICE_ID,"image":d.hex(),"metadata":md}),qos=1);return True
        except Exception as e:logger.error(f"MQTT img err: {e}");return False
    def cleanup(self):self.client.loop_stop();self.client.disconnect();logger.info("MQTT shutdown")

# ===== Main App =====
class SecureVoltApp:
    def __init__(self):
        self.hardware=HardwareController()
        self.modem=SIM7600Controller()
        self.mqtt=SecureVoltMQTT(self.hardware)
        self.detector=MotionDetector()
        self.uploader=FileUploader()
        self.running=False

    def run(self):
        self.running=True
        if not self.mqtt.connect():logger.error("MQTT connect fail");return
        threading.Thread(target=self.detector.run_detection,daemon=True).start()
        try:
            while self.running:
                status={"alarm_active":self.hardware.alarm_active,"motion_detected":self.detector.motion_detected}
                env=self.hardware.env_sensor.read()
                if env.get("status")=="OK":
                    md={"network":self.modem.get_network_status(),"signal":self.modem.get_signal_quality(),"gps":self.modem.get_gps_data()}
                    ms={"active":self.detector.motion_detected,"last_update":time.time(),"alarm_active":self.hardware.alarm_active,"current_alarm_interval":Config.ALARM_DURATION}
                    self.mqtt.publish_sensor_data(md,env,self.hardware.alarm_active,ms)
                self.mqtt.publish_status(status)
                time.sleep(30)
        except KeyboardInterrupt:
            logger.info("Shutdown requested")
        finally:
            self.shutdown()

    def shutdown(self):
        self.running=False
        self.hardware.activate_alarm(False)
        self.hardware.cleanup()
        self.modem.close()
        self.mqtt.cleanup()
        self.uploader.close()
        logger.info("SecureVolt shutdown complete")

if __name__ == "__main__":
    app=SecureVoltApp()
    app.run()
