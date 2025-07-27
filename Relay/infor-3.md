Here's your relay control script rewritten using **`gpiozero`**, which provides a **simpler and more Pythonic interface** for GPIO handling. It works well on **Raspberry Pi 5** and uses the **BCM pin numbering** scheme.

---

### ✅ Rewritten Code Using `gpiozero`:

```python
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
```

---

### ✅ Notes:

* `gpiozero` handles cleanup automatically.
* You **don’t need `sudo`** for `gpiozero` because it uses `/dev/gpiomem`.
* BCM numbers:

  * **26** → physical pin **37** (CH1)
  * **20** → pin **38** (CH2)
  * **21** → pin **40** (CH3)

---

### ✅ Install `gpiozero` (if not already):

```bash
sudo apt install python3-gpiozero
```

---

Let me know if you'd like to add button control, timed cycles, or web access next!
