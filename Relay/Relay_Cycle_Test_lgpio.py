#!/usr/bin/env python3
import lgpio
import time

# GPIO mapping (BCM numbers, not BOARD)
# BOARD 37 = GPIO 26
# BOARD 38 = GPIO 20
# BOARD 40 = GPIO 21

RELAY_PINS = [21, 20, 26]  # BCM GPIOs for CH3, CH2, CH1

# Open a handle to the GPIO chip
h = lgpio.gpiochip_open(0)

# Set each relay pin as output
for pin in RELAY_PINS:
    lgpio.gpio_claim_output(h, pin, 0)

try:
    while True:
        # CH3
        lgpio.gpio_write(h, 21, 1)
        time.sleep(1)
        lgpio.gpio_write(h, 21, 0)

        # CH2
        lgpio.gpio_write(h, 20, 1)
        time.sleep(1)
        lgpio.gpio_write(h, 20, 0)

        # CH1
        lgpio.gpio_write(h, 26, 1)
        time.sleep(1)
        lgpio.gpio_write(h, 26, 0)

except KeyboardInterrupt:
    print("Program interrupted by user.")
except Exception as e:
    print(f"An error occurred: {e}")
finally:
    # Set all relays off
    for pin in RELAY_PINS:
        lgpio.gpio_write(h, pin, 0)

    # Close handle
    lgpio.gpiochip_close(h)
    print("GPIO cleaned up.")
