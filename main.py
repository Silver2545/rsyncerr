import os
import logging
from logging.handlers import RotatingFileHandler
import subprocess
import re
import shlex
import time
from transmission_rpc import Client, TransmissionError

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
os.environ['TIMEZONE'] = os.getenv('TIMEZONE', 'UTC')  # Default to UTC if TZ is not set
time.tzset()

# Define the log file and rotation settings
log_file = 'rsyncerr.log'
max_log_size = 10 * 1024 * 1024  # 10 MB
backup_count = 5

# Get the log level from the environment variable, default to INFO if not set
log_level_env = os.getenv('LOG_LEVEL', 'INFO').upper()
log_levels = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL
}
log_level = log_levels.get(log_level_env, logging.INFO)

# Create a rotating file handler
file_handler = RotatingFileHandler(log_file, maxBytes=max_log_size, backupCount=backup_count)
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(log_formatter)

# Stream Handler for stdout
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)

# Get the root logger and set the level
logger = logging.getLogger()
logger.setLevel(log_level)
logger.addHandler(file_handler)
logger.addHandler(stream_handler)

# Connect to the Local Transmission instance
try:
    local = Client(
        host=LOCAL_HOST,
        port=LOCAL_PORT,
        username=LOCAL_USERNAME,
        password=LOCAL_PASSWORD
    )
    logging.info("Successfully connected to the local Transmission instance.")
except TransmissionError as e:
    logging.error(f"Failed to connect to the local Transmission instance: {e}")
except Exception as e:
    logging.error(f"An unexpected error occurred: {e}")

# Connect to the Remote Transmission instance
try:
    remote = Client(
        host=REMOTE_HOST,
        port=REMOTE_PORT,
        username=REMOTE_USERNAME,
        password=REMOTE_PASSWORD,
		protocol=REMOTE_PROTOCOL
    )
    logging.info("Successfully connected to the remote Transmission instance.")
except TransmissionError as e:
    logging.error(f"Failed to connect to the remote Transmission instance: {e}")
except Exception as e:
    logging.error(f"An unexpected error occurred: {e}")

# Strings to be skipped during rsync output
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

# Unused process but useful to list keys and values for actions
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

# Obtain the current list of all local torrents
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
    logging.debug(f"{localTorrentList}")
    return localTorrentList

