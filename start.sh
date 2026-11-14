#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export TZ="Asia/Kolkata"

echo "=== [PicoW Clow Koyeb Instant Startup & Background Sync] ==="

# 1. START KEEP-ALIVE SERVER INSTANTLY
if [ -f /app/keep_alive.py ]; then
  echo ">> Starting keep-alive HTTP server immediately..."
  python3 /app/keep_alive.py &
fi

BOOT_TS=$(date +%s)
notify() { python3 /app/notify.py "$1" >/dev/null 2>&1 || true; }
if [ "${NOTIFY_ON_BOOT:-1}" = "1" ]; then
  notify "🔄 Server restart झाला. Google Drive वरून history restore सुरू आहे...
PicoClaw अजून start केलेला नाही - restore पूर्ण झाल्यावर 'ready' message येईल, तोपर्यंत message पाठवू नका."
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

# Persistent Python packages: EVERY pip install (pip, pip3, `python3 -m pip`,
# run by the agent or by hand) goes to ~/.picow_clow/pyuser, which is synced to
# Drive and restored on boot. This does not depend on the bin/ wrappers, which
# `python3 -m pip install ...` bypasses.
export PYTHONUSERBASE="$HOME/.picow_clow/pyuser"
export PIP_USER=1
export PIP_BREAK_SYSTEM_PACKAGES=1
export PATH="$PYTHONUSERBASE/bin:$PATH"
mkdir -p "$PYTHONUSERBASE"

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
RESTORE_OK=0
RESTORE_SECS=0
CLEAN_START=0
if [ -f ~/.config/rclone/rclone.conf ]; then
  # PicoClaw is NOT started until the restore has fully succeeded; a failed restore is retried
  # (otherwise it would start with an empty history and the next sync would overwrite Drive).
  #   After RESTORE_MAX_ATTEMPTS (default 2) failed attempts PicoClaw starts with a CLEAN history
  #   (half-restored files are wiped first). Background sync to Drive stays DISABLED for that run
  #   so the old backup on Drive is never overwritten. The next restart tries the restore again.
  #   RESTORE_MAX_ATTEMPTS=0 or ALLOW_START_WITHOUT_RESTORE=0 -> never start without a restore (retry forever)
  RESTORE_START=$(date +%s)
  ATTEMPT=0
  while true; do
    ATTEMPT=$((ATTEMPT + 1))
    echo ">> Restore attempt $ATTEMPT from Google Drive ($REMOTE_BACKUP)..."
    rclone mkdir "$REMOTE_BACKUP" 2>/dev/null || true
    rclone mkdir "$PICOCLAW_REMOTE" 2>/dev/null || true
    if rclone_sync sync "$REMOTE_BACKUP" ~/.picow_clow/ --drive-chunk-size 8M --exclude "picoclaw/**" \
       && picoclaw_sync sync "$PICOCLAW_REMOTE" ~/.picoclaw/ --drive-chunk-size 8M; then
      SYNC_SAFE=1; RESTORE_OK=1
      RESTORE_SECS=$(( $(date +%s) - RESTORE_START ))
      echo ">> Restore OK (${RESTORE_SECS}s)."
      break
    fi
    echo ">> Restore FAILED (attempt $ATTEMPT)."
    if [ "${RESTORE_MAX_ATTEMPTS:-2}" -gt 0 ] && [ "$ATTEMPT" -ge "${RESTORE_MAX_ATTEMPTS:-2}" ] \
       && [ "${ALLOW_START_WITHOUT_RESTORE:-1}" = "1" ]; then
      echo ">> Restore failed $ATTEMPT times -> wiping partial local state and starting with a CLEAN history."
      # Local-only wipe (container disk). Drive is untouched because SYNC_SAFE stays 0.
      find ~/.picoclaw ~/.picow_clow -mindepth 1 -delete 2>/dev/null || true
      mkdir -p ~/.picow_clow ~/.picoclaw
      CLEAN_START=1
      notify "🧹 Restore $ATTEMPT वेळा फेल झाला, म्हणून PicoClaw CLEAN history ने start होतोय (जुनी history या वेळी नाही).
