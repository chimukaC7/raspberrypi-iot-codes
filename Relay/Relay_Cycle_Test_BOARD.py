# import GPIO and time
import RPi.GPIO as GPIO
import time

# set GPIO numbering mode and define output pins
GPIO.setmode(GPIO.BOARD)
GPIO.setup(37, GPIO.OUT)
GPIO.setup(38, GPIO.OUT)
GPIO.setup(40, GPIO.OUT)

# cycle those relays
try:
    while True:
        GPIO.output(40, True)
        time.sleep(1)
        GPIO.output(40, False)

        GPIO.output(38, True)
        time.sleep(1)
        GPIO.output(38, False)

        GPIO.output(37, True)
        time.sleep(1)
        GPIO.output(37, False)

finally:
    # cleanup the GPIO before finishing :)
    GPIO.cleanup()
