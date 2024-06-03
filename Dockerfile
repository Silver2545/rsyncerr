FROM python:3.9-slim

# Install necessary packages
RUN apt-get update && apt-get install -y \
    unrar \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the monitoring script
COPY monitor.py /app/monitor.py

# Set the working directory
WORKDIR /app

# Run the monitoring script
CMD ["python", "monitor.py"]

