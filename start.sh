#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export TZ="Asia/Kolkata"

# Mirror everything this script prints (= what the Render/Koyeb log page shows) into /tmp/server.log, so the
# Telegram /log command can send the complete server log. Output still goes to the platform log unchanged.
rm -f /tmp/server.log
exec > >(tee -a /tmp/server.log) 2>&1

echo "=== [PicoW Clow Koyeb Instant Startup & Background Sync] ==="

rm -f /tmp/shutting_down /tmp/restore_progress.json /tmp/restore.log /tmp/setup_picoclaw.log /tmp/picoclaw.log /tmp/stream_proxy.log /tmp/backup_now /tmp/backup_status.json

# 1. START KEEP-ALIVE SERVER INSTANTLY
if [ -f /app/keep_alive.py ]; then
  echo ">> Starting keep-alive HTTP server immediately..."
  # supervised: if it ever crashes the platform health check must not start failing until the next redeploy
  (
    while [ ! -f /tmp/shutting_down ]; do
      python3 /app/keep_alive.py
      [ -f /tmp/shutting_down ] && break
      echo ">> keep_alive.py exited - restarting in 2s"
      sleep 2
    done
  ) &
fi

# 1a. RAM GUARD: kills the agent job that spikes RAM >= 91% (before the Linux OOM killer), supervised.
if [ -f /app/ram_guard.py ] && [ "${RAM_GUARD:-1}" != "0" ]; then
  (
    while [ ! -f /tmp/shutting_down ]; do
      python3 /app/ram_guard.py >> /tmp/ram_guard.log 2>&1
      sleep 2
    done
  ) &
fi

BOOT_TS=$(date +%s)
notify() { python3 /app/notify.py "$1" >/dev/null 2>&1 || true; }

run_logged() {
  local logfile="$1"
  shift
  "$@" > "$logfile" 2>&1
  local rc=$?
  cat "$logfile"
  return $rc
}

collect_error_logs() {
  python3 - <<'PY' 2>/dev/null
from pathlib import Path

sections = [
    ("picoclaw gateway", "/tmp/picoclaw.log", 35),
    ("setup_picoclaw.py", "/tmp/setup_picoclaw.log", 20),
    ("llm_proxy.py", "/tmp/stream_proxy.log", 15),
    ("rclone restore", "/tmp/restore.log", 15),
]
out = []
for title, path, nlines in sections:
    p = Path(path)
    if p.is_file():
        lines = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        if lines:
            tail = "\n".join(lines[-nlines:])
            out.append(f"--- {title} ---\n{tail}")
print("\n\n".join(out) if out else "(कोणताही error log सापडला नाही)")
PY
}

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
      for p in $(echo "${TAILSCALE_PORTS:-10000,18790,18800,${FS_PORT:-8765}}" | tr ',' ' '); do
        if $TS serve --bg --tcp="$p" "tcp://127.0.0.1:$p" >/dev/null 2>&1; then
          echo ">> [tailscale] forwarding tailnet port $p -> 127.0.0.1:$p"
        else
          echo ">> [tailscale] could not forward port $p"
        fi
      done
      # publish EVERY listening port (PicoClaw gateway/UI, dashboards, ...) automatically + index page on :8099
      echo ">> [tailscale] starting port watcher (all listening ports -> tailnet)"
      tsports watch >> /tmp/tsports.log 2>&1 &
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

RCLONE_EXCLUDES=(--exclude "cache/**" --exclude "audio_cache/**" --exclude "image_cache/**" --exclude "runtime/**" --exclude "node_modules/**" --exclude ".env")
rclone_sync() {
  rclone "$@" "${RCLONE_EXCLUDES[@]}"
}

# PicoClaw state (sessions, memory, workspace) is backed up next to the rest.
# config.json / .security.yml hold the bot token + API key, so they are NOT
# synced to Drive - they are regenerated from env vars on every boot.
PICOCLAW_REMOTE="${REMOTE_BACKUP}/picoclaw"
PICOCLAW_EXCLUDES=(--exclude "config.json" --exclude ".security.yml" --exclude "*.log" --exclude "*.pid" --exclude "*.lock" --exclude "*.sock" --exclude "cache/**")
picoclaw_sync() {
  rclone "$@" "${PICOCLAW_EXCLUDES[@]}"
}

# Python packages (~/.picow_clow/pyuser) are thousands of tiny files. Google Drive handles ~1 small file per second,
# so restoring them one by one takes hours. They are therefore stored on Drive as ONE archive
# (archives/pyuser.tar.gz) and the pyuser/ folder itself is excluded from the file-by-file sync.
ARCHIVE_DIR=~/.picow_clow/archives
PYUSER_ARCHIVE="$ARCHIVE_DIR/pyuser.tar.gz"
USE_PYUSER_ARCHIVE=0

