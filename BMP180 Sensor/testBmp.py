import bmpsensor
import time

try:
    while True:
        print("====================================")
        temp, pressure, altitude = bmpsensor.readBmp180()
        print("Temperature is ", temp)  # degC
        print("Pressure is ", pressure)  # Pressure in Pa
        print("Altitude is ", altitude)  # Altitude in meters
        print("Timestamp: ", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))

        print("Sensor readings complete.")
        print("====================================")
        print("Note: Ensure the BMP180 sensor is connected properly.")


        print("\n")

        time.sleep(2) # Wait for 2 seconds before the next reading

except KeyboardInterrupt:
    print("Program stopped by user")
except Exception as e:
    print(f"An error occurred: {e}")
finally:
    print("Cleaning up resources")
    # No specific cleanup needed for bmpsensor, but you can add any necessary cleanup code here
    pass  # Placeholder for any cleanup code if needed
