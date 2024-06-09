import os
import requests
import logging
import subprocess
import re
import time

# Set up logging to output to stdout and stderr
# LOG_LEVEL May be set via a docker-compose environment variable - DEBUG, INFO, WARN, ERROR, CRITICAL
log_level = os.getenv('LOG_LEVEL', 'INFO')

# Convert the log level string to its corresponding constant from the logging module
log_level = getattr(logging, log_level.upper())

# Configure the logging with the dynamically set log level
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=log_level)

def api_request(url, api_key):
    if not re.match(r'^https?://', url):
        url = 'http://' + url
    full_url = f"{url}/api/v3/queue?page=1&pageSize=10&includeUnknownMovieItems=false&includeMovie=false&apikey={api_key}"
    redacted_url = f"{url}/api/v3/queue?page=1&pageSize=10&includeUnknownMovieItems=false&includeMovie=false&apikey=API_KEY_REDACTED"
    logging.debug(f"Formatted URL: {redacted_url}")

    try:
        response = requests.get(full_url)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"API Request error: {e}")
        return None


def transfer_file(source, destination):
    milestones = {0, 25, 50, 75, 100}
    logged_milestones = set()
    if os.path.isdir(source):
        source += '/'
    if os.path.isdir(destination):
        destination += '/'
    
    command = ['rsync', '-av', '--progress', source, destination]
    logging.info(f"Running rsync command: {' '.join(command)}")
    
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    pattern = re.compile(r'^\d{1,3}(?:,\d{3})*\s+(\d{1,3})%\s+\d+(\.\d+)?[kMG]B/s\s+(?:[0-8]?\d|9[0-8]):[0-5]\d:[0-5]\d$')

    for line in iter(process.stdout.readline, ''):
        stripped_line = line.strip()
        match = pattern.match(stripped_line)
        if not match:
            logging.info(stripped_line)
        else:
            percentage = int(match.group(1))
            if percentage in milestones and percentage not in logged_milestones:
                logging.info(stripped_line)
                logged_milestones.add(percentage)

    for line in iter(process.stderr.readline, ''):
        stripped_line = line.strip()
        match = pattern.match(stripped_line)
        if not match:
            logging.error(stripped_line)
        else:
            percentage = int(match.group(1))
            if percentage in milestones and percentage not in logged_milestones:
                logging.error(stripped_line)
                logged_milestones.add(percentage)

    process.stdout.close()
    process.stderr.close()
    process.wait()

    if process.returncode != 0:
        logging.error(f"Rsync failed with return code {process.returncode}")
        raise subprocess.CalledProcessError(process.returncode, command)

    return process.returncode == 0

def rsync_transfer(source, destination, exclude_dirs=[]):
    command = ['rsync', '-avP', '--stats', source, destination]  # Add --stats option
    for exclude in exclude_dirs:
        command.extend(['--exclude', exclude])
    
    logging.info(f"Running rsync command: {' '.join(command)}")
    
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        # Log the output of rsync while it's running
        for line in iter(process.stdout.readline, ''):
            logging.debug(line.strip())
            if "Number of regular files transferred:" in line:
                num_files_transferred = int(re.search(r'(\d+)', line).group(1))  # Extract the number of files transferred
                if num_files_transferred == 0:
                    logging.info("No files transferred from /seedbox to /data.")
                    return False  # Set the flag to indicate no files were transferred
        
        for line in iter(process.stderr.readline, ''):
            logging.error(line.strip())

        process.wait()  # Wait for the process to finish

        # If the loop completes without finding the 0 condition, files have been transferred
        if num_files_transferred is not None:
            logging.info(f"{num_files_transferred} files have been transferred from /seedbox to /data.")
        return True  # Set the flag to indicate files were transferred

    except Exception as e:
        logging.error(f"Rsync error: {e}")
        raise


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

