#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export TZ="Asia/Kolkata"

echo "=== [PicoW Clow Koyeb Instant Startup & Background Sync] ==="

# 1. START KEEP-ALIVE SERVER INSTANTLY
if [ -f /app/keep_alive.py ]; then
  echo ">> Starting keep-alive HTTP server immediately..."
  python3 /app/keep_alive.py &
fi

# 1b. TAILSCALE (background, userspace mode: works on Koyeb without TUN/NET_ADMIN)
# Needs TAILSCALE_AUTHKEY (use a Reusable + Ephemeral key). Services that listen
# only on 127.0.0.1 are forwarded to your tailnet with `tailscale serve --tcp`.
if [ -n "$TAILSCALE_AUTHKEY" ] && command -v tailscaled >/dev/null 2>&1; then
  (
    TS_SOCK=/tmp/tailscale/tailscaled.sock
    TS="tailscale --socket=$TS_SOCK"
    mkdir -p /tmp/tailscale
    echo ">> [tailscale] starting tailscaled (userspace)..."
    tailscaled --tun=userspace-networking --state=mem: --socket="$TS_SOCK" \
      --socks5-server=localhost:1055 >/tmp/tailscale/tailscaled.log 2>&1 &
    for _ in $(seq 1 30); do [ -S "$TS_SOCK" ] && break; sleep 1; done
    if timeout 90 $TS up --auth-key="$TAILSCALE_AUTHKEY" \
         --hostname="${TAILSCALE_HOSTNAME:-hermes}" --accept-dns=false; then
      echo ">> [tailscale] connected, IPv4: $($TS ip -4 2>/dev/null | head -n1)"
      for p in $(echo "${TAILSCALE_PORTS:-10000,9119,18790,18800}" | tr ',' ' '); do
        if $TS serve --bg --tcp="$p" "tcp://127.0.0.1:$p" >/dev/null 2>&1; then
          echo ">> [tailscale] forwarding tailnet port $p -> 127.0.0.1:$p"
        else
          echo ">> [tailscale] could not forward port $p"
        fi
      done
    else
      echo ">> [tailscale] 'tailscale up' failed - last log lines:"
      tail -n 15 /tmp/tailscale/tailscaled.log 2>/dev/null
    fi
  ) &
elif [ -n "$TAILSCALE_AUTHKEY" ]; then
  echo ">> [tailscale] TAILSCALE_AUTHKEY set but tailscaled binary is missing"
fi

mkdir -p ~/.picow_clow ~/.picoclaw

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

# PicoClaw state (sessions, memory, workspace) is backed up next to the rest.
# config.json / .security.yml hold the bot token + API key, so they are NOT
# synced to Drive - they are regenerated from env vars on every boot.
PICOCLAW_REMOTE="${REMOTE_BACKUP}/picoclaw"
picoclaw_sync() {
  rclone "$@" \
    --exclude "config.json" \
    --exclude ".security.yml" \
    --exclude "*.log" \
    --exclude "cache/**"
}

# 3. Restore from Google Drive (ensure remote folder exists or handle first run)
# SYNC_SAFE=1 only if the restore finished OK. Otherwise the background sync
# is disabled, so an empty local folder can never wipe the Drive backup.
SYNC_SAFE=0
if [ -f ~/.config/rclone/rclone.conf ]; then
  echo ">> Ensuring remote backup directory exists..."
  rclone mkdir "$REMOTE_BACKUP" 2>/dev/null || true
  rclone mkdir "$PICOCLAW_REMOTE" 2>/dev/null || true

  echo ">> Restoring PicoW Clow state from Google Drive ($REMOTE_BACKUP)..."
  if rclone_sync sync "$REMOTE_BACKUP" ~/.picow_clow/ --drive-chunk-size 8M --exclude "picoclaw/**" \
     && picoclaw_sync sync "$PICOCLAW_REMOTE" ~/.picoclaw/ --drive-chunk-size 8M; then
    SYNC_SAFE=1
    echo ">> Restore OK."
  else
    echo ">> Restore FAILED - background sync to Drive is DISABLED to protect the backup."
  fi
fi

# 3b. Replay packages installed earlier via the bin/ wrappers (apt/pip/npm)
INST=~/.picow_clow/installed
if [ -s "$INST/apt.list" ]; then
  echo ">> Re-installing apt packages: $(tr '\n' ' ' < $INST/apt.list)"
  apt-get update -qq && apt-get install -y --no-install-recommends $(cat "$INST/apt.list") || echo ">> apt replay failed"
fi
[ -s "$INST/pip.list" ] && { echo ">> Re-installing pip packages..."; pip install $(cat "$INST/pip.list") || echo ">> pip replay failed"; }
[ -s "$INST/npm.list" ] && command -v npm >/dev/null && { echo ">> Re-installing npm globals..."; npm install -g $(cat "$INST/npm.list") || echo ">> npm replay failed"; }

# Signal keep-alive that startup is complete
echo ">> Signaling startup complete..."
curl -s "http://localhost:${PORT:-10000}/startup-ready" || true

# 4. Background Sync Loop
sync_to_cloud() {
  if [ -f ~/.config/rclone/rclone.conf ] && [ "$SYNC_SAFE" = "1" ]; then
    rclone mkdir "$REMOTE_BACKUP" 2>/dev/null || true
    rclone_sync sync ~/.picow_clow/ "$REMOTE_BACKUP" --drive-chunk-size 8M --fast-list --exclude "picoclaw/**" || true
    picoclaw_sync sync ~/.picoclaw/ "$PICOCLAW_REMOTE" --drive-chunk-size 8M --fast-list || true
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
APP_PID=""
cleanup() {
  trap - SIGTERM SIGINT EXIT
  echo ">> Container shutting down. Performing final sync..."
  [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null || true
  kill $SYNC_PID 2>/dev/null || true
  sync_to_cloud
  exit 0
}
trap cleanup SIGTERM SIGINT EXIT

# 6. Start PicoClaw gateway (Telegram long-polling, no inbound port needed)
if command -v picoclaw >/dev/null 2>&1 && python3 /app/setup_picoclaw.py; then
  echo ">> Starting PicoClaw gateway (Telegram)..."
  picoclaw gateway &
  APP_PID=$!
  wait "$APP_PID"
elif [ -f /app/main.py ]; then
  echo ">> Starting main.py..."
  python3 /app/main.py
elif [ -f /app/app.py ]; then
  echo ">> Starting app.py..."
  python3 /app/app.py
else
  echo ">> PicoClaw not started (check TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOW_FROM / LLM_API_KEY). Keeping container alive..."
  sleep infinity
fi
