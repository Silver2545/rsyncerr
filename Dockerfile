# Stage 1: Get unrar binary from linuxserver/unrar image
FROM linuxserver/unrar as unrar

# Stage 2: Build the main image
FROM python:3.9-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Set working directory
WORKDIR /app

# Copy the unrar binary from the unrar stage
COPY --from=unrar /usr/bin/unrar-ubuntu /usr/bin/unrar

# Install necessary packages
RUN apt-get update && \
    apt-get install -y rsync && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir transmission-rpc==7.0.10

# Copy the application code
COPY main.py /app/main.py

# Run the application
CMD ["python", "main.py"]
