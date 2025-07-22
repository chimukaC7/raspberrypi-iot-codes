#!/usr/bin/env python3
import os
import paramiko

# ── Configuration ───────────────────────────────────────────────────────────
HOST       = "10.110.50.94"
PORT       = 22
USER       = "root"
PASSWORD   = "root123"
LOCAL_DIR  = "/home/zesco/hikvision_captures/"
REMOTE_DIR = "/var/www/html/AntiVandalismSensorSystem/public/videos/"
# Only upload these extensions (set to [] to upload everything)
# EXT_WHITELIST = [".mp4", ".avi", ".jpg", ".png"]
EXT_WHITELIST = []
# ─────────────────────────────────────────────────────────────────────────────

def ensure_remote_dir(sftp, remote_path):
    """Recursively create remote directory if it doesn’t exist."""
    dirs = []
    head, tail = os.path.split(remote_path.rstrip('/'))
    while tail:
        dirs.insert(0, tail)
        head, tail = os.path.split(head)
    path = "/"
    for d in dirs:
        path = os.path.join(path, d)
        try:
            sftp.stat(path)
        except IOError:
            print(f"Creating remote directory: {path}")
            sftp.mkdir(path)

def main():
    transport = paramiko.Transport((HOST, PORT))
    transport.connect(username=USER, password=PASSWORD)
    sftp = paramiko.SFTPClient.from_transport(transport)

    # Make sure the target directory exists
    ensure_remote_dir(sftp, REMOTE_DIR)

    for fname in os.listdir(LOCAL_DIR):
        if EXT_WHITELIST:
            if not any(fname.lower().endswith(ext) for ext in EXT_WHITELIST):
                continue

        local_path  = os.path.join(LOCAL_DIR, fname)
        if not os.path.isfile(local_path):
            continue

        remote_path = os.path.join(REMOTE_DIR, fname)
        print(f"Uploading {fname} → {remote_path}")
        try:
            sftp.put(local_path, remote_path)
        except Exception as e:
            print(f"❌ Error uploading {fname}: {e}")

    sftp.close()
    transport.close()

if __name__ == "__main__":
    main()
