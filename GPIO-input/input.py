from Adafruit_GPIO import GPIO
import time


#use these when interacting with GPIO pins and switches

#GPIO.setup(21, GPIO.IN, pull_up_down=GPIO.PUD_UP)  # Set GPIO pin 21 as input with pull-up resistor


#GPIO.setup(21, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)  # Set GPIO pin 21 as input with pull-down resistor


GPIO.setmode(GPIO.BCM)  # Use BCM pin numbering/name
GPIO.setwarnings(False)  # Disable warnings
# Define the GPIO pin for the input
input_pin = 21  # GPIO pin number for input
# Setup the GPIO pin as input with pull-up resistor
GPIO.setup(input_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)  # Set GPIO pin as input with pull-up resistor

try:
    while True:
        # Read the input state
        input_state = GPIO.input(input_pin)

        if input_state == GPIO.LOW:  # Button pressed (active low)
            print("Button Pressed!")
        else:
            print("Button Released!")

        time.sleep(0.5)  # Delay to avoid rapid state changes


        if GPIO.input(input_pin) == GPIO.LOW:
            print("Button Pressed!")
        else:
            print("Button Released!")
        time.sleep(0.5)


except KeyboardInterrupt:
    print("Program stopped by user")
except Exception as e:
    print(f"An error occurred: {e}")
finally:
    # Cleanup GPIO settings
    GPIO.cleanup()  # Uncomment this line if you want to clean up GPIO settings after the loop