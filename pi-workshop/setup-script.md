# If you previously created the venv as root or another user, remove it:
sudo rm -rf /opt/securevolt/venv

# Ensure directory ownership
sudo chown -R securevolt:www-data /opt/securevolt

# Create venv and install deps as the service user
sudo -u securevolt -H bash -lc '
cd /opt/securevolt
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install flask gunicorn opencv-python-headless
'



# If pip wheels keep failing on Pi

Use system packages instead (no venv needed):
sudo apt update
sudo apt install -y python3-flask python3-opencv gunicorn
