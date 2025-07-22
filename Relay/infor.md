
Here’s a breakdown of the Waveshare 3‑Channel Relay HAT (designed for Pi 3/4/5 use via the standard 40‑pin GPIO header):

🛠️ Key Features
- 3 high-quality relays: Each rated for up to 5 A at 250 VAC or 5 A at 30 VDC
- Opto‑isolated inputs: Photo‑couplers (PC817) isolate the Pi from relay switching noise 
- On-board indicator LEDs: Show real-time status of each relay
- Control pin jumper block: Allows either default GPIO use or selection of custom GPIO pins via jumpers 
- Power supply: Powered directly from the Raspberry Pi 5V rail, no external power supply needed
- Screw terminal connectors: Each relay includes common (COM), normally-open (NO), and normally-closed (NC) terminals 

### Connections
Using BOARD numbering mode, which maps the physical pin numbers on the Raspberry Pi GPIO header. The Waveshare 3-Channel Relay HAT defaults to the following pin mapping:

| **Physical Pin (BOARD mode)** | **BCM GPIO** | **Relay Channel** |
| ----------------------------- | ------------ | ----------------- |
| 37                            | GPIO26       | CH1               |
| 38                            | GPIO20       | CH2               |
| 40                            | GPIO21       | CH3               |

 ### Power and Usage
- Draws standard 5 V from the Pi’s header for relays and +3.3 V for PCB logic when jumpers are closed—no external power required for control.
- Ensure the Pi can safely supply the combined current; if you drive multiple relays often, consider an external 5 V power supply for the relay coils.

 ### Using It on Raspberry Pi 5
- The Pi 5’s GPIO are still 3.3 V, so this HAT is fully compatible—no level shifting needed. The opto-isolation adds safety when switching devices like lamps or motors.


 ### Cautions
- Always verify wiring: connect COM, NO/NC correctly and ensure your external load shares a common ground or proper isolation.
- Add flyback diodes on any inductive loads.
- If powering high-current devices, use a separate 5 V supply to avoid overloading the Pi’s 5 V rail.