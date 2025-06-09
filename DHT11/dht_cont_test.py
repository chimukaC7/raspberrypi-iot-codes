
import Adafruit_DHT
import time
sensor = Adafruit_DHT.DHT11

pin = 21

try:
    while True:
        humidity, temperature = Adafruit_DHT.read_retry(sensor, pin)
        if humidity is not None and temperature is not None:
            print('Temp={0}*C  Humidity={1}%'.format(temperature, humidity))
        else:
            print('Failed to get reading. Try again!')
        time.sleep(1) # Wait for a second before the next reading

except KeyboardInterrupt:
    print("Program stopped by user")
except Exception as e:
    print(f"An error occurred: {e}")
finally:
    print("Cleaning up resources")
    # No specific cleanup needed for Adafruit_DHT, but you can add any necessary cleanup code here