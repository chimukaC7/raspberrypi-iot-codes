# import GPIO and time
import RPi.GPIO as GPIO
import time



# set GPIO numbering mode and define output pins
GPIO.setmode(GPIO.BOARD)
GPIO.setup(37, GPIO.OUT) # Relay Channel 1 (CH1)
GPIO.setup(38, GPIO.OUT) # Relay Channel 2 (CH2)
GPIO.setup(40, GPIO.OUT) # Relay Channel 3 (CH3)

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
