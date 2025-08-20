import time
import serial
import board
import busio
from adafruit_bme280 import basic as adafruit_bme280
from gpiozero import LED

# ---------- BME280 SETUP ----------
i2c = busio.I2C(board.SCL, board.SDA)
bme280 = adafruit_bme280.Adafruit_BME280_I2C(i2c)
bme280.sea_level_pressure = 1013.25  # Optional for altitude accuracy

# ---------- SIM7600 SETUP ----------
SERIAL_PORT = "/dev/ttyUSB2"  # Usually AT command port
BAUD_RATE = 115200
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

def send_at(command, delay=1):
    """Send AT command and return response"""
    ser.write((command + "\r\n").encode())
    time.sleep(delay)
    return ser.read_all().decode(errors='ignore')

def send_sms(number, message):
    """Send SMS using SIM7600"""
    send_at("AT+CMGF=1")  # SMS text mode
    send_at(f'AT+CMGS="{number}"')
    ser.write(message.encode() + b"\x1A")  # CTRL+Z to send
    time.sleep(3)
    print("SMS sent!")

def get_gps():
    """Get GPS data"""
    send_at("AT+CGPS=1,1", 2)  # Enable GPS
    response = send_at("AT+CGPSINFO", 2)
    if ",," not in response:
        try:
            nmea_data = response.split(":")[1].strip().split("\n")[0]
            parts = nmea_data.split(",")
            if len(parts) >= 6:
                lat = parts[0] + parts[1]
                lon = parts[2] + parts[3]
                return lat, lon
        except:
            return None, None
    return None, None

# ---------- RELAY SETUP ----------
# Relay connected to GPIO pins (BCM)
relay_siren = LED(26)  # BOARD 37 (adjust if needed)
relay_light = LED(20)  # BOARD 38

def trigger_relays(duration=5):
    """Activate siren & light for a duration"""
    relay_siren.on()
    relay_light.on()
    print("Relays ON (siren & strobe)")
    time.sleep(duration)
    relay_siren.off()
    relay_light.off()
    print("Relays OFF")

# ---------- ALERT SETTINGS ----------
ALERT_PHONE = "+260XXXXXXXXX"  # Replace with real number
TEMP_THRESHOLD = 50.0  # °C
HUMIDITY_THRESHOLD = 90.0  # %
PRESSURE_THRESHOLD = 950.0  # hPa

print("Anti-vandalism system started...")

# ---------- MAIN LOOP ----------
while True:
    try:
        # Read BME280 data
        temp = bme280.temperature
        humidity = bme280.humidity
        pressure = bme280.pressure

        print(f"Temperature: {temp:.2f}°C, Humidity: {humidity:.2f}%, Pressure: {pressure:.2f}hPa")

        # Read GPS data
        lat, lon = get_gps()
        gps_text = f"Location: {lat},{lon}" if lat and lon else "GPS not fixed"
        print(gps_text)

        # Check thresholds & send SMS + activate relays
        if temp > TEMP_THRESHOLD or humidity > HUMIDITY_THRESHOLD or pressure < PRESSURE_THRESHOLD:
            alert_msg = (f"ALERT! Environmental anomaly.\n"
                         f"Temp: {temp:.1f}C, Hum: {humidity:.1f}%, Pres: {pressure:.1f}hPa\n"
                         f"{gps_text}")
            print("Sending alert:", alert_msg)
            send_sms(ALERT_PHONE, alert_msg)
            trigger_relays(duration=10)  # Sound siren/strobe for 10s
            time.sleep(15)  # Delay to prevent spamming

        time.sleep(5)  # Read every 5 seconds

    except KeyboardInterrupt:
        print("Exiting...")
        relay_siren.off()
        relay_light.off()
        break
    except Exception as e:
        print("Error:", e)
        time.sleep(2)
