import RPi.GPIO as GPIO
import time

GPIO.setmode(GPIO.BCM)

led=21

GPIO.setup(led,GPIO.OUT)

try:
    while True:
        GPIO.output(led,True)
        time.sleep(4)
        # time.sleep(0.3)
        GPIO.output(led,False)
        time.sleep(2)
        # time.sleep(0.1)
except KeyboardInterrupt:
    print("Program stopped by user")
except Exception as e:
    print(f"An error occurred: {e}")
finally:
    GPIO.cleanup()  # Cleanup GPIO settings
