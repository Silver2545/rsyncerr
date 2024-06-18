import os
import requests
import logging
import subprocess
import re
import time
import threading

# Set up logging to output to stdout and stderr
log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, log_level, logging.INFO)
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=log_level)

# Assign User and Group IDs for RSYNC from environment variables or default to 1001:1001
PUID = os.getenv('PUID', '1001')
GUID = os.getenv('GUID', '1001')

def api_request(url, api_key):
    if not re.match(r'^https?://', url):
        url = 'http://' + url
    full_url = f"{url}/api/v3/queue?page=1&pageSize=100&includeUnknownMovieItems=true&includeMovie=false&apikey={api_key}"
    redacted_url = f"{url}/api/v3/queue?page=1&pageSize=100&includeUnknownMovieItems=true&includeMovie=false&apikey=API_KEY_REDACTED"
    logging.debug(f"Formatted URL: {redacted_url}")

    try:
        response = requests.get(full_url)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"API Request error: {e}")
        return None

def rsync_transfer(source, destination, exclude_dirs=[], milestones={0, 5, 25, 50, 75, 100}, tolerance=2):
    logged_milestones = set()

    if os.path.isdir(source):
        source += '/'
    if os.path.isdir(destination):
        destination += '/'
    command = ['rsync', '-avP', '--progress', '--stats', '--chown=' + PUID + ':' + GUID, source, destination]
    for exclude in exclude_dirs:
        command.extend(['--exclude', exclude])

    logging.info(f"Transferring files from {source} to {destination}")
    logging.debug(f"Running rsync command: {' '.join(command)}")

    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    pattern = re.compile(r'^\d{1,3}(?:,\d{3})*\s+(\d{1,3})%\s+\d+(\.\d+)?[kMG]B/s\s+(?:[0-8]?\d|9[0-8]):[0-5]\d:[0-5]\d$')

    def within_tolerance(value, milestones, tolerance):
        for milestone in milestones:
            if milestone - tolerance <= value <= milestone + tolerance:
                return milestone
        return None

    num_files_transferred = None
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
                    return command, False
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

    if process.returncode != 0:
        logging.error(f"Rsync failed with return code {process.returncode}")
        raise subprocess.CalledProcessError(process.returncode, command)

    if num_files_transferred is not None:
        logging.info(f"{num_files_transferred} files have been transferred from /seedbox to /data.")
        return command, True

    return command, False

def rsync_dry_run(source, destination, exclude_dirs=[]):
    if os.path.isdir(source):
        source += '/'
    if os.path.isdir(destination):
        destination += '/'

    command = ['rsync', '-avP', '--dry-run', '--progress', '--chown=' + PUID + ':' + GUID, '--stats', source, destination]
    for exclude in exclude_dirs:
        command.extend(['--exclude', exclude])

    logging.info(f"Performing rsync dry-run from {source} to {destination}")
    logging.debug(f"Running rsync dry-run command: {' '.join(command)}")

    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate()

        if process.returncode != 0:
            logging.error(f"Rsync dry-run failed with return code {process.returncode}")
            logging.error(stderr)
            return []

        file_list = []
        for line in stdout.split('\n'):
            if line.startswith('>f'):
                file_list.append(line[12:])

        return file_list
    except Exception as e:
        logging.error(f"Rsync dry-run error: {e}")
        return []

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

def transfer_torrents(transfer_title_torrent):
    new_torrents = find_new_torrents()
    matching_torrents = [file for file in new_torrents if transfer_title_torrent in file]
    logging.info(f"Matching torrents for '{transfer_title_torrent}': {matching_torrents}")
    for file in matching_torrents:
        src_file = os.path.join('/torrents', file)
        dest_file = os.path.join('/watch', file)
        rsync_command = ['rsync', '-avP', src_file, dest_file]
        logging.debug(f"Running torrent rsync command: {' '.join(rsync_command)}")

        try:
            process = subprocess.Popen(rsync_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for line in iter(process.stdout.readline, ''):
                logging.debug(line.strip())
            for line in iter(process.stderr.readline, ''):
                logging.error(line.strip())

            process.stdout.close()
            process.stderr.close()
            process.wait()
        except Exception as e:
            logging.error(f"Torrent rsync error: {e}")

def process_records(records, service):
    files_processed = False
    downloading = False
    downloading_titles = []
    current_title = None

    for record in records:
        current_title = record.get('title')
        status = record.get('status')
        output_path = record.get('outputPath', '')

        if status == 'downloading':
            downloading = True
            downloading_titles.append(current_title)

        for status_message in record.get('statusMessages', []):
            logging.debug(f"Examining status message: {status_message} for title {current_title}")

            messages = status_message.get('messages', [])
            logging.debug(f"Messages list: {messages}")

            if "Found archive file, might need to be extracted" in status_message.get('title',