def process_records(records, service):
    files_processed = False
    downloading = False
    downloading_titles = []
    current_title = None
    for record in records:
        current_title = record.get('title')
        if (record.get('status') == 'downloading'):
            downloading = True
            downloading_titles.append(record.get('title', None))
 
        for status_message in record.get('statusMessages', []):
            logging.debug(f"Examining status message: {status_message}")

            messages = status_message.get('messages', [])
            logging.debug(f"Messages list: {messages}")

            for message in messages:
                if "No files found are eligible for import" in message:
                    match = re.search(r'in (/.+)$', message)
                    if match:
                        destination = match.group(1)
                        source = destination.replace('/data/', '/seedbox/')
                        logging.info(f"Import error found for {destination}. Rsync transfer initiated.")
                        try:
                            if transfer_file(source, destination):
                                files_processed = True
                                # Find new torrents and filter by the current title
                                new_torrents = find_new_torrents()
                                matching_torrents = [file for file in new_torrents if current_title in file]
                                logging.info(f"Matching torrents for '{current_title}': {matching_torrents}")

                                # Rsync matching torrents to the /watch directory
                                for file in matching_torrents:
                                    src_file = os.path.join('/torrents', file)
                                    dest_file = os.path.join('/watch', file)
                                    rsync_command = ['rsync', '-avP', src_file, dest_file]
                                    logging.info(f"Running torrent rsync command: {' '.join(rsync_command)}")
                                    
                                    process = subprocess.Popen(rsync_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                                    for line in iter(process.stdout.readline, ''):
                                        logging.info(line.strip())
                                    for line in iter(process.stderr.readline, ''):
                                        logging.error(line.strip())

                                    process.stdout.close()
                                    process.stderr.close()
                                    process.wait()

                                # Check if there are .rar files after transfer
                                if any('.rar' in file for file in os.listdir(destination)):
                                    logging.info("Found .rar files after transfer. Initiating unrar process.")
                                    unrar_files(destination)
                        except Exception as e:
                            logging.error(f"Rsync error: {e}")
                    break
                elif "Found archive file, might need to be extracted" in message:
                    output_path = record.get('outputPath', '')
                    if output_path:
                        logging.info(f"Found archive file at {output_path}. Initiating unrar process.")
                        try:
                            unrar_files(output_path)
                            files_processed = True
                        except Exception as e:
                            logging.error(f"Unrar error: {e}")
                # Additional condition to always check for .rar files in the outputPath
                elif messages == ['Sample']:
                    output_path = record.get('outputPath', '')
                    if output_path and any('.rar' in file for file in os.listdir(output_path)):
                        logging.info(f"Found .rar files in {output_path}. Initiating unrar process.")
                        try:
                            unrar_files(output_path)
                            files_processed = True
                        except Exception as e:
                            logging.error(f"Unrar error: {e}")
    return files_processed, downloading, downloading_titles

def find_new_torrents():
    files_in_folder1 = set(file for file in os.listdir('/local/torrents') if file.endswith('.torrent'))
    files_in_folder2 = set(file for file in os.listdir('/torrents') if file.endswith('.torrent'))
    new_torrents = files_in_folder2 - files_in_folder1
    return new_torrents

def main():
    env_vars = {key: value for key, value in os.environ.items() if re.match(r'^(RADARR|SONARR|LIDARR|READARR)_API_(URL|KEY)$', key)}

    source = '/seedbox/'
    destination = '/data'
    exclude_dirs = ['sonarr', 'radarr']

    services = set(key.split('_API_')[0] for key in env_vars.keys())

    logging.info(f"Services to process: {services}")
    downloading = False  # Reset downloading status before processing services
    downloading_titles = []  # Reset downloading titles list before processing services
    while True:
        new_files_processed = False
        for service in services:
            api_url = env_vars.get(f"{service}_API_URL")
            api_key = env_vars.get(f"{service}_API_KEY")

            logging.info(f"Processing {service} with URL: {api_url}")

            if api_url and api_key:
                logging.debug(f"API Request sent to {service}.")
                response = api_request(api_url, api_key)
                if response:
                    logging.debug(f"API response received {response}.")
                    files_processed, downloading, downloading_titles = process_records(response.get('records', []), service) #Adjusted to track service
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
                if not rsync_seedbox_to_data_files_processed:
                    new_torrents = find_new_torrents()
                    new_torrents = {file for file in new_torrents if not any(title in file for title in downloading_titles)}
                    for file in new_torrents:
                         logging.info(f"New torrent detected but not transferred: {file}")
#                        src_file = os.path.join('/torrents', file)
#                        dest_file = os.path.join('/watch', file)
#                        rsync_command = ['rsync', '-avP', src_file, dest_file]
#                        print("Rsync command:", ' '.join(rsync_command))
#                        logging.info(f"Running torrent rsync command: {' '.join(rsync_command)}")
#                        process = subprocess.Popen(rsync_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
#                        for line in iter(process.stdout.readline, ''):
#                            logging.info(line.strip())
#                        for line in iter(process.stderr.readline, ''):
#                            logging.error(line.strip())
#
#                        process.stdout.close()
#                        process.stderr.close()
#                        process.wait()
            except Exception as e:
                logging.error(f"An error occurred during rsync: {e}")

        logging.info("Sleeping for 5 minutes before checking again...")
        time.sleep(300)
        downloading = False  # Reset downloading status before the next iteration
        downloading_titles = []  # Reset downloading titles list before the next iteration

if __name__ == "__main__":
    main()
