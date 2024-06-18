import os
import requests
import logging
import subprocess
import re
import time
import threading

# Set up logging to output to stdout and stderr
# LOG_LEVEL May be set via a docker-compose environment variable - DEBUG, INFO, WARN, ERROR, CRITICAL
log_level = os.getenv('LOG_LEVEL', 'INFO')

# Convert the log level string to its corresponding constant from the logging module
log_level = getattr(logging, log_level.upper())

# Configure the logging with the dynamically set log level
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=log_level)

# Assign User and Group IDs for RSYNC from environment variables or default to 1001:1001
PUID = os.getenv('PUID', '1001')  # Default to '1001' if PUID is not set
GUID = os.getenv('GUID', '1001')  # Default to '1001' if GUID is not set

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

def transfer_torrents(transfer_title_torrent): #Transfers torrents for the linked title
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

            # Check status_message title directly for specific conditions
            if "Found archive file, might need to be extracted" in status_message.get('title', '') or messages == ['Sample']:
                logging.info(f"Potential rar file at {output_path}. Initiating unrar process.")
                if output_path:
                    try:
                        unrar_files(output_path)
                        files_processed = True
                    except Exception as e:
                        logging.error(f"Unrar error: {e}")

            elif "One or more episodes expected in this release were not imported" in status_message.get('title', ''):
                logging.info(f"The file {current_title} has an error but may also not have been properly transferred. Checking transfer now.")
                full_transfer(current_title, output_path)
                new_torrents = find_new_torrents()
                matching_torrents = [file for file in new_torrents if current_title in file]

                if not matching_torrents:
                    error_torrent_info = "There is no associated torrent to transfer."
                else:
                    error_torrent_info = f"The associated torrent {matching_torrents} has not been transferred."

                logging.info(f"The file {current_title} has an error not handled by this program. {error_torrent_info}")

            # Process each message individually
            for message in messages:
                if "No files found are eligible for import" in message:    
                    logging.info(f"Import error found for {current_title}. Rsync transfer initiated.")
                    full_transfer(current_title, output_path)

                elif "Found matching series via grab history, but release was matched to series by ID." in message:
                    logging.info(f"Potential sync error found for {current_title}. Rsync transfer initiated.")
                    full_transfer(current_title, output_path)
                    
                elif "Manual Import required." in message or "manual import required." in message.lower():
                    new_torrents = find_new_torrents()
                    matching_torrents = [file for file in new_torrents if current_title in file]

                    if not matching_torrents:
                        error_torrent_info = "There is no associated torrent to transfer."
                    else:
                        error_torrent_info = f"The associated torrent {matching_torrents} has not been transferred."

                    logging.info(f"The file {current_title} has an error not handled by this program. {error_torrent_info}")
                    
    return files_processed, downloading, downloading_titles

def process_records_dry_run(records, service):
    pending_tasks = []
    for record in records:
        current_title = record.get('title')
        status = record.get('status')
        output_path = record.get('outputPath', '')

        for status_message in record.get('statusMessages', []):
            messages = status_message.get('messages', [])

            if "Found archive file, might need to be extracted" in status_message.get('title', '') or messages == ['Sample']:
                pending_tasks.append({'type': 'unrar', 'title': current_title, 'path': output_path})
            elif "One or more episodes expected in this release were not imported" in status_message.get('title', ''):
                pending_tasks.append({'type': 'error', 'title': current_title, 'message': 'Error in import'})
            for message in messages:
                if "No files found are eligible for import" in message:    
                    pending_tasks.append({'type': 'transfer', 'title': current_title, 'path': output_path})
                elif "Found matching series via grab history, but release was matched to series by ID." in message:
                    pending_tasks.append({'type': 'error', 'message': 'Title Match required', 'title': current_title, 'path': output_path})
                elif "Manual Import required." in message or "manual import required." in message.lower():
                    pending_tasks.append({'type': 'manual_import', 'title': current_title, 'message': 'Manual import required'})
                    
    return pending_tasks

def full_transfer(current_title, output_path):
    logging.info(f"Starting full transfer for {current_title} to {output_path}")
    try:
        destination = output_path
        source = destination.replace('/data/', '/seedbox/')
        command, success = rsync_transfer(source, destination)
        if success:
            files_processed = True
            logging.info(f"Rsync transfer successful for {current_title}")
            transfer_torrents(current_title)
            
            # Check if there are .rar files after transfer
            if os.path.isdir(destination) and any('.rar' in file for file in os.listdir(destination)):
                logging.info("Found .rar files after transfer. Initiating unrar process.")
                unrar_files(destination)
        else:
            logging.warning(f"Rsync transfer failed or no files transferred for {current_title}")
    except Exception as e:
        logging.error(f"Error during transfer: {e}")
    
    logging.info(f"Completed full transfer for {current_title}")


