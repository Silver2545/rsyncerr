import os
import logging
import subprocess
import re
import shlex
import time
from transmission_rpc import Client, TransmissionError

# Configure logging
logging.basicConfig(filename='rsyncerr.log', level=logging.INFO, format='%(asctime)s - %(message)s')

# Configure logging to log to both file and stdout
log_formatter = logging.Formatter('%(asctime)s - %(message)s')

# File Handler
file_handler = logging.FileHandler('transmission_log.txt')
file_handler.setFormatter(log_formatter)

# Stream Handler for stdout
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)

# Get the root logger and set the level
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(stream_handler)

# Set Environment Variables or use defaults
REMOTE_HOST = os.getenv('REMOTE_HOST')
REMOTE_PORT = int(os.getenv('REMOTE_PORT', 9091))  # Default Transmission port
REMOTE_USERNAME = os.getenv('REMOTE_USERNAME', 'transmission')  # Default Transmission username
REMOTE_PASSWORD = os.getenv('REMOTE_PASSWORD', 'password')  # Default Transmission password
REMOTE_PROTOCOL = os.getenv('REMOTE_PROTOCOL', 'https')  # Default to https
REMOTE_DIRECTORY = os.getenv('REMOTE_DIRECTORY', '/downloads')  # This is where the Remote version of Transmission sees files it downloads
LOCAL_DIRECTORY = os.getenv('LOCAL_DIRECTORY', '/data')
LOCAL_HOST = os.getenv('LOCAL_HOST', '192.168.0.100')
LOCAL_PORT = int(os.getenv('LOCAL_PORT', 9091))  # Default Transmission port
LOCAL_USERNAME = os.getenv('LOCAL_USERNAME', 'transmission')  # Default Transmission username
LOCAL_PASSWORD = os.getenv('LOCAL_PASSWORD', 'password')  # Default Transmission password
PUID = os.getenv('PUID', '1001')
GUID = os.getenv('GUID', '1001')

# Connect to the Local Transmission instance
local = Client(
    host=LOCAL_HOST,
    port=LOCAL_PORT,
    username=LOCAL_USERNAME,
    password=LOCAL_PASSWORD,
)

# Connect to the Remote Transmission instance
remote = Client(
    host=REMOTE_HOST,
    port=REMOTE_PORT,
    username=REMOTE_USERNAME,
    password=REMOTE_PASSWORD,
    protocol=REMOTE_PROTOCOL
)

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

def log_torrent_info():
    try:
        # Get list of torrents
        torrents = remote.get_torrents()

        if torrents:
            first_torrent = torrents[0]  # Assuming at least one torrent is available

            # Log torrent information
            logging.info("Torrent Information:")
            for key, value in first_torrent.__dict__['fields'].items():
                logging.info(f"{key}: {value}")
        else:
            logging.warning("No torrents found in Transmission.")

    except TransmissionError as e:
        logging.error(f"Error fetching torrent information: {e}")

def access_local():
    localTorrentList = []
    local_torrents = local.get_torrents()
    for torrent in local_torrents:
        if 'fields' in torrent.__dict__:
            fields = torrent.__dict__['fields']
            if 'torrentFile' in fields:
                torrentFilePath = fields['torrentFile']
                torrentFileName = os.path.basename(torrentFilePath)
                percent_done = fields.get('percentDone', 0) * 100
                status = fields.get('status', 7)
                error = fields.get('error', 0)
                errorString = fields.get('errorString', '')
                downloadDir = fields.get('downloadDir', '')
                name = fields.get('name', 'Unknown')
                info_hash = fields.get('hashString', '')  # Get the info_hash
                localTorrentList.append({
                    'torrent_file': torrentFileName,
                    'percent_done': percent_done,
                    'status': status,
                    'error': error,
                    'error_string': errorString,
                    'download_dir': downloadDir,
                    'name': name,
                    'info_hash': info_hash
                })
    # logging.info(f"{localTorrentList}")
    return localTorrentList
    
