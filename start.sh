#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export TZ="Asia/Kolkata"

echo "=== [Hermes Koyeb Instant Startup & Background Sync] ==="

# 1. START KEEP-ALIVE SERVER INSTANTLY (So Koyeb health-check passes immediately!)
if [ -f /app/keep_alive.py ]; then
    echo ">> Starting keep-alive HTTP server immediately..."
    python3 /app/keep_alive.py &
fi

# Set Custom API Endpoint from Environment Variables
export OPENAI_API_BASE="https://unknown44.onrender.com/v1/"
export OPENAI_API_KEY="${OPENAI_API_KEY:-Swapnpurti@1181}"
export MODEL_PROVIDER="custom"
export MODEL_DEFAULT="gemini-pro"

# 2. Setup Rclone configuration
mkdir -p ~/.config/rclone

if [ -n "$RCLONE_CONFIG_BASE64" ]; then
    echo ">> Configuring rclone from RCLONE_CONFIG_BASE64..."
    echo "$RCLONE_CONFIG_BASE64" | base64 -d > ~/.config/rclone/rclone.conf
elif [ -n "$RCLONE_CONFIG" ]; then
    echo ">> Configuring rclone from RCLONE_CONFIG..."
    echo "$RCLONE_CONFIG" > ~/.config/rclone/rclone.conf
fi

if [ -n "$SA_KEY_BASE64" ]; then
    echo ">> Configuring Service Account from SA_KEY_BASE64..."
    echo "$SA_KEY_BASE64" | base64 -d > ~/.config/rclone/sa.json
fi

# Remote backup target
REMOTE_BACKUP="${RCLONE_REMOTE:-gdrive:hermes_backup}"

# 3. Fast Restore from Google Drive (Using clean explicit includes)
if [ -f ~/.config/rclone/rclone.conf ]; then
    echo ">> Restoring Hermes state from Google Drive ($REMOTE_BACKUP)..."
    mkdir -p ~/.hermes
    rclone sync "$REMOTE_BACKUP" ~/.hermes/ --include "/config.yaml" --include "/memories/**" --include "/sessions/**" --include "/state.db*" --include "/skills/**" --include "/cron/**" --include "/.env" --include "/shared-state.db" --include "/channel_directory.json" --drive-chunk-size 8M || echo ">> Restore skipped."
fi

# 4. Auto-clean Caches & Background Sync Loop (runs every 10 minutes)
clean_caches() {
    rm -rf ~/.hermes/audio_cache/* ~/.hermes/image_cache/* ~/.hermes/cache/terminal/* /tmp/* 2>/dev/null || true
}

sync_to_cloud() {
    clean_caches
    if [ -f ~/.config/rclone/rclone.conf ]; then
        rclone sync ~/.hermes/ "$REMOTE_BACKUP" --include "/config.yaml" --include "/memories/**" --include "/sessions/**" --include "/state.db*" --include "/skills/**" --include "/cron/**" --include "/.env" --include "/shared-state.db" --include "/channel_directory.json" --drive-chunk-size 8M --fast-list || true
    fi
}

(
    while true; do
        sleep 60
        sync_to_cloud
    done
) &
SYNC_PID=$!

# 5. Trap for graceful shutdown / restart
cleanup() {
    echo ">> Container shutting down. Performing final sync..."
    kill $SYNC_PID 2>/dev/null || true
    sync_to_cloud
    echo ">> Final sync complete. Exiting."
    exit 0
}
trap cleanup SIGTERM SIGINT EXIT

# 6. Start Hermes Gateway (Telegram listener)
echo ">> Starting Hermes Gateway..."
hermes config set terminal.backend local || true
hermes config set model.base_url "https://unknown44.onrender.com/v1/" || true
hermes config set model.api_key "$OPENAI_API_KEY" || true
hermes config set model.provider "custom" || true
hermes config set model.default "custom/gemini-pro" || true
hermes gateway run || echo ">> Hermes gateway exited."

cleanup
