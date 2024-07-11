FROM python:3.9-slim

# Install necessary packages
RUN apt-get update && \
    apt-get install -y rsync && \
    apt-get install -y unrar-free && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir transmission-rpc==7.0.10

# Copy the monitoring script
COPY main.py /app/main.py

# Set the working directory
WORKDIR /app

# Run the monitoring script
CMD ["python", "main.py"]
