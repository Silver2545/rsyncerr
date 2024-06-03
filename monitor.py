import os
import time
import re
import subprocess
import paho.mqtt.client as mqtt
from threading import Lock

# Configuration from environment variables
LOG_PATHS = os.getenv('LOG_PATHS', '').split(';')
MQTT_SERVER = os.getenv('MQTT_SERVER')
MQTT_TOPIC = os.getenv('MQTT_TOPIC')
SOURCE_BASE = '/seedbox'
TARGET_BASE = '/data'

# Check if MQTT is enabled
MQTT_ENABLED = MQTT_SERVER is not None and MQTT_TOPIC is not None

# MQTT client setup
if MQTT_ENABLED:
    mqtt_client = mqtt.Client()
    mqtt_client.connect(MQTT_SERVER, 1883, 60)

# Lock to ensure only one rsync runs at a time
rsync_lock = Lock()

def monitor_logs():
    log_files = [open(log_path, 'r') for log_path in LOG_PATHS]
    for f in log_files:
        f.seek(0, os.SEEK_END)  # Move to the end of each file

    while True:
        for f in log_files:
            line = f.readline()
            if not line:
                time.sleep(1)  # Sleep briefly
                continue
            process_log_line(line.strip())

def process_log_line(log_line):
    error_pattern = r'path does not exist or is not accessible by (\w+): (.*)\. Ensure the path exists'
    match = re.search(error_pattern, log_line)
    
    if match:
        program = match.group(1).lower()
        path = match.group(2).strip()
        execute_rsync(program, path)

def execute_rsync(program, path):
    with rsync_lock:
        source_path = path.replace(f"{TARGET_BASE}/{program}", f"{SOURCE_BASE}/{program}")
        target_path = path
        
        # Check if the file already exists at the target location
        if os.path.exists(target_path):
            log_and_publish(f"File already exists at target location: {target_path}")
            return
        
        rsync_command = f"rsync -av --progress {source_path} {target_path}"
        try:
            result = subprocess.run(rsync_command, shell=True, check=True, capture_output=True, text=True)
            log_and_publish(f"rsync completed successfully for {path}")
            if path.endswith('.rar'):
                unpack_rar(target_path)
        except subprocess.CalledProcessError as e:
            log_and_publish(f"rsync failed for {path}: {e.output}")

def unpack_rar(file_path):
    unpack_command = f"unrar x {file_path} {os.path.dirname(file_path)}"
    try:
        result = subprocess.run(unpack_command, shell=True, check=True, capture_output=True, text=True)
        log_and_publish(f"Unpacked {file_path} successfully")
    except subprocess.CalledProcessError as e:
        log_and_publish(f"Unpacking failed for {file_path}: {e.output}")

def log_and_publish(message):
    print(message)  # Logging to stdout
    if MQTT_ENABLED:
        mqtt_client.publish(MQTT_TOPIC, message)
    else:
        print(f"MQTT not enabled: {message}")

def main():
    try:
        monitor_logs()
    except Exception as e:
        log_and_publish(f"Error monitoring logs: {e}")

if __name__ == "__main__":
    main()

