import RPi.GPIO as GPIO
import time
TRIG=21 # GPIO pin for Trigger
ECHO=20 # GPIO pin for Echo
GPIO.setmode(GPIO.BCM)

try:
    while True:
        print("distance measurement in progress")
        GPIO.setup(TRIG,GPIO.OUT) # Set the Trigger pin as an output
        GPIO.setup(ECHO,GPIO.IN) # Set the Echo pin as an input

        GPIO.output(TRIG,False) # Ensure the Trigger pin is LOW
        print ("waiting for sensor to settle")
        time.sleep(0.2)

        GPIO.output(TRIG,True) # Send a 10 microsecond pulse to the Trigger pin
        time.sleep(0.00001)
        GPIO.output(TRIG,False)

        while GPIO.input(ECHO)==0:
            pulse_start=time.time()
        while GPIO.input(ECHO)==1:
            pulse_end=time.time()

        # Calculate the distance based on the time of flight
        pulse_duration=pulse_end-pulse_start

        # Speed of sound is approximately 34300 cm/s, so we divide by 2 for the round trip
        # and multiply by 1000000 to convert seconds to microseconds
        # Distance in cm = (time in seconds * speed of sound in cm/s) / 2
        # Here, we use 17150 to convert the time in seconds to distance in cm
        # Formula: distance = (pulse_duration * speed_of_sound) / 2
        distance=pulse_duration*17150
        distance=round(distance,2)
        print("distance:",distance,"cm")
        time.sleep(2)
except KeyboardInterrupt:
    print("Measurement stopped by user")
except Exception as e:
    print(f"An error occurred: {e}")
finally:
    print("Cleaning up resources")
    GPIO.cleanup()