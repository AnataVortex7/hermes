#!/bin/bash

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export TZ="Asia/Kolkata"

echo "=== [Hermes Koyeb Instant Startup & Background Sync] ==="

# 1. START KEEP-ALIVE SERVER INSTANTLY
if [ -f /app/keep_alive.py ]; then
    echo ">> Starting keep-alive HTTP server immediately..."
    python3 /app/keep_alive.py &
fi

mkdir -p ~/.hermes

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

# 3. Restore from Google Drive (runs BEFORE we write our own config below,
# so a restored old config.yaml/.env can never overwrite what we set here)
if [ -f ~/.config/rclone/rclone.conf ]; then
    echo ">> Restoring Hermes state from Google Drive ($REMOTE_BACKUP)..."
    rclone sync "$REMOTE_BACKUP" ~/.hermes/ --exclude "cache/**" --exclude "audio_cache/**" --exclude "image_cache/**" --exclude "runtime/**" --drive-chunk-size 8M || echo ">> Restore skipped."
fi

# 4. Point Hermes at the wapi round-robin proxy.
# "auto" tells wapi's smart_router.py to round-robin across ALL of its keys/models
# for every request -- see AnataVortex7/wapi smart_router.py: is_round_robin check.
export OPENAI_API_KEY="Swapnpurti@1181"
export OPENAI_BASE_URL="https://unknown44.onrender.com/v1/"
export OPENAI_API_BASE="https://unknown44.onrender.com/v1/"   # older var name, harmless to set both

cat <<EOF > ~/.hermes/config.yaml
model:
  provider: custom
  base_url: https://unknown44.onrender.com/v1/
  default: auto
terminal:
  backend: local
EOF

# Symlink Himalaya config
mkdir -p ~/.config/himalaya
if [ -f ~/.hermes/skills/email/himalaya/config.toml ]; then
    ln -sf ~/.hermes/skills/email/himalaya/config.toml ~/.config/himalaya/config.toml
fi

# 5. Background Sync Loop (Every 1 Minute)
sync_to_cloud() {
    if [ -f ~/.config/rclone/rclone.conf ]; then
        rclone sync ~/.hermes/ "$REMOTE_BACKUP" --exclude "cache/**" --exclude "audio_cache/**" --exclude "image_cache/**" --exclude "runtime/**" --drive-chunk-size 8M --fast-list || true
    fi
}

(
    while true; do
        sleep 60
        sync_to_cloud
    done
) &
SYNC_PID=$!

# 6. Trap for graceful shutdown
cleanup() {
    echo ">> Container shutting down. Performing final sync..."
    kill $SYNC_PID 2>/dev/null || true
    sync_to_cloud
    exit 0
}
trap cleanup SIGTERM SIGINT EXIT

# 7. Start Hermes Gateway
echo ">> Starting Hermes Gateway..."
hermes doctor || true
hermes gateway run || echo ">> Hermes gateway exited."

cleanup
