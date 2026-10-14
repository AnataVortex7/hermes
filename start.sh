#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export TZ="Asia/Kolkata"

echo "=== [PicoW Clow Koyeb Instant Startup & Background Sync] ==="

# 1. START KEEP-ALIVE SERVER INSTANTLY
if [ -f /app/keep_alive.py ]; then
  echo ">> Starting keep-alive HTTP server immediately..."
  python3 /app/keep_alive.py &
fi

mkdir -p ~/.picow_clow

# 2. Setup Rclone configuration
mkdir -p ~/.config/rclone
if [ -n "$RCLONE_CONFIG_BASE64" ]; then
  echo ">> Configuring rclone from RCLONE_CONFIG_BASE64..."
  echo "$RCLONE_CONFIG_BASE64" | base64 -d > ~/.config/rclone/rclone.conf
elif [ -n "$RCLONE_CONFIG" ]; then
  echo ">> Configuring rclone from RCLONE_CONFIG..."
  echo "$RCLONE_CONFIG" > ~/.config/rclone/rclone.conf
fi

REMOTE_BACKUP="${RCLONE_REMOTE:-gdrive:picow_clow_backup}"

rclone_sync() {
  rclone "$@" \
    --exclude "cache/**" \
    --exclude "audio_cache/**" \
    --exclude "image_cache/**" \
    --exclude "runtime/**" \
    --exclude "node_modules/**" \
    --exclude ".env"
}

# 3. Restore from Google Drive (ensure remote folder exists or handle first run)
if [ -f ~/.config/rclone/rclone.conf ]; then
  echo ">> Ensuring remote backup directory exists..."
  rclone mkdir "$REMOTE_BACKUP" 2>/dev/null || true
  
  echo ">> Restoring PicoW Clow state from Google Drive ($REMOTE_BACKUP)..."
  rclone_sync sync "$REMOTE_BACKUP" ~/.picow_clow/ --drive-chunk-size 8M || echo ">> Restore skipped."
fi

# Signal keep-alive that startup is complete
echo ">> Signaling startup complete..."
curl -s "http://localhost:${PORT:-10000}/startup-ready" || true

# 4. Background Sync Loop
sync_to_cloud() {
  if [ -f ~/.config/rclone/rclone.conf ]; then
    rclone mkdir "$REMOTE_BACKUP" 2>/dev/null || true
    rclone_sync sync ~/.picow_clow/ "$REMOTE_BACKUP" --drive-chunk-size 8M --fast-list || true
  fi
}

echo ">> Initial sync to Drive..."
sync_to_cloud

(
  while true; do
    sleep 300
    sync_to_cloud
  done
) &
SYNC_PID=$!

# 5. Trap for graceful shutdown
cleanup() {
  echo ">> Container shutting down. Performing final sync..."
  kill $SYNC_PID 2>/dev/null || true
  sync_to_cloud
  exit 0
}
trap cleanup SIGTERM SIGINT EXIT

# 6. Start PicoW Clow Main Process / Application
if [ -f /app/main.py ]; then
  echo ">> Starting PicoW Clow main.py..."
  python3 /app/main.py
elif [ -f /app/app.py ]; then
  echo ">> Starting PicoW Clow app.py..."
  python3 /app/app.py
else
  echo ">> No main.py or app.py found, entering infinite sleep..."
  sleep infinity
fi