def process_local_torrents():
    local_torrents = local.get_torrents()
    for torrent in local_torrents:
        if 'fields' in torrent.__dict__:
            fields = torrent.__dict__['fields']
            percent_done = fields.get('percentDone', 0) * 100
            status = fields.get('status', 7)
            name = fields.get('name', 'Unknown')
            info_hash = fields.get('hashString', '')  # Get the info_hash
            error_string = fields.get('errorString', '')
            downloadDir = fields.get('downloadDir', '')  # Ensure we use the same variable name

            # Log warning for paused torrents at 0% completion
            if status == 0 and percent_done == 0:
                logging.warning(f"Local Torrent is paused with no data: {name}")

            # Resume torrents that are fully downloaded and paused
            elif status == 0 and percent_done >= 100:
                try:
                    local.start_torrent(info_hash)  # Resume using info_hash
                    logging.info(f"Resumed torrent: {name}")
                except TransmissionError as e:
                    logging.error(f"Error resuming torrent: {name}, Error: {e}")

            # Pause torrents with error "Stopped peer doesn't exist"
            elif error_string == "Stopped peer doesn't exist":
                try:
                    local.stop_torrent(info_hash)  # Pause using info_hash
                    logging.info(f"Torrent paused to clear error: {name}")
                except TransmissionError as e:
                    logging.error(f"Error stopping torrent: {name}, Error: {e}")

            # Handle torrents with downloadDir set to /data/completed and 0% completion
            elif downloadDir == "/data/completed" and percent_done == 0:
                files = fields.get('files', [])
                for file in files:
                    file_name = os.path.basename(file['name'])
                    find_command = f'find /data -type f -name "{file_name}"'
                    process = subprocess.Popen(find_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    stdout, stderr = process.communicate()

                    if stdout:
                        new_location = os.path.dirname(stdout.strip())
                        logging.info(f"File found for {name}: {stdout.strip()}")
                        try:
                            torrent.locate_data(new_location)
                            logging.info(f"Download directory for torrent {name} updated to {new_location}")
                        except TransmissionError as e:
                            logging.error(f"Error updating download directory for {name}: {e}")
                        break
                    else:
                        logging.warning(f"File not found for {name}: {file_name}")
                        

def check_remote_torrents(localTorrentList):
    remote_torrents_info = []

    remote_torrents = remote.get_torrents()
    for idx, torrent in enumerate(remote_torrents, start=1):
        if 'fields' in torrent.__dict__:
            fields = torrent.__dict__['fields']

            remoteTorrentName = fields.get('name', 'Unknown')
            status = fields.get('status', 'Unknown')
            percent_done = fields.get('percentDone', 0) * 100
            total_size = fields.get('totalSize', 0)
            relativeDir = fields.get('downloadDir', '').replace(REMOTE_DIRECTORY, '').lstrip('/')
            remoteTorrentFilePath = fields.get('torrentFile', '')
            remoteTorrentFileName = os.path.basename(remoteTorrentFilePath)

            # Check if the torrent file name (without extension) exists in localTorrentList
            if any(torrent['torrent_file'] == remoteTorrentFileName for torrent in localTorrentList):
                logging.info(f"{remoteTorrentName} has already been transferred to the local server.")
                continue

            # Check if torrent is fully downloaded and seeding (status 6)
            if percent_done >= 100 and status == 6:
                torrent_info = {
                    'name': remoteTorrentName,
                    'status': status,
                    'percent_done': percent_done,
                    'total_size': total_size,
                    'relative_dir': relativeDir,
                    'remote_torrent_file_path': remoteTorrentFilePath,
                    'remote_torrent_file_name': remoteTorrentFileName
                }
                logging.info(f"Adding torrent to transfer list: {torrent_info}")
                remote_torrents_info.append(torrent_info)
            else:
                logging.info(f"Torrent {remoteTorrentName} is not yet ready for transfer (Status: {status}, Progress: {percent_done}%)")

    return remote_torrents_info

def transfer_torrent(remoteTorrentFilePath, relativeDir, torrentFileName):
    try:
        with open(remoteTorrentFilePath, 'rb') as f:
            torrent_content = f.read()

        downloadDir = os.path.join(LOCAL_DIRECTORY, relativeDir)
        os.makedirs(downloadDir, exist_ok=True)

        local.add_torrent(torrent_content, paused=True, download_dir=downloadDir)
        logging.info(f"Torrent added successfully: {torrentFileName} to {downloadDir}")
        return True

    except Exception as e:
        logging.error(f"Error adding torrent: {torrentFileName}, Error: {e}")
        return False

def unrar_files(directory):
    rar_files = [f for f in os.listdir(directory) if f.endswith('.rar')]
    if rar_files:
        try:
            for rar_file in rar_files:
                rar_path = os.path.join(directory, rar_file)
                subprocess.run(['unrar-free', 'e', rar_path, directory], check=True)
                logging.info(f"Unrar completed for {rar_file}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Unrar error: {e}")

def transfer_files(remote_torrents_info):
    for torrent_info in remote_torrents_info:
        source = os.path.join(REMOTE_DIRECTORY, torrent_info['relative_dir'], torrent_info['name'])
        destination = os.path.join(LOCAL_DIRECTORY, torrent_info['relative_dir'], torrent_info['name'])

        # Handle a directory being transferred properly
        if os.path.isdir(source):
            source += '/'
        if os.path.isdir(destination):
            destination += '/'

#        # Quote files and directories as needed to prevent errors
#        source = shlex.quote(source)
#        destination = shlex.quote(destination)
        # Always use double quotes around the paths
        source = f'"{source}"'
        destination = f'"{destination}"'

        # Create needed directories for rsync
        destination_dir = os.path.dirname(destination.strip('"'))  # Remove quotes for os.path functions
        if not os.path.exists(destination_dir):
            os.makedirs(destination_dir)
            os.chown(destination_dir, int(PUID), int(GUID))


        rsync_command = f"rsync -avP --progress --stats --chown={PUID}:{GUID} {source} {destination}"

        logging.info(f"Rsync command: {rsync_command}")

        process = subprocess.Popen(rsync_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        pattern = re.compile(r'^\d{1,3}(?:,\d{3})*\s+(\d{1,3})%\s+\d+(\.\d+)?[kMG]B/s\s+(?:[0-8]?\d|9[0-8]):[0-5]\d:[0-5]\d$')
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
                        logging.info("No files transferred from Remote to Local.")
                        # return False  # Because we're adding torrents paused, it should be unnecessary to skip transferring the torrent and may address edge cases where the process is interrupted.
            else:
                percentage = int(match.group(1))
                milestone = within_tolerance(percentage, milestones, tolerance)
                if milestone is not None and milestone not in logged_milestones:
                    logging.info(f"{stripped_line} ({milestone}%)")
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
                    logging.error(f"{stripped_line} ({milestone}%)")
                    logged_milestones.add(milestone)

        process.stdout.close()
        process.stderr.close()
        process.wait()

        if process.returncode != 0:
            logging.error(f"Rsync failed with return code {process.returncode}")
            logging.error(f"Failed rsync command: {rsync_command}")
            continue  # Skip to the next torrent
            raise subprocess.CalledProcessError(process.returncode, rsync_command)

        logging.info(f"{num_files_transferred} files have been transferred from Remote to Local. Now transferring the .torrent file")
        transfer_torrent(torrent_info['remote_torrent_file_path'], torrent_info['relative_dir'], torrent_info['remote_torrent_file_name'])

        # Check and unrar files if .rar files are present
        destination_dir = os.path.join(LOCAL_DIRECTORY, torrent_info['relative_dir'])
        unrar_files(destination_dir)

    return True

def main():
#    log_torrent_info() # Activate when needing to check a single torrent to provide information on available fields
    local_torrent_list = access_local()
    process_local_torrents()
    remote_torrents_info = check_remote_torrents(local_torrent_list)
    transfer_files(remote_torrents_info)

if __name__ == "__main__":
    while True:
        main()
        logging.info(f"Sleeping for 1 minute before resuming")
        time.sleep(60)

