FROM python:3.10-slim

# Install necessary tools & rclone
RUN apt-get update && apt-get install -y curl git sudo bash unzip && \
    curl https://rclone.org/install.sh | bash && \
    rm -rf /var/lib/apt/lists/*

# Install Hermes Agent (using their official script)
RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

WORKDIR /app

# Copy our custom scripts
COPY keep_alive.py /app/keep_alive.py
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# Render/Koyeb exposes PORT environment variable
EXPOSE 10000

# Start script
CMD ["/app/start.sh"]
