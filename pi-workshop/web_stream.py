import cv2
from flask import Flask, Response

app = Flask(__name__)

# RTSP URL from Tapo camera
RTSP_URL = "rtsp://securevolt1:Securevolt1@10.42.0.132:554/stream1"
cap = cv2.VideoCapture(RTSP_URL)

def generate_frames():
    while True:
        success, frame = cap.read()
        if not success:
            print("Failed to capture frame from camera.")
            yield (b'--frame\r\n'
                   b'Content-Type: text/plain\r\n\r\n'
                   b'Error: Could not read frame\r\n')
            break
        else:
            # Encode frame as JPEG
            _, buffer = cv2.imencode('.jpg', frame)
            frame_bytes = buffer.tobytes()

            # Yield frame as multipart HTTP stream
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

@app.route('/')
def index():
    return "<h1>Tapo C510W Live Stream</h1><img src='/video'>"

@app.route('/video')
def video():
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    print("🌐 Visit http://<your-pi-ip>:5000 to view the stream.")
    app.run(host='0.0.0.0', port=5000, threaded=True)
