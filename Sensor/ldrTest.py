import RPi.GPIO as GPIO
import time

GPIO.setmode(GPIO.BCM)
pin=21

try:
    while True:
        reading=0
        GPIO.setup(pin,GPIO.OUT) # Set the pin as an output, which will allow us to control the LDR
        GPIO.output(pin,GPIO.LOW) # Set the pin to LOW to discharge the capacitor
        time.sleep(1) # Wait for a second to ensure the capacitor is discharged
        GPIO.setup(pin,GPIO.IN) # Set the pin as an input to read the LDR value

        # Read the LDR value by counting how long it takes for the pin to go HIGH
        while(GPIO.input(pin)==GPIO.LOW):
            reading = reading + 1 # Increment the reading count while the pin is LOW

        # Print the reading value
        print(reading)

        # The reading variable now contains the time it took for the pin to go HIGH
        reading = reading / 1000.0 # Convert the count to seconds
        reading = round(reading, 2)

        # Print the reading value
        print (reading)
        time.sleep(1) # Wait for a second before the next reading

except KeyboardInterrupt:
    print("Program stopped by user")
except Exception as e:
    print(f"An error occurred: {e}")
finally:
    GPIO.cleanup() # Cleanup GPIO settings