def find_new_torrents():
    files_in_folder1 = set(file for file in os.listdir('/local/torrents') if file.endswith('.torrent'))
    files_in_folder2 = set(file for file in os.listdir('/torrents') if file.endswith('.torrent'))
    new_torrents = files_in_folder2 - files_in_folder1
    return new_torrents
    
def dry_run_loop():
    env_vars = {key: value for key, value in os.environ.items() if re.match(r'^[A-Z]+_API_(URL|KEY)$', key)}
    services = set(key.split('_API_')[0] for key in env_vars.keys())

    all_pending_tasks = []

    for service in services:
        logging.info(f"Dry run for service: {service}")
        api_url = env_vars.get(f"{service}_API_URL")
        api_key = env_vars.get(f"{service}_API_KEY")

        if api_url and api_key:
            response = api_request(api_url, api_key)
            if response:
                pending_tasks = process_records_dry_run(response.get('records', []), service)
                all_pending_tasks.extend(pending_tasks)
    
    new_torrents = find_new_torrents()
    all_pending_tasks.extend([{'type': 'torrent', 'file': file} for file in new_torrents])
    
    source = '/seedbox/'
    destination = '/data'
    exclude_dirs = [service.lower() for service in services]
    rsync_files = rsync_dry_run(source, destination, exclude_dirs)
    all_pending_tasks.extend([{'type': 'rsync', 'file': file} for file in rsync_files])

    logging.info("Dry run completed. Pending tasks:")
    for task in all_pending_tasks:
        logging.info(task)

    return all_pending_tasks

def main_loop():
    env_vars = {key: value for key, value in os.environ.items() if re.match(r'^[A-Z]+_API_(URL|KEY)$', key)} # Match environment variables that end with _API_URL or _API_KEY and capture the app name
    services = set(key.split('_API_')[0] for key in env_vars.keys())  # Extract unique service names from the matched environment variables

    for service in services:
        logging.info(f"Detected services to be processed: {service}")
   
    source = '/seedbox/'
    destination = '/data'
    exclude_dirs = [service.lower() for service in services]

    logging.info(f"Transferring data from {source} to {destination}. Transfers within {exclude_dirs} will be handled via API analysis.")
    
    while True:
        new_files_processed = False
        
        for service in services:
            api_url = env_vars.get(f"{service}_API_URL")
            api_key = env_vars.get(f"{service}_API_KEY")

            logging.info(f"Processing {service} at: {api_url}")

            if api_url and api_key:
                logging.debug(f"API Request sent to {service}.")
                response = api_request(api_url, api_key)
                
                if response:
                    logging.debug(f"API response received {response}.")
                    files_processed, downloading, downloading_titles = process_records(response.get('records', []), service)  # Adjusted to track service
                    if downloading_titles:
                        logging.info("Files being downloaded:")
                        for title in downloading_titles:
                            logging.info(f"{service} - {title}")
                    if files_processed:
                        new_files_processed = True
                else:
                    logging.warning(f"No response or empty response for {service}.")
            else:
                logging.warning(f"{service}_API_URL or {service}_API_KEY environment variables are not provided. Skipping {service}.")

        if not new_files_processed:
            try:
                rsync_seedbox_to_data_files_processed = rsync_transfer(source, destination, exclude_dirs)
            except Exception as e:
                logging.error(f"An error occurred during rsync: {e}")

                
        new_torrents = find_new_torrents()
        new_torrents = {file for file in new_torrents if not any(title in file for title in downloading_titles)}
        for file in new_torrents:
            logging.info(f"New torrent detected but not transferred: {file}")

        logging.info("Sleeping for 2 minutes before checking again...")
        time.sleep(120)
        downloading = False  # Reset downloading status before the next iteration
        downloading_titles = []  # Reset downloading titles list before the next iteration

if __name__ == "__main__":
    main_thread = threading.Thread(target=main_loop)
    dry_run_thread = threading.Thread(target=dry_run_loop)

    main_thread.start()
    dry_run_thread.start()

    main_thread.join()
    dry_run_thread.join()
