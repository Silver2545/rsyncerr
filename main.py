import os
import requests
import logging
import subprocess
import re
import time

# Set up logging to output to stdout and stderr
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

def api_request(url, api_key):
    if not re.match(r'^https?://', url):
        url = 'http://' + url
    full_url = f"{url}/api/v3/queue?page=1&pageSize=10&includeUnknownMovieItems=false&includeMovie=false&apikey={api_key}"
    logging.info(f"Formatted URL: {full_url}")

    try:
        response = requests.get(full_url)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"API Request error: {e}")
        return None

def transfer_file(source, destination):
    if os.path.isdir(source):
        source += '/'
    if os.path.isdir(destination):
        destination += '/'
    
    command = ['rsync', '-av', '--progress', '--chown=1001:1001', source, destination]
    logging.info(f"Running rsync command: {' '.join(command)}")
    
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    
    for line in iter(process.stdout.readline, ''):
        logging.info(line.strip())
    for line in iter(process.stderr.readline, ''):
        logging.error(line.strip())

    process.stdout.close()
    process.stderr.close()
    process.wait()

    if process.returncode != 0:
        logging.error(f"Rsync failed with return code {process.returncode}")
        raise subprocess.CalledProcessError(process.returncode, command)

    return process.returncode == 0


def rsync_transfer(source, destination, exclude_dirs=[]):
    command = ['rsync', '-avP', '--chown=1001:1001', '--stats', source, destination]  # Add --stats option
    for exclude in exclude_dirs:
        command.extend(['--exclude', exclude])
    
    logging.info(f"Running rsync command: {' '.join(command)}")
    
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        # Log the output of rsync while it's running
        for line in iter(process.stdout.readline, ''):
            logging.info(line.strip())
            if "Number of regular files transferred: 0" in line:  # Check within the loop
                logging.info("No files transferred from /seedbox to /data.")
                return False  # Set the flag to indicate no files were transferred
        
        for line in iter(process.stderr.readline, ''):
            logging.error(line.strip())

        process.wait()  # Wait for the process to finish

        # If the loop completes without finding the condition, files have been transferred
        logging.info("Files have been transferred from /seedbox to /data.")
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

def process_records(records):
    files_processed = False
    for record in records:
        for status_message in record.get('statusMessages', []):
            logging.info(f"Examining status message: {status_message}")

            messages = status_message.get('messages', [])
            logging.info(f"Messages list: {messages}")

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
    return files_processed

def main():
    env_vars = {key: value for key, value in os.environ.items() if re.match(r'^(RADARR|SONARR|LIDARR|READARR)_API_(URL|KEY)$', key)}

    source = '/seedbox/'
    destination = '/data'
    exclude_dirs = ['sonarr', 'radarr']

    logging.info(f"Environment variables: {env_vars}")

    services = set(key.split('_API_')[0] for key in env_vars.keys())

    logging.info(f"Services to process: {services}")
    while True:
        new_files_processed = False

        for service in services:
            api_url = env_vars.get(f"{service}_API_URL")
            api_key = env_vars.get(f"{service}_API_KEY")

            logging.info(f"Processing {service} with URL: {api_url} and Key: {api_key}")

            if api_url and api_key:
                logging.info(f"API Request sent to {service}.")
                response = api_request(api_url, api_key)
                if response:
                    logging.info(f"API response received {response}.")
                    files_processed = process_records(response.get('records', []))
                    if files_processed:
                        new_files_processed = True
                else:
                    logging.warning(f"No response or empty response for {service}.")
            else:
                logging.warning(f"{service}_API_URL or {service}_API_KEY environment variables are not provided. Skipping {service}.")

        if not new_files_processed:
            logging.info("No new files processed. Running rsync with /seedbox/ to /data.")
            try:
                rsync_seedbox_to_data_files_processed = rsync_transfer(source, destination, exclude_dirs)
                if not rsync_seedbox_to_data_files_processed:
#                    command = ['rsync', '-avP', '--chown=1001:1001', '/torrents/', '/watch']
                    command = ['rsync', '-avP', '--chown=1001:1001', '--detect-renamed', '/torrents/', '/watch']
                    logging.info(f"No files were transferred from /seedbox to /data. Running torrent rsync command: {' '.join(command)}")
                    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for line in iter(process.stdout.readline, ''):
                        logging.info(line.strip())
                    for line in iter(process.stderr.readline, ''):
                        logging.error(line.strip())

                    process.stdout.close()
                    process.stderr.close()
                    process.wait()
                    
            except Exception as e:
                logging.error(f"An error occurred during rsync: {e}")

        logging.info("Sleeping for 5 minutes before checking again...")
        time.sleep(300)

if __name__ == "__main__":
    main()


