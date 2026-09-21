#!/bin/bash

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export TZ="Asia/Kolkata"

echo "=== [Hermes Koyeb Instant Startup & Background Sync] ==="

# 1. START KEEP-ALIVE SERVER INSTANTLY
if [ -f /app/keep_alive.py ]; then
    echo ">> Starting keep-alive HTTP server immediately..."
    python3 /app/keep_alive.py &
fi

# Ensure Hermes config dir exists
mkdir -p ~/.hermes

# Write the API keys from environment variables to Hermes .env
cat <<EOF > ~/.hermes/.env
OPENAI_API_KEY=${OPENAI_API_KEY}
UNKNOWN44_API_KEY=${UNKNOWN44_API_KEY:-${OPENAI_API_KEY}}
CUSTOM_API_KEY=${CUSTOM_API_KEY:-${OPENAI_API_KEY}}
EOF

# Set Custom API Endpoint
export OPENAI_API_BASE="${OPENAI_API_BASE:-https://unknown44.onrender.com/v1/}"
export OPENAI_API_KEY="${OPENAI_API_KEY}"
export UNKNOWN44_API_KEY="${UNKNOWN44_API_KEY:-${OPENAI_API_KEY}}"
export CUSTOM_API_KEY="${CUSTOM_API_KEY:-${OPENAI_API_KEY}}"
export MODEL_PROVIDER="${MODEL_PROVIDER:-custom}"
export MODEL_DEFAULT="${MODEL_DEFAULT:-gemini-pro}"
export api_key="${OPENAI_API_KEY}"

# 2. Setup Rclone configuration
mkdir -p ~/.config/rclone

if [ -n "$RCLONE_CONFIG_BASE64" ]; then
    echo ">> Configuring rclone from RCLONE_CONFIG_BASE64..."
    echo "$RCLONE_CONFIG_BASE64" | base64 -d > ~/.config/rclone/rclone.conf
elif [ -n "$RCLONE_CONFIG" ]; then
    echo ">> Configuring rclone from RCLONE_CONFIG..."
    echo "$RCLONE_CONFIG" > ~/.config/rclone/rclone.conf
fi

REMOTE_BACKUP="${RCLONE_REMOTE:-gdrive:hermes_backup}"

# 3. Restore from Google Drive
# --ignore-checksum  : checksum mismatch वर error नाही, file accept करतो
# --copy-links       : symlinks ला actual file म्हणून copy करतो
# --ignore-errors    : एखादी file fail झाली तरी पुढे चालू राहतो
if [ -f ~/.config/rclone/rclone.conf ]; then
    echo ">> Restoring Hermes state from Google Drive ($REMOTE_BACKUP)..."
    mkdir -p ~/.hermes

    rclone sync "$REMOTE_BACKUP" ~/.hermes/ \
        --exclude "cache/**" \
        --exclude "audio_cache/**" \
        --exclude "image_cache/**" \
        --exclude "runtime/**" \
        --exclude "*.partial" \
        --ignore-checksum \
        --copy-links \
        --ignore-errors \
        --drive-chunk-size 8M \
        --transfers 4 \
        || echo ">> Restore completed with some warnings."

    # Corrupted partial/state files clean करा — fresh start साठी
    echo ">> Cleaning up corrupted partial/state files..."
    find ~/.hermes -name "*.partial" -delete 2>/dev/null || true
    find ~/.hermes/state -name "*.partial" -delete 2>/dev/null || true
    find ~/.hermes/cron -name "*.partial" -delete 2>/dev/null || true

    echo ">> Restore done."
else
    echo ">> No rclone config found, skipping restore."
fi

# Symlink Himalaya config
mkdir -p ~/.config/himalaya
if [ -f ~/.hermes/skills/email/himalaya/config.toml ]; then
    ln -sf ~/.hermes/skills/email/himalaya/config.toml ~/.config/himalaya/config.toml
fi

# 4. Background Sync Loop (Every 1 Minute)
sync_to_cloud() {
    if [ -f ~/.config/rclone/rclone.conf ]; then
        rclone sync ~/.hermes/ "$REMOTE_BACKUP" \
            --exclude "cache/**" \
            --exclude "audio_cache/**" \
            --exclude "image_cache/**" \
            --exclude "runtime/**" \
            --exclude "*.partial" \
            --ignore-checksum \
            --copy-links \
            --ignore-errors \
            --drive-chunk-size 8M \
            --fast-list || true
    fi
}

(
    while true; do
        sleep 60
        sync_to_cloud
    done
) &
SYNC_PID=$!

# 5. Watchdog — Hermes crash check (every 2 minutes)
watchdog_check() {
    while true; do
        sleep 120
        if ! pgrep -f "hermes gateway" > /dev/null 2>&1; then
            echo ">> [WATCHDOG] Hermes gateway not found! Restarting..."
            pkill -f "hermes" 2>/dev/null || true
            sleep 2
            hermes gateway run &
            echo ">> [WATCHDOG] Hermes restarted with PID $!"
        else
            echo ">> [WATCHDOG] Hermes is running OK."
        fi
    done
}

watchdog_check &
WATCHDOG_PID=$!

# 6. Trap for graceful shutdown
cleanup() {
    echo ">> Container shutting down. Performing final sync..."
    kill $SYNC_PID 2>/dev/null || true
    kill $WATCHDOG_PID 2>/dev/null || true
    pkill -f "hermes gateway" 2>/dev/null || true
    sync_to_cloud
    exit 0
}

trap cleanup SIGTERM SIGINT EXIT

# 7. Setup Hermes config once
echo ">> Configuring Hermes..."
hermes config set terminal.backend local || true
hermes auth add custom \
    --type api-key \
    --api-key "${OPENAI_API_KEY}" \
    --inference-url "${OPENAI_API_BASE:-https://unknown44.onrender.com/v1/}" || true

# 8. Hermes Gateway — Auto-restart loop on crash
CRASH_COUNT=0
MAX_CRASHES=10
RESTART_DELAY=5

echo ">> Starting Hermes Gateway with auto-restart..."

while true; do
    echo ">> [$(date '+%H:%M:%S')] Hermes Gateway starting (crash count: $CRASH_COUNT)..."

    hermes gateway run
    EXIT_CODE=$?

    echo ">> [$(date '+%H:%M:%S')] Hermes Gateway exited with code $EXIT_CODE"

    CRASH_COUNT=$((CRASH_COUNT + 1))

    if [ $CRASH_COUNT -ge $MAX_CRASHES ]; then
        echo ">> [ERROR] Hermes crashed $MAX_CRASHES times. Waiting 60s before retry..."
        CRASH_COUNT=0
        RESTART_DELAY=60
    fi

    echo ">> Restarting in ${RESTART_DELAY} seconds..."
    sleep $RESTART_DELAY
    RESTART_DELAY=5
done

cleanup