# Locate the downloaded files for a torrent that has either been moved or had the incorrect location set
def find_torrent_location(full_name):
    file_name = os.path.basename(full_name)
    find_command = f'find /data -type f -name "{file_name}"'
    process = subprocess.Popen(find_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = process.communicate()

    if stdout:
        new_location = stdout.strip()
        new_location = new_location.replace(full_name, '')  # Handle individual and multi-pack files
        new_location = new_location.rstrip('/')
        logging.info(f"File found: {stdout.strip()}")
    else:
        new_location = None
        logging.warning(f"File not found: {file_name}")

    return new_location

# Correct the location for a torrent that has either been moved or had the incorrect location set
def change_torrent_location(client, torrent_ids, new_location):
    try:
        client.change_torrent(
            ids=torrent_ids,
            location=new_location
        )
        print(f"Changed location of torrent(s) {torrent_ids} to {new_location}")
    except transmission_rpc.TransmissionError as e:
        print(f"Error changing location of torrent(s) {torrent_ids}: {e}")


def process_local_torrents():
# Unused but if uncommented allow a list of all available commands for Transmission
#    local_client_methods = [method for method in dir(local) if callable(getattr(local, method))]
#    print("Methods of local client object:", local_client_methods)

#    local_client_attributes = local.__dict__
#    print("Attributes of local client object:", local_client_attributes)

    local_torrents = local.get_torrents()
    for torrent in local_torrents:
        if 'fields' in torrent.__dict__:
            fields = torrent.__dict__['fields']
            percent_done = fields.get('percentDone', 0) * 100
            status = fields.get('status', 7)
            name = fields.get('name', 'Unknown')
            info_hash = fields.get('hashString', '')
            error_string = fields.get('errorString', '')
            downloadDir = fields.get('downloadDir', '')

            logging.debug(f"Working on torrent {name}. Percent completed: {percent_done}. Status: {status} Error: {error_string} File location: {downloadDir}")

#            # Log warning for paused torrents at 0% completion
#            if status == 0 and percent_done == 0:
#                logging.warning(f"Local Torrent is paused with no data: {name}")


            # Resume torrents that are fully downloaded and paused
            if status == 0 and percent_done >= 100:
                try:
                    logging.info(f"Resuming torrent: {name}")
                    local.start_torrent(info_hash)  # Resume using info_hash
                except TransmissionError as e:
                    logging.error(f"Error resuming torrent: {name}, Error: {e}")

            # Pause torrents with error "Stopped peer doesn't exist"
            if "Stopped peer doesn't exist" in error_string:
                try:
                    logging.info(f"Torrent paused to clear error: {name}")
                    local.stop_torrent(info_hash)  # Pause using info_hash
                except TransmissionError as e:
                    logging.error(f"Error stopping torrent: {name}, Error: {e}")

            # Torrents either with no downloaded data or "No data found!" error likely need located
            if status not in [1, 2] and ((percent_done == 0) or ("No data found!" in error_string)): # Adjusted to account for torrents verifying or queued to verify
#            if (percent_done == 0) or ("No data found!" in error_string):
                logging.info(f"Torrent {name} has downloaded {percent_done}%. {error_string} Attempting to correct.")
                files = fields.get('files', [])
                if files:
                    # Find the largest file to avoid potential duplicate named sample or nfo files
                    largest_file = max(files, key=lambda f: f['length'])
                    full_name = largest_file['name']  # Get the full path from the largest file dictionary
                    file_name = os.path.basename(full_name)
                    find_command = f'find {LOCAL_DIRECTORY} -type f -name "{file_name}"'
                    process = subprocess.Popen(find_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    stdout, stderr = process.communicate()

                    if stdout:
                        found_file_path = stdout.strip()
                        new_location = found_file_path.replace(full_name, '').rstrip('/')
                        try:
                            local.move_torrent_data(info_hash, new_location)
                            logging.info(f"Download directory for torrent {name} updated to {new_location}")
                            local.verify_torrent(info_hash) #Start the verification process on the newly moved torrent
                        except TransmissionError as e:
                            logging.error(f"Error updating download directory for {name}: {e}")
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
            remoteErrorString = fields.get('errorString', '')
            # Check if the torrent file name (without extension) exists in localTorrentList
            if any(torrent['torrent_file'] == remoteTorrentFileName for torrent in localTorrentList):
                logging.debug(f"{remoteTorrentName} has already been transferred to the local server.")
                continue

            # Check for new "too many open files" error.
            if "Too many open save files" in remoteErrorString:
                try:
                    logging.info(f"Torrent restarted to clear error: {name}")
                    remote.restart_torrent(info_hash)  # Pause using info_hash
                except TransmissionError as e:
                    logging.error(f"Error restarting torrent: {name}, Error: {e}")


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
                logging.info(f"Adding torrent to transfer list: {remoteTorrentName}")
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
        logging.debug(f"remoteTorrentFilePath: {remoteTorrentFilePath}")
        logging.debug(f"LOCAL_DIRECTORY: {LOCAL_DIRECTORY}")
        logging.debug(f"relativeDir: {relativeDir}")
        logging.debug(f"downloadDir: {downloadDir}")
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
                subprocess.run(['unrar', 'e', rar_path, directory], check=True)
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
        rsync_source = f'"{source}"'
        rsync_destination = f'"{destination}"'

        # Create needed directories for rsync
        destination_dir = os.path.dirname(destination.strip('"'))  # Remove quotes for os.path functions
#        logging.info(f"destination is {destination} for future unrar info")
#        logging.info(f"destination_dir is {destination_dir} for future unrar info")
        if not os.path.exists(destination_dir):
            os.makedirs(destination_dir)
            os.chown(destination_dir, int(PUID), int(GUID))


        rsync_command = f"rsync -avP --progress --stats --chown={PUID}:{GUID} {rsync_source} {rsync_destination}"

        logging.debug(f"Rsync command: {rsync_command}")

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


        if os.path.isdir(destination):
            logging.debug(f"Checking {destination} for potential rar files to be un-rared")
            unrar_files(destination)
        else:
            logging.debug(f"{destination} not being checked for rar files. Not a directory.")

        if process.returncode != 0:
            logging.error(f"Rsync failed with return code {process.returncode}")
            logging.error(f"Failed rsync command: {rsync_command}")
            continue  # Skip to the next torrent
            raise subprocess.CalledProcessError(process.returncode, rsync_command)

        logging.info(f"{num_files_transferred} files have been transferred from Remote to Local. Now transferring the .torrent file")
        logging.debug(f"Attempting to transfer torrent with the following details:\n"
                      f"  remote_torrent_file_path: {torrent_info['remote_torrent_file_path']}\n"
                      f"  relative_dir: {torrent_info['relative_dir']}\n"
                      f"  remote_torrent_file_name: {torrent_info['remote_torrent_file_name']}")
        transfer_torrent(torrent_info['remote_torrent_file_path'], torrent_info['relative_dir'], torrent_info['remote_torrent_file_name'])

#        # Check and unrar files if .rar files are present
#        destination_dir = os.path.join(LOCAL_DIRECTORY, torrent_info['relative_dir'])
#        unrar_files(destination_dir)

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
        logging.info(f"Sleeping for 5 minutes before resuming")
        time.sleep(600)


