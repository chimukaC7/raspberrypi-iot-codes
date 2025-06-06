import RPi.GPIO as GPIO
import time

# Setup GPIO
# GPIO.setmode(GPIO.BoARD)  # Use BOARD pin numbering
GPIO.setmode(GPIO.BCM)  # Use BCM pin numbering/name
GPIO.setwarnings(False)  # Disable warnings

# LED setup
led1=12 # Define the GPIO pin for the LED
led2=16 # Define the GPIO pin for the LED
led3=20 # Define the GPIO pin for the LED
led4=21 # Define the GPIO pin for the LED


GPIO.setup(led1,GPIO.OUT) # Set the LED pin as an output
GPIO.setup(led2,GPIO.OUT) # Set the LED pin as an output
GPIO.setup(led3,GPIO.OUT) # Set the LED pin as an output
GPIO.setup(led4,GPIO.OUT) # Set the LED pin as an output

try:
    while True:
        GPIO.output(led1,True) # Turn on the LED
        GPIO.output(led2,True) # Turn on the LED
        GPIO.output(led3,True) # Turn on the LED
        GPIO.output(led4,True) # Turn on the LED
        # GPIO.output(led,GPIO.HIGH)  # Turn on the LED
        print("LED ON")

        time.sleep(4) # Wait for 4 seconds

        GPIO.output(led1,False) # Turn off the LED
        GPIO.output(led2,False) # Turn off the LED
        GPIO.output(led3,False) # Turn off the LED
        GPIO.output(led4,False) # Turn off the LED
        # GPIO.output(led,GPIO.LOW)  # Turn off the LED
        print("LED OFF")

        time.sleep(2) # Wait for 2 seconds before the next iteration

except KeyboardInterrupt:
    print("Program stopped by user")
except Exception as e:
    print(f"An error occurred: {e}")
finally:
    # Cleanup GPIO settings
    GPIO.cleanup()  # Uncomment this line if you want to clean up GPIO settings after the loop
    