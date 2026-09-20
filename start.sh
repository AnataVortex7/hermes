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

# 3. Restore from Google Drive (Sync EVERYTHING except cache/tmp)
# NOTE: this runs BEFORE we write our custom API config below, so a
# restored old .env/config can never overwrite what we set for this run.
if [ -f ~/.config/rclone/rclone.conf ]; then
    echo ">> Restoring Hermes state from Google Drive ($REMOTE_BACKUP)..."
    mkdir -p ~/.hermes
    rclone sync "$REMOTE_BACKUP" ~/.hermes/ --exclude "cache/**" --exclude "audio_cache/**" --exclude "image_cache/**" --exclude "runtime/**" --drive-chunk-size 8M || echo ">> Restore skipped."
fi

# 4. Write custom API config AFTER restore, so it always wins.
# Only CUSTOM_API_KEY is used - we deliberately do NOT export OPENAI_API_KEY,
# since its presence makes some tools auto-pick the "openai" provider even
# when MODEL_PROVIDER is set to "custom".
cat <<EOF > ~/.hermes/.env
CUSTOM_API_KEY=${CUSTOM_API_KEY}
EOF

export CUSTOM_API_KEY="${CUSTOM_API_KEY}"
export MODEL_PROVIDER="custom"
export MODEL_DEFAULT="${MODEL_DEFAULT:-gpt-4o-mini}"

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
hermes config set terminal.backend local || true

# No more `|| true` here - if this fails, we want the script to fail loudly
# instead of silently falling back to whatever provider was already configured.
hermes auth add custom --type api-key --api-key "$CUSTOM_API_KEY" --inference-url "https://unknown44.onrender.com/v1/"

echo ">> Starting Hermes Gateway..."
hermes gateway run || echo ">> Hermes gateway exited."

cleanup
