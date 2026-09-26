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

# Write the API keys from env vars to Hermes .env
cat <<ENVEOF > ~/.hermes/.env
OPENAI_API_KEY=${CUSTOM_API_KEY}
UNKNOWN44_API_KEY=${CUSTOM_API_KEY}
CUSTOM_API_KEY=${CUSTOM_API_KEY}
ENVEOF

# Dashboard language → English force (config.yaml)
mkdir -p ~/.hermes
python3 -c "
import os
cfg_path = os.path.expanduser('~/.hermes/config.yaml')
try:
    content = open(cfg_path).read()
    if 'language:' in content:
        import re
        content = re.sub(r'language:\s*\S+', 'language: en', content)
    else:
        content += '\nui:\n  language: en\n'
    open(cfg_path,'w').write(content)
except Exception:
    pass
" 2>/dev/null || true

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

# NOTE: these excludes are shared by BOTH the restore and the backup sync below.
# hermes-agent/, tools/, and installs/ are the app install (node_modules, the
# bundled python runtime, the built hermes binary) — they're baked into the
# Docker image at build time and must NEVER be touched by the Drive sync.
# Previously the sync swept these up too, and since rclone drops symlinks by
# default, every backup silently stripped the executable symlinks out of the
# app install. Restoring that broken copy over the good build-time install is
# what caused "hermes: not found" and the endless watchdog restart loop.
RCLONE_STATE_EXCLUDES=(
  --exclude "cache/**"
  --exclude "audio_cache/**"
  --exclude "image_cache/**"
  --exclude "runtime/**"
  --exclude "hermes-agent/**"
  --exclude "tools/**"
  --exclude "installs/**"
  --exclude "node_modules/**"
)

# 3. Restore from Google Drive
if [ -f ~/.config/rclone/rclone.conf ]; then
  echo ">> Restoring Hermes state from Google Drive ($REMOTE_BACKUP)..."
  mkdir -p ~/.hermes
  rclone sync "$REMOTE_BACKUP" ~/.hermes/ \
    "${RCLONE_STATE_EXCLUDES[@]}" \
    --drive-chunk-size 8M || echo ">> Restore skipped."
fi

# Symlink Himalaya config
mkdir -p ~/.config/himalaya
if [ -f ~/.hermes/skills/email/himalaya/config.toml ]; then
  ln -sf ~/.hermes/skills/email/himalaya/config.toml ~/.config/himalaya/config.toml
fi

# Fix script permissions after restore
if [ -d ~/.hermes/scripts ]; then
  chmod +x ~/.hermes/scripts/*.sh 2>/dev/null || true
fi

# 4. STARTUP NOTIFICATION
FIRST_USER=$(echo "${TELEGRAM_ALLOWED_USERS}" | cut -d',' -f1 | tr -d ' ')
if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$FIRST_USER" ]; then
  echo ">> Sending Telegram startup notification to ${FIRST_USER}..."
  curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${FIRST_USER}" \
    --data-urlencode "text=🟢 Hermes is back online!
⏰ $(date '+%d %b %Y %I:%M %p IST')

✅ Drive restore complete
🚀 Gateway connecting now...

📋 Check all pending tasks." > /dev/null 2>&1
  echo ">> Notification sent."
fi

# Signal keep-alive that startup is complete
echo ">> Signaling startup complete..."
curl -s "http://localhost:${PORT:-10000}/startup-ready" || true

# ── Cache Clean Function ──
clean_cache() {
  echo ">> [Cache Clean] Running scheduled cache cleanup..."
  rm -rf ~/.hermes/cache/* 2>/dev/null || true
  rm -rf ~/.hermes/audio_cache/* 2>/dev/null || true
  rm -rf ~/.hermes/image_cache/* 2>/dev/null || true
  rm -rf ~/.hermes/tmp/* 2>/dev/null || true
  find ~/.hermes -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
  find ~/.hermes -name "*.pyc" -delete 2>/dev/null || true
  rm -rf /root/.cache/pip 2>/dev/null || true
  find /tmp -maxdepth 1 -type f -mmin +30 -delete 2>/dev/null || true
  rm -rf /root/.npm/_cacache 2>/dev/null || true
  echo ">> [Cache Clean] Done. RAM free: $(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)MB"
}

# 5. Background Sync + Cache Clean Loop
sync_to_cloud() {
  if [ -f ~/.config/rclone/rclone.conf ]; then
    rclone sync ~/.hermes/ "$REMOTE_BACKUP" \
      "${RCLONE_STATE_EXCLUDES[@]}" \
      --drive-chunk-size 8M --fast-list || true
  fi
}

(
  LOOP_COUNT=0
  while true; do
    sleep 300
    sync_to_cloud
    LOOP_COUNT=$(( LOOP_COUNT + 1 ))
    # दर 12 loops = दर 1 तास → cache clean
    if [ $(( LOOP_COUNT % 12 )) -eq 0 ]; then
      clean_cache
    fi
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

# 7. Start Hermes Dashboard (port 9119 — keep_alive proxy करेल)
echo ">> Starting Hermes Dashboard on port 9119..."
hermes dashboard --host 0.0.0.0 --port 9119 --no-open --insecure &
DASHBOARD_PID=$!
echo ">> Dashboard PID: $DASHBOARD_PID"

# Dashboard ला 3 seconds warmup
sleep 3

# 8. Start Watchdog (manages Hermes Gateway)
echo ">> Starting Watchdog..."
/app/watchdog.sh &

# Wait forever
wait