pyuser_fingerprint() {  # changes when a package file is added / removed / modified (names, sizes, mtimes in seconds)
  find "$PYTHONUSERBASE" -type f -not -path '*/__pycache__/*' -printf '%P %s %Ts\n' 2>/dev/null | sort | md5sum | cut -d' ' -f1
}

# pack_pyuser <full|quick>: returns 0 when a usable archive exists (so pyuser/ can be excluded from the file sync).
#   full  = repack first when packages changed (periodic sync; low priority, runs in the background)
#   quick = never repack (initial + shutdown sync must be fast); the last archive stays valid
pack_pyuser() {
  local mode="${1:-full}" fp tmp
  if [ "$mode" = "quick" ]; then
    [ -f "$PYUSER_ARCHIVE" ] && return 0
    return 1
  fi
  [ -d "$PYTHONUSERBASE" ] && [ -n "$(find "$PYTHONUSERBASE" -type f -print -quit 2>/dev/null)" ] || return 1
  fp=$(pyuser_fingerprint)
  if [ -f "$PYUSER_ARCHIVE" ] && [ "$(cat "$ARCHIVE_DIR/pyuser.fp" 2>/dev/null)" = "$fp" ]; then
    return 0
  fi
  mkdir -p "$ARCHIVE_DIR"
  tmp="$PYUSER_ARCHIVE.tmp"
  echo ">> Packing Python packages into one archive..."
  if nice -n 19 tar -C ~/.picow_clow --exclude='__pycache__' -I 'gzip -1' -cf "$tmp" pyuser 2>/dev/null; then
    mv -f "$tmp" "$PYUSER_ARCHIVE" && echo "$fp" > "$ARCHIVE_DIR/pyuser.fp"
    echo ">> Python packages archive: $(du -h "$PYUSER_ARCHIVE" | cut -f1)"
    return 0
  fi
  rm -f "$tmp"
  echo ">> Packing Python packages failed - they stay in the file-by-file sync"
  return 1
}

unpack_pyuser() {  # after a successful restore that used the archive
  [ "$USE_PYUSER_ARCHIVE" = "1" ] && [ -f "$PYUSER_ARCHIVE" ] || return 0
  echo ">> Unpacking Python packages archive..."
  progress_stage unpacking "Unpacking saved Python packages..."
  rm -rf "$PYTHONUSERBASE"
  if tar -C ~/.picow_clow -I 'gzip -d' -xf "$PYUSER_ARCHIVE"; then
    echo ">> Python packages unpacked."
  else
    echo ">> Unpacking Python packages FAILED - they will be re-installed from pip.list if recorded there"
    rm -rf "$PYTHONUSERBASE"
  fi
}

# Live restore progress for the /memory dashboard (percent, MB, speed, time left). See restore_progress.py.
progress_stage() {  # progress_stage <stage> <message> [--clean-start]
  [ -f /app/restore_progress.py ] || return 0
  python3 /app/restore_progress.py --stage "$1" --message "$2" ${3:-} >/dev/null 2>&1 || true
}

# Restore speed. rclone defaults (4 transfers, 8 checkers, Drive pacer 100ms = ~10 requests/s) are very slow for a backup made
# of many small files. Tunable with RESTORE_TRANSFERS / RESTORE_CHECKERS / RESTORE_EXTRA_FLAGS. The faster Drive pacer is used
# ONLY when your own Google API client_id is in the rclone config: the shared default rclone client is throttled by Google
# and would answer with 403 rateLimitExceeded errors.
RESTORE_SPEED=(--transfers "${RESTORE_TRANSFERS:-8}" --checkers "${RESTORE_CHECKERS:-16}")
if grep -qE '^[[:space:]]*client_id[[:space:]]*=[[:space:]]*[^[:space:]]' ~/.config/rclone/rclone.conf 2>/dev/null; then
  RESTORE_SPEED+=(--drive-pacer-min-sleep 10ms --drive-pacer-burst 200)
  _SECRET=$(grep -cE '^[[:space:]]*client_secret[[:space:]]*=[[:space:]]*[^[:space:]]' ~/.config/rclone/rclone.conf 2>/dev/null)
  if [ "${_SECRET:-0}" -gt 0 ]; then
    echo ">> [rclone] own Google client_id + client_secret found -> fast Drive mode ON"
  else
    echo ">> [rclone] client_id found but client_secret is MISSING -> add client_secret too (fast mode may fail)"
  fi
