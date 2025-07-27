from gpiozero import OutputDevice
from time import sleep

# Define relay outputs using BCM pin numbers
relay_ch1 = OutputDevice(26)  # BOARD 37
relay_ch2 = OutputDevice(20)  # BOARD 38
relay_ch3 = OutputDevice(21)  # BOARD 40

try:
    while True:
        # CH3
        relay_ch3.on()
        sleep(1)
        relay_ch3.off()

        # CH2
        relay_ch2.on()
        sleep(1)
        relay_ch2.off()

        # CH1
        relay_ch1.on()
        sleep(1)
        relay_ch1.off()

except KeyboardInterrupt:
    print("Program interrupted by user.")

finally:
    # Turn off all relays
    relay_ch1.off()
    relay_ch2.off()
    relay_ch3.off()
    print("GPIO cleaned up.")
