import os
import cv2
import time
import threading
from flask import Flask, Response

# Optional: nudges OpenCV's FFmpeg backend for RTSP stability
# - force TCP transport
# - short read timeout
# - small max_delay (lower latency)
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000|max_delay;500000"

app = Flask(__name__)

RTSP_URL = "rtsp://securevolt1:Securevolt1@10.42.0.132:554/stream1"

class Camera:
    def __init__(self, url):
        self.url = url
        self.cap = None
        self.lock = threading.Lock()
        self.latest_jpeg = None
        self.running = False
        self.thread = None
        self.width = None
        self.height = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        self._release()

    def _open(self):
        # Use FFMPEG backend explicitly (more RTSP options)
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)

        # Try to trim buffering / add timeouts (OpenCV honors some of these)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)                  # small queue
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)        # 5s open timeout
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)        # 5s read timeout

        if not cap.isOpened():
            return None
        return cap

    def _release(self):
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    def _reader(self):
        backoff = 1.0
        while self.running:
            if self.cap is None:
                self.cap = self._open()
                if self.cap is None:
                    time.sleep(min(backoff, 10))
                    backoff = min(backoff * 2, 10)
                    continue
                backoff = 1.0  # reset on success

            ok, frame = self.cap.read()
            if not ok or frame is None:
                # Lost the stream: close and retry
                self._release()
                time.sleep(0.3)
                continue

            # Cache dimensions (optional)
            if self.width is None or self.height is None:
                self.height, self.width = frame.shape[:2]

            # Encode once here (so clients don't compete)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                continue
            with self.lock:
                self.latest_jpeg = buf.tobytes()

            # Small sleep helps CPU when FPS is very high
            time.sleep(0.001)

    def get_frame(self):
        with self.lock:
            return self.latest_jpeg

camera = Camera(RTSP_URL)
camera.start()

def mjpeg_generator():
    boundary = b"--frame"
    while True:
        jpg = camera.get_frame()
        if jpg is None:
            # Before first frame or reconnecting
            time.sleep(0.05)
            continue
        yield (boundary + b"\r\n"
               b"Content-Type: image/jpeg\r\n"
               b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" +
               jpg + b"\r\n")

@app.route("/")
def index():
    # Simple page
    return "<h1>Tapo C510W Live</h1><img style='max-width:100%;' src='/video'>"

@app.route("/video")
def video():
    # Each client gets frames from the same background reader (no races)
    return Response(mjpeg_generator(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")

if __name__ == "__main__":
    # Keep Flask threaded (multiple clients OK) because camera reading is single-threaded
    print("🌐 Visit http://<your-pi-ip>:5000 to view the stream.")
    try:
        app.run(host="0.0.0.0", port=5000, threaded=True)
    finally:
        camera.stop()