⚠️ Drive वरचा जुना backup सुरक्षित ठेवण्यासाठी या run चा Drive sync बंद आहे. Drive ठीक झाल्यावर Koyeb मध्ये redeploy/restart केला की जुनी history परत येईल."
      break
    fi
    [ "$ATTEMPT" = "1" ] && notify "⚠️ Drive restore फेल झाला (attempt 1). PicoClaw अजून start केलेला नाही, परत प्रयत्न करतोय..."
    sleep 20
  done
else
  echo ">> No rclone config - nothing to restore (fresh state)."
  notify "⚠️ rclone config नाही, म्हणून Drive restore झालं नाही (fresh state). RCLONE_CONFIG_BASE64 तपासा."
fi

mkdir -p "$PYTHONUSERBASE"

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

  # Tell the owner on Telegram when PicoClaw is really up (gateway /ready == 200).
  (
    HP="${PICOCLAW_HEALTH_PORT:-18790}"
    READY=0
    for _ in $(seq 1 120); do
      kill -0 "$APP_PID" 2>/dev/null || break
      if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$HP/ready" 2>/dev/null)" = "200" ]; then
        READY=1; break
      fi
      sleep 1
    done
    TOTAL=$(( $(date +%s) - BOOT_TS ))
    JOBS=$(python3 - <<'PY' 2>/dev/null
import json, os
try:
    p = os.path.expanduser("~/.picoclaw/workspace/cron/jobs.json")
    print(len(json.load(open(p)).get("jobs", [])))
except Exception:
    print(0)
PY
)
    PKGS=$(cat "$INST/pip.list" 2>/dev/null | wc -l)
    FILES=$(find ~/.picoclaw ~/.picow_clow -type f 2>/dev/null | wc -l)
    if [ "$READY" = "1" ] && [ "$RESTORE_OK" = "1" ]; then
      notify "✅ PicoClaw ready! Restore पूर्ण, history आणि सगळं परत आलंय. आता message पाठवू शकता.
⏱ Restore: ${RESTORE_SECS}s | एकूण boot: ${TOTAL}s
📁 Restored files: ${FILES} | ⏰ Cron jobs: ${JOBS} | 📦 Persisted pip pkgs: ${PKGS}"
    elif [ "$READY" = "1" ] && [ "$CLEAN_START" = "1" ]; then
      notify "✅ PicoClaw ready - पण CLEAN history ने (Drive restore फेल झाला). Boot: ${TOTAL}s
ℹ️ या run ची history Drive ला save होणार नाही. Drive ठीक झाल्यावर restart करा."
    elif [ "$READY" = "1" ]; then
      notify "⚠️ PicoClaw चालू आहे, पण Drive restore पूर्ण नाही झाला. (${TOTAL}s)"
    elif kill -0 "$APP_PID" 2>/dev/null; then
      notify "⚠️ PicoClaw process चालू आहे पण /ready check 120s मध्ये आला नाही. Koyeb logs बघा. (Restore: $([ "$RESTORE_OK" = "1" ] && echo OK || echo NOT-OK))"
    else
      notify "❌ PicoClaw start होताच बंद पडला. Koyeb logs तपासा."
    fi
  ) &

  wait "$APP_PID"
elif [ -f /app/main.py ]; then
  echo ">> Starting main.py..."
  python3 /app/main.py
elif [ -f /app/app.py ]; then
  echo ">> Starting app.py..."
  python3 /app/app.py
else
  echo ">> PicoClaw not started (check TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOW_FROM / LLM_API_KEY). Keeping container alive..."
  notify "❌ PicoClaw start झाला नाही (setup फेल). TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOW_FROM / LLM_API_KEY तपासा. Restore: $([ "$RESTORE_OK" = "1" ] && echo OK || echo NOT-OK)"
  sleep infinity
fi
