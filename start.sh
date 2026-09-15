#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

# 1. Start the keep-alive server in the background (so Render doesn't kill the app)
python3 /app/keep_alive.py &

# 2. Wait a few seconds
sleep 3

# 3. Setup Hermes environment variables (You must set these in Render Dashboard!)
# OPENAI_API_BASE="https://unknown44.onrender.com/v1"
# OPENAI_API_KEY="Swapnpurti@1181"
# TELEGRAM_BOT_TOKEN="your_telegram_token"

echo "Starting Hermes Agent..."

# If hermes is installed globally by the script, run the gateway
# Note: Hermes CLI might require manual setup first time, so we just run a sleep loop 
# if the main command fails, to keep the container alive so you can inspect it.

# Enable all super-powers (Tools) for Hermes so it can create files and run code
hermes config set terminal.backend local
hermes config set tools.enabled_toolsets '["core", "terminal", "python", "browser"]'

# VERY IMPORTANT: Force Hermes to use your secure API Password from Environment
if [ -n "$API_PASSWORD" ]; then
    hermes config set model.api_key "$API_PASSWORD"
else
    echo "WARNING: API_PASSWORD is not set in Environment Variables!"
fi

# Start Hermes in gateway mode (Telegram/Discord listener)
hermes gateway run || echo "Hermes failed to start."

# Keep container alive forever just in case Hermes crashes
sleep infinity
