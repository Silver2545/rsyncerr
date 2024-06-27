import transmission_rpc
import logging
import os
import subprocess
import re

# Configure logging
logging.basicConfig(filename='transmission_log.txt', level=logging.INFO, format='%(asctime)s - %(message)s')

# Transmission instance details
HOST = 'ternyon-transmission.cloud.seedboxes.cc'
PORT = 443  # Default Transmission port
USERNAME = 'ternyon'
PASSWORD = 'xamDxkyeJfDUwq23zcL9'
PROTOCOL = 'https'
REMOTE_DIRECTORY = '/home/user/files/downloads'
LOCAL_DIRECTORY = '/data'
LOCAL_TORRENTS = '/mnt/storage/local/torrents'
PUID = '1001'
GUID = '1001'


skip_strings = [
    "sending incremental file list",
    "Number of files:",
    "Number of created files:",
    "Number of deleted files:",
    "Total file size:",
    "Total transferred file size:",
    "Literal data:",
    "Matched data:",
    "File list size:",
    "File list generation time",
    "File list transfer time:",
    "Total bytes sent:",
    "Total bytes received:",
    "total size is"
]

pattern = re.compile(r'(\d+)%')  # Pattern to match percentage
milestones = [10, 25, 50, 75, 90]  # Example milestones for progress logging
tolerance = 2  # Tolerance for logging milestones

def within_tolerance(value, milestones, tolerance):
    for milestone in milestones:
        if abs(value - milestone) <= tolerance:
            return milestone
    return None


def format_size(size_bytes):
    """
    Convert bytes to a human-readable format (e.g., KB, MB, GB).
    """
    if size_bytes < 1024:
        return f"{size_bytes} bytes"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.2f} KB"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / (1024 ** 2):.2f} MB"
    else:
        return f"{size_bytes / (1024 ** 3):.2f} GB"

try:
    # Connect to the Transmission instance
    client = transmission_rpc.Client(
        host=HOST,
        port=PORT,
        username=USERNAME,
        password=PASSWORD,
        protocol=PROTOCOL
    )

    # Log connection success
    logging.info("Successfully connected to Transmission daemon")
    print("Successfully connected to Transmission daemon")

    # Get a list of all torrents
    torrents = client.get_torrents()

    # Print attributes of each torrent
    if torrents:
        for idx, torrent in enumerate(torrents):
            logging.info(f"Torrent {idx + 1} attributes:")
            print(f"Torrent {idx + 1} attributes:")

            if 'fields' in torrent.__dict__:
                fields = torrent.__dict__['fields']

                # Print specific attributes with enhanced formatting
                if 'id' in fields:
                    print(f"id: {fields['id']}")
                if 'name' in fields:
                    print(f"name: {fields['name']}")

                if 'status' in fields:
                    status = fields['status']
                    status_meaning = {
                        0: "Stopped",
                        1: "Queued to verify local data",
                        2: "Verifying local data",
                        3: "Queued to download",
                        4: "Downloading",
                        5: "Queued to seed",
                        6: "Seeding"
                    }
                    if status in status_meaning:
                        print(f"status: {status} ({status_meaning[status]})")
                    else:
                        print(f"status: {status}")

                if 'percentDone' in fields:
                    percent_done = fields['percentDone'] * 100
                    print(f"percentDone: {percent_done:.2f}%")

                if 'totalSize' in fields:
                    total_size = fields['totalSize']
                    print(f"totalSize: {format_size(total_size)}")

                # Check if torrent file already exists locally
                torrent_file = fields.get('torrentFile')
                if torrent_file:
                    torrent_file_name = os.path.basename(torrent_file)
                    local_torrent_path = os.path.join(LOCAL_TORRENTS, torrent_file_name)
                    if os.path.exists(local_torrent_path):
                        print(f"Torrent {torrent_file_name} has already been transferred.")
                        continue  # Skip further processing for this torrent

                # Proceed with rsync command
                if 'downloadDir' in fields:
                    relative_dir = fields.get('downloadDir').replace(REMOTE_DIRECTORY, '').lstrip('/')
                    source = os.path.join(fields.get('downloadDir'), fields.get('name'))
                    destination = os.path.join(LOCAL_DIRECTORY, relative_dir, fields.get('name'))
                    command = ['rsync', '-avP', '--progress', '--stats', '--chown=1001:1001', source, destination]
                    print(f"Rsync command: {' '.join(command)}")

                    # Uncomment to execute rsync command
                    # subprocess.run(command, check=True)


                    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

                    logged_milestones = set()
                    num_files_transferred = None

                    for line in iter(process.stdout.readline, ''):
                        stripped_line = line.strip()
                        if any(skip_str in stripped_line for skip_str in skip_strings):
                            continue
                        match = pattern.match(stripped_line)
                        if not match:
                            logging.info(stripped_line)
                            if "Number of regular files transferred:" in stripped_line:
                                num_files_transferred = int(re.search(r'(\d+)', stripped_line).group(1))
                                if num_files_transferred == 0:
                                    logging.info("No files transferred from /seedbox to /data.")
                        else:
                            percentage = int(match.group(1))
                            milestone = within_tolerance(percentage, milestones, tolerance)
                            if milestone is not None and milestone not in logged_milestones:
                                logging.info(stripped_line)
                                logged_milestones.add(milestone)

                    for line in iter(process.stderr.readline, ''):
                        stripped_line = line.strip()
                        match = pattern.match(stripped_line)
                        if not match:
                            logging.error(stripped_line)
                        else:
                            percentage = int(match.group(1))
                            milestone = within_tolerance(percentage, milestones, tolerance)
                            if milestone is not None and milestone not in logged_milestones:
                                logging.error(stripped_line)
                                logged_milestones.add(milestone)

                    process.stdout.close()
                    process.stderr.close()
                    process.wait()

                print("")  # Separate torrents in the print output



except transmission_rpc.error.TransmissionError as e:
    logging.error(f"TransmissionError: {e}")
    print(f"TransmissionError: {e}")

except Exception as e:
    logging.error(f"Error: {e}")
    print(f"Error: {e}")