elif [ -f ~/.config/rclone/rclone.conf ]; then
  echo ">> [rclone] NO own client_id in the rclone config -> using rclone's shared Google client (slow, ~1 small file/s). See README 'Restore speed'."
fi
# shellcheck disable=SC2206
[ -n "${RESTORE_EXTRA_FLAGS:-}" ] && RESTORE_SPEED+=(${RESTORE_EXTRA_FLAGS})

# One restore attempt: both rclone syncs, with live progress. Falls back to the plain commands if the helper is missing.
do_restore() {
  local px=()
  USE_PYUSER_ARCHIVE=0
  if rclone lsf "$REMOTE_BACKUP/archives" --files-only 2>/dev/null | grep -qx 'pyuser.tar.gz'; then
    px=(--exclude "pyuser/**"); USE_PYUSER_ARCHIVE=1
    echo ">> Python packages come from one archive (fast path)"
  else
    echo ">> No packages archive on Drive yet - restoring pyuser/ file by file (slow, one time; an archive is made after the first periodic sync)"
  fi
  if [ -f /app/restore_progress.py ]; then
    python3 /app/restore_progress.py --attempt "$ATTEMPT" --attempts "${RESTORE_MAX_ATTEMPTS:-2}" -- \
      "History and settings" rclone sync "$REMOTE_BACKUP" ~/.picow_clow/ "${RCLONE_EXCLUDES[@]}" --exclude "picoclaw/**" "${px[@]}" --drive-chunk-size 8M "${RESTORE_SPEED[@]}" \
      +++ "PicoClaw data" rclone sync "$PICOCLAW_REMOTE" ~/.picoclaw/ "${PICOCLAW_EXCLUDES[@]}" --drive-chunk-size 8M "${RESTORE_SPEED[@]}"
  else
    rclone_sync sync "$REMOTE_BACKUP" ~/.picow_clow/ --drive-chunk-size 8M --exclude "picoclaw/**" "${px[@]}" "${RESTORE_SPEED[@]}" \
      && picoclaw_sync sync "$PICOCLAW_REMOTE" ~/.picoclaw/ --drive-chunk-size 8M "${RESTORE_SPEED[@]}"
  fi
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
    : > /tmp/restore.log
    if do_restore >> /tmp/restore.log 2>&1; then
      cat /tmp/restore.log
      SYNC_SAFE=1; RESTORE_OK=1
      RESTORE_SECS=$(( $(date +%s) - RESTORE_START ))
      echo ">> Restore OK (${RESTORE_SECS}s)."
      break
    fi
    cat /tmp/restore.log
    RESTORE_ERR=$(tail -n 25 /tmp/restore.log 2>/dev/null)
    echo ">> Restore FAILED (attempt $ATTEMPT)."
    if [ "${RESTORE_MAX_ATTEMPTS:-2}" -gt 0 ] && [ "$ATTEMPT" -ge "${RESTORE_MAX_ATTEMPTS:-2}" ] \
       && [ "${ALLOW_START_WITHOUT_RESTORE:-1}" = "1" ]; then
      echo ">> Restore failed $ATTEMPT times -> wiping partial local state and starting with a CLEAN history."
      # Local-only wipe (container disk). Drive is untouched because SYNC_SAFE stays 0.
      find ~/.picoclaw ~/.picow_clow -mindepth 1 -delete 2>/dev/null || true
      mkdir -p ~/.picow_clow ~/.picoclaw
      CLEAN_START=1
      progress_stage clean_start "Restore failed ${ATTEMPT} times - starting with a CLEAN history" --clean-start
      notify "🧹 Restore $ATTEMPT वेळा फेल झाला, म्हणून PicoClaw CLEAN history ने start होतोय (जुनी history या वेळी नाही).
⚠️ Drive वरचा जुना backup सुरक्षित ठेवण्यासाठी या run चा Drive sync बंद आहे. Drive ठीक झाल्यावर Koyeb मध्ये redeploy/restart केला की जुनी history परत येईल.

📋 Error Log:
${RESTORE_ERR:-(कोणताही error log सापडला नाही)}"
      break
    fi
    [ "$ATTEMPT" = "1" ] && notify "⚠️ Drive restore फेल झाला (attempt 1). PicoClaw अजून start केलेला नाही, परत प्रयत्न करतोय...

📋 Error Log:
${RESTORE_ERR:-(कोणताही error log सापडला नाही)}"
    progress_stage retrying "Restore failed (attempt ${ATTEMPT}) - retrying in 20s"
    sleep 20
  done
else
  progress_stage no_restore "No rclone config - nothing to restore (fresh state)"
  echo ">> No rclone config - nothing to restore (fresh state)."
  notify "⚠️ rclone config नाही, म्हणून Drive restore झालं नाही (fresh state). RCLONE_CONFIG_BASE64 तपासा."
fi

[ "$RESTORE_OK" = "1" ] && unpack_pyuser
mkdir -p "$PYTHONUSERBASE"

# 3b. Replay packages installed earlier via the bin/ wrappers (apt/pip/npm)
INST=~/.picow_clow/installed
{ [ -s "$INST/apt.list" ] || [ -s "$INST/pip.list" ] || [ -s "$INST/npm.list" ]; } && progress_stage packages "Re-installing saved packages..."
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
# Every run is recorded by backup_status.py (start / result / duration / error): the /memory dashboard shows
# "last backup ... ago", a failing backup raises a red alert, and its "Backup now" button creates $BACKUP_TRIGGER.
BACKUP_INTERVAL="${BACKUP_INTERVAL:-300}"
BACKUP_TRIGGER="${BACKUP_TRIGGER_FILE:-/tmp/backup_now}"
bstatus() { python3 /app/backup_status.py "$@" >/dev/null 2>&1 || true; }

sync_to_cloud() {  # sync_to_cloud <full|quick>
  if [ -f ~/.config/rclone/rclone.conf ] && [ "$SYNC_SAFE" = "1" ]; then
    local px=() t0 rc1=0 rc2=0 e1=/tmp/backup_err1.log e2=/tmp/backup_err2.log err
    t0=$(date +%s)
    bstatus start "${1:-full}"
    pack_pyuser "${1:-full}" && px=(--exclude "pyuser/**")
    rclone mkdir "$REMOTE_BACKUP" 2>/dev/null || true
    # stderr goes to a file first so the exit code is not lost (it used to be swallowed by `|| true`), then to the log
    rclone_sync sync ~/.picow_clow/ "$REMOTE_BACKUP" --drive-chunk-size 8M --fast-list --exclude "picoclaw/**" "${px[@]}" 2>"$e1" || rc1=$?
    cat "$e1" >&2
    picoclaw_sync sync ~/.picoclaw/ "$PICOCLAW_REMOTE" --drive-chunk-size 8M --fast-list 2>"$e2" || rc2=$?
    cat "$e2" >&2
    if [ "$rc1" = "0" ] && [ "$rc2" = "0" ]; then
      bstatus done "${1:-full}" ok $(( $(date +%s) - t0 ))
    else
      err=$(cat "$e1" "$e2" 2>/dev/null | grep -a -E 'ERROR|Failed|Fatal' | tail -n 3 | cut -c1-300 | tr '\n' ' ')
      echo ">> [backup] FAILED (rclone exit codes: history=$rc1 picoclaw=$rc2) $err"
      bstatus done "${1:-full}" fail $(( $(date +%s) - t0 )) "rclone exit $rc1/$rc2: $err"
    fi
  else
    bstatus disabled "the Drive restore did not finish, so syncing is switched off to protect the existing backup"
  fi
}

echo ">> Initial sync to Drive..."
sync_to_cloud quick

(
  while true; do
    # wait BACKUP_INTERVAL seconds, but start at once when the dashboard's "Backup now" button created the trigger file
    waited=0
    while [ "$waited" -lt "$BACKUP_INTERVAL" ] && [ ! -f "$BACKUP_TRIGGER" ]; do
      sleep 1
      waited=$((waited + 1))
    done
    rm -f "$BACKUP_TRIGGER"
    sync_to_cloud full
  done
) &
SYNC_PID=$!

# 5. Trap for graceful shutdown
APP_PID=""
TAIL_PID=""
cleanup() {
  trap - SIGTERM SIGINT EXIT
  touch /tmp/shutting_down
  echo ">> Container shutting down. Performing final sync..."
  [ -n "$TAIL_PID" ] && kill "$TAIL_PID" 2>/dev/null || true
  [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null || true
  kill $SYNC_PID 2>/dev/null || true
  pkill -TERM -P "$SYNC_PID" 2>/dev/null || true     # an rclone from the loop would otherwise race the final sync
  sync_to_cloud quick
  exit 0
}
trap cleanup SIGTERM SIGINT EXIT

# 6. Start PicoClaw gateway (Telegram long-polling, no inbound port needed)
if command -v picoclaw >/dev/null 2>&1 && run_logged /tmp/setup_picoclaw.log python3 /app/setup_picoclaw.py; then
  echo ">> Starting PicoClaw gateway (Telegram)..."
  echo ">> PicoClaw build: $(cat /etc/picoclaw-version 2>/dev/null || echo unknown)  (\"+summaryfix\" = summary patch active)"
  : > /tmp/picoclaw.log
  tail -n +1 -f /tmp/picoclaw.log &
  TAIL_PID=$!
  picoclaw gateway >> /tmp/picoclaw.log 2>&1 &
  APP_PID=$!
  # bring back dashboards published earlier with `hdash start` (waits for Tailscale itself)
  ( hdash restore >> /tmp/dash.log 2>&1 & ) 2>/dev/null

  # Wait up to 120s in the main shell to verify PicoClaw is up (gateway /ready == 200) or detect early crash.
  HP="${PICOCLAW_HEALTH_PORT:-18790}"
  READY=0
  for _ in $(seq 1 120); do
    [ -f /tmp/shutting_down ] && break
    kill -0 "$APP_PID" 2>/dev/null || break
    if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$HP/ready" 2>/dev/null)" = "200" ]; then
      READY=1; break
    fi
    sleep 1
  done

  if [ ! -f /tmp/shutting_down ]; then
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
      ERR_LOG=$(collect_error_logs)
      notify "⚠️ PicoClaw process चालू आहे पण /ready check 120s मध्ये आला नाही. (Restore: $([ "$RESTORE_OK" = "1" ] && echo OK || echo NOT-OK))

📋 Error Log:
${ERR_LOG}"
    else
      wait "$APP_PID" 2>/dev/null
      EXIT_CODE=$?
      sleep 1
      ERR_LOG=$(collect_error_logs)
      echo ">> PicoClaw exited on startup with code $EXIT_CODE"
      notify "❌ PicoClaw start होताच बंद पडला (exit code: ${EXIT_CODE}).

📋 Error Log:
${ERR_LOG}"
      sleep infinity
    fi
  fi

  # Supervise the gateway: if it crashes (panic / OOM kill) restart it automatically instead of leaving the bot dead
  # until someone redeploys. A run that lasted > 10 min resets the counter; > PICOCLAW_MAX_RESTARTS quick crashes in a
  # row = give up (crash loop) and tell the owner.
  MAX_RESTARTS="${PICOCLAW_MAX_RESTARTS:-20}"
  RESTARTS=0
  RUN_START=$(date +%s)
  while true; do
    wait "$APP_PID"
    EXIT_CODE=$?
    [ -f /tmp/shutting_down ] && break
    sleep 1
    RAN=$(( $(date +%s) - RUN_START ))
    if [ "$RAN" -gt 600 ]; then RESTARTS=1; else RESTARTS=$((RESTARTS + 1)); fi
    ERR_LOG=$(collect_error_logs)
    echo ">> PicoClaw exited unexpectedly with code $EXIT_CODE after ${RAN}s (restart #$RESTARTS)"
    if [ "$RESTARTS" -gt "$MAX_RESTARTS" ]; then
      notify "❌ PicoClaw वारंवार बंद पडतोय (${MAX_RESTARTS} वेळा, exit code: ${EXIT_CODE}). Auto-restart थांबवला.

📋 Error Log:
${ERR_LOG}"
      sleep infinity
    fi
    notify "⚠️ PicoClaw अचानक बंद पडला (exit code: ${EXIT_CODE}, ${RAN}s नंतर) — आपोआप परत सुरू करतोय (#${RESTARTS}). चालू काम असेल तर 'continue' पाठवा.

📋 Error Log:
${ERR_LOG}"
    sleep 3
    picoclaw gateway >> /tmp/picoclaw.log 2>&1 &
    APP_PID=$!
    RUN_START=$(date +%s)
  done
elif [ -f /app/main.py ]; then
  echo ">> Starting main.py..."
  python3 /app/main.py
elif [ -f /app/app.py ]; then
  echo ">> Starting app.py..."
  python3 /app/app.py
else
  if ! command -v picoclaw >/dev/null 2>&1; then
    echo "picoclaw binary not found in PATH ($PATH)" >> /tmp/setup_picoclaw.log
  fi
  ERR_LOG=$(collect_error_logs)
  echo ">> PicoClaw not started (check TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOW_FROM / LLM_API_KEY). Keeping container alive..."
  notify "❌ PicoClaw start झाला नाही (setup फेल). TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOW_FROM / LLM_API_KEY तपासा. Restore: $([ "$RESTORE_OK" = "1" ] && echo OK || echo NOT-OK)

📋 Error Log:
${ERR_LOG}"
  sleep infinity
fi
