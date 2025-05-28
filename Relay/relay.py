import RPi.GPIO as GPIO
import time

led1 = 21

# Setup GPIO mode and pin
GPIO.setmode(GPIO.BCM) # Use BCM pin numbering
GPIO.setup(led1, GPIO.OUT) # Set pin 21 as an output pin

# This code will turn on an LED connected to GPIO pin 21 for 2 seconds, then turn it off for 2 seconds, repeatedly.
try:
    while True:
        GPIO.output(led1, True) # Turn on the LED
        time.sleep(2) # Wait for 2 seconds
        GPIO.output(led1, False) # Turn off the LED
        time.sleep(2) # Wait for 2 seconds
except KeyboardInterrupt:
    print("Program interrupted by user.")
except Exception as e:
    print(f"An error occurred: {e}")
finally:
    GPIO.cleanup() # Cleanup GPIO settings on exit
    print("GPIO cleanup done.")
