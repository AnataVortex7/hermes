#!/bin/bash
set -e

echo "=== [Hermes Koyeb Production Startup & Rclone Sync] ==="

# 1. Ensure PATH
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

# 2. Setup Rclone configuration from Environment Variables if present
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

# Remote backup target (default: gdrive:hermes_backup)
REMOTE_BACKUP="${RCLONE_REMOTE:-gdrive:hermes_backup}"
# Optimized Exclude: Only backup essential user state files, ignore binaries and caches
# We exclude everything and include specific directories
EXCLUDE_RULES='--exclude "/**" --include "/config.yaml" --include "/memories/**" --include "/sessions/**" --include "/state.db*" --include "/skills/**" --include "/cron/**" --include "/.env" --include "/shared-state.db"'

# 3. Restore data from Google Drive before starting Hermes
if [ -f ~/.config/rclone/rclone.conf ]; then
    echo ">> Restoring Hermes state from Google Drive ($REMOTE_BACKUP)..."
    mkdir -p ~/.hermes
    rclone sync "$REMOTE_BACKUP" ~/.hermes/ $EXCLUDE_RULES --drive-chunk-size 8M || echo ">> Restore skipped or remote empty."
else
    echo ">> Warning: No rclone config found. Running with ephemeral local storage."
fi

# 4. Auto-clean Caches & Background Sync Function (runs every 10 minutes)
clean_caches() {
    echo ">> Auto-clearing temporary caches and audio/image files..."
    rm -rf ~/.hermes/audio_cache/* ~/.hermes/image_cache/* ~/.hermes/cache/terminal/* /tmp/* 2>/dev/null || true
}

sync_to_cloud() {
    clean_caches
    if [ -f ~/.config/rclone/rclone.conf ]; then
        echo ">> [Background Sync] Syncing ~/.hermes to Google Drive ($REMOTE_BACKUP)..."
        rclone sync ~/.hermes/ "$REMOTE_BACKUP" $EXCLUDE_RULES --drive-chunk-size 8M --fast-list || true
    fi
}

(
    while true; do
        sleep 600
        sync_to_cloud
    done
) &
SYNC_PID=$!

# 5. Trap for graceful shutdown / restart (Ensures last-minute memory/chats are saved)
cleanup() {
    echo ">> Container shutting down. Performing final sync..."
    kill $SYNC_PID 2>/dev/null || true
    sync_to_cloud
    echo ">> Final sync complete. Exiting."
    exit 0
}
trap cleanup SIGTERM SIGINT EXIT

# 6. Start keep_alive health-check server in background
if [ -f /app/keep_alive.py ]; then
    echo ">> Starting keep-alive HTTP server..."
    python3 /app/keep_alive.py &
fi

# 7. Configure Hermes Agent
echo ">> Configuring Hermes Agent settings..."
hermes config set terminal.backend local || true
hermes config set tools.enabled_toolsets '["core", "terminal", "python", "browser"]' || true

# 8. Start Hermes Gateway (Telegram listener)
echo ">> Starting Hermes Gateway..."
hermes gateway run || echo ">> Hermes gateway exited."

# Final cleanup
cleanup
