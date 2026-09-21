#!/bin/bash

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export TZ="Asia/Kolkata"

# ============================================================
# HERMES AUTO-PILOT — Lightweight (512MB RAM / 2GB Disk)
# फक्त जे लागतं तेच restore/backup — बाकी skip
# ============================================================

log() {
    echo ">> [$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# ── 1. KEEP-ALIVE (सगळ्यात आधी)
if [ -f /app/keep_alive.py ]; then
    log "Keep-alive starting..."
    python3 /app/keep_alive.py &
    KEEPALIVE_PID=$!
fi

# ── 2. ENVIRONMENT
if [ -z "$OPENAI_API_KEY" ]; then
    log "ERROR: OPENAI_API_KEY not set!"
    wait $KEEPALIVE_PID
    exit 1
fi

# Core variables — env मधून येतात, hardcoded नाहीत
export OPENAI_API_BASE="${OPENAI_API_BASE:-https://unknown44.onrender.com/v1/}"
export UNKNOWN44_API_KEY="${UNKNOWN44_API_KEY:-${OPENAI_API_KEY}}"
export CUSTOM_API_KEY="${CUSTOM_API_KEY:-${OPENAI_API_KEY}}"
export MODEL_PROVIDER="${MODEL_PROVIDER:-custom}"
export MODEL_DEFAULT="${MODEL_DEFAULT:-gemini-pro}"
export api_key="${OPENAI_API_KEY}"

log "Environment loaded."

# ── 3. RCLONE SETUP
mkdir -p ~/.config/rclone ~/.hermes

if [ -n "$RCLONE_CONFIG_BASE64" ]; then
    echo "$RCLONE_CONFIG_BASE64" | base64 -d > ~/.config/rclone/rclone.conf
    log "Rclone configured from RCLONE_CONFIG_BASE64."
elif [ -n "$RCLONE_CONFIG" ]; then
    echo "$RCLONE_CONFIG" > ~/.config/rclone/rclone.conf
    log "Rclone configured from RCLONE_CONFIG."
fi

REMOTE_BACKUP="${RCLONE_REMOTE:-gdrive:hermes_backup}"
RCLONE_AVAILABLE=false
[ -f ~/.config/rclone/rclone.conf ] && RCLONE_AVAILABLE=true

# ── फक्त हे folders restore/backup होतील (lightweight)
# plugins/, node/, bin/, cache/ — SKIP (heavy, disk भरेल)
ESSENTIAL_INCLUDES=(
    "--include=state/**"
    "--include=cron/**"
    "--include=config/**"
    "--include=auth/**"
    "--include=skills/email/**"
    "--include=*.json"
    "--include=*.toml"
    "--include=*.yaml"
    "--include=*.yml"
    "--include=.env"
)

# Env मधून override — नवीन folders add करायचे असतील तर
# EXTRA_BACKUP_INCLUDES="--include=memory/** --include=logs/**"
EXTRA_INCLUDES="${EXTRA_BACKUP_INCLUDES:-}"

# ── CLEANUP
clean_partial_files() {
    find ~/.hermes -name "*.partial" -delete 2>/dev/null || true
}

# Disk मधून फक्त confirmed-safe temporary folders delete करतो
clean_heavy_dirs() {
    log "Disk cleanup — safe temporary files delete करतो..."

    # Cache folders — 100% safe, regenerate होतात
    rm -rf ~/.hermes/cache \
           ~/.hermes/audio_cache \
           ~/.hermes/image_cache \
           ~/.hermes/tmp \
           ~/.hermes/runtime/tmp 2>/dev/null || true

    # Python bytecode — 100% safe, Python आपोआप परत बनवतो
    find ~/.hermes -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    find ~/.hermes -name "*.pyc" -delete 2>/dev/null || true

    # .partial files — corrupt/incomplete files
    clean_partial_files

    local DISK_USED
    DISK_USED=$(du -sm ~/.hermes 2>/dev/null | cut -f1)
    log "Disk cleanup done. ~/.hermes = ${DISK_USED}MB"
}

# ── RESTORE (फक्त essential files, timeout 60s)
rclone_restore() {
    if [ "$RCLONE_AVAILABLE" = true ]; then
        log "Restoring essential files from Google Drive..."
        timeout 60s rclone copy "$REMOTE_BACKUP" ~/.hermes/ \
            "${ESSENTIAL_INCLUDES[@]}" \
            $EXTRA_INCLUDES \
            --ignore-checksum \
            --ignore-errors \
            --transfers 8 \
            2>&1 | grep -v "NOTICE:" || true

        [ $? -eq 124 ] && log "WARNING: Restore timeout (60s) — continuing anyway."
        clean_partial_files
        log "Restore done."
    fi
}

# ── BACKUP (फक्त essential files)
rclone_backup() {
    if [ "$RCLONE_AVAILABLE" = true ]; then
        timeout 90s rclone sync ~/.hermes/ "$REMOTE_BACKUP" \
            "${ESSENTIAL_INCLUDES[@]}" \
            $EXTRA_INCLUDES \
            --ignore-checksum \
            --ignore-errors \
            --fast-list \
            2>&1 | grep -v "NOTICE:" || true
    fi
}

# ── 4. HERMES CONFIG WRITE (env variables मधून — hardcoded नाही)
write_hermes_config() {
    # .env — env variables मधून येतं
    cat > ~/.hermes/.env <<EOF
OPENAI_API_KEY=${OPENAI_API_KEY}
UNKNOWN44_API_KEY=${UNKNOWN44_API_KEY}
CUSTOM_API_KEY=${CUSTOM_API_KEY}
OPENAI_API_BASE=${OPENAI_API_BASE}
MODEL_PROVIDER=${MODEL_PROVIDER}
MODEL_DEFAULT=${MODEL_DEFAULT}
EOF

    # Cron config — env मधून override होतं
    # HERMES_CRON_SCHEDULE env set केला तर तो वापरेल
    if [ -n "$HERMES_CRON_SCHEDULE" ] && [ -d ~/.hermes/cron ]; then
        log "Cron schedule override: $HERMES_CRON_SCHEDULE"
        # cron config असेल तर schedule update करा
        find ~/.hermes/cron -name "*.json" | while read f; do
            # JSON मध्ये schedule field असेल तर override
            if command -v python3 &>/dev/null; then
                python3 -c "
import json, sys
try:
    with open('$f') as fp:
        d = json.load(fp)
    if 'schedule' in d:
        d['schedule'] = '$HERMES_CRON_SCHEDULE'
        with open('$f', 'w') as fp:
            json.dump(d, fp, indent=2)
        print('Updated schedule in $f')
except: pass
" 2>/dev/null || true
            fi
        done
    fi

    # config.yaml patches — warnings fix करण्यासाठी
    mkdir -p ~/.hermes
    CONFIG_YAML=~/.hermes/config.yaml

    if [ -f "$CONFIG_YAML" ]; then
        # SQLite: delete mode stick करायचा (WAL warning बंद होईल)
        if ! grep -q "journal_mode" "$CONFIG_YAML"; then
            echo "" >> "$CONFIG_YAML"
            echo "database:" >> "$CONFIG_YAML"
            echo "  journal_mode: delete" >> "$CONFIG_YAML"
            log "config.yaml: journal_mode: delete added."
        fi
        # Model context length fix
        if ! grep -q "context_length" "$CONFIG_YAML"; then
            echo "" >> "$CONFIG_YAML"
            echo "model:" >> "$CONFIG_YAML"
            echo "  context_length: 128000" >> "$CONFIG_YAML"
            log "config.yaml: context_length: 128000 added."
        fi
    else
        cat > "$CONFIG_YAML" <<YAMLEOF
database:
  journal_mode: delete
model:
  context_length: 128000
YAMLEOF
        log "config.yaml created with defaults."
    fi

    # Himalaya symlink
    mkdir -p ~/.config/himalaya
    if [ -f ~/.hermes/skills/email/himalaya/config.toml ]; then
        ln -sf ~/.hermes/skills/email/himalaya/config.toml ~/.config/himalaya/config.toml
    fi
}

setup_hermes_auth() {
    log "Setting up Hermes auth..."
    hermes config set terminal.backend local 2>/dev/null || true
    hermes auth add custom \
        --type api-key \
        --api-key "${OPENAI_API_KEY}" \
        --inference-url "${OPENAI_API_BASE}" 2>/dev/null || true
    log "Hermes auth done."
}

# ── 5. STARTUP
rclone_restore
clean_heavy_dirs   # ← disk वाचवतो — restore नंतर लगेच
write_hermes_config
setup_hermes_auth

# ── 6. BACKGROUND SYNC (दर 1 मिनिट — फक्त essential files)
(
    while true; do
        sleep 60
        rclone_backup
    done
) &
SYNC_PID=$!
log "Background sync started (PID: $SYNC_PID) — essential files only"

# ── 7. WATCHDOG
(
    while true; do
        sleep 120
        if ! pgrep -f "hermes gateway" > /dev/null 2>&1; then
            log "[WATCHDOG] Hermes missing — restart loop handle करेल."
        else
            log "[WATCHDOG] Hermes OK."
        fi
    done
) &
WATCHDOG_PID=$!

# ── 8. GRACEFUL SHUTDOWN
cleanup() {
    log "Shutdown — final backup..."
    kill $SYNC_PID 2>/dev/null || true
    kill $WATCHDOG_PID 2>/dev/null || true
    pkill -f "hermes gateway" 2>/dev/null || true
    rclone_backup
    log "Done."
    exit 0
}
trap cleanup SIGTERM SIGINT EXIT

# ── 9. AUTO-RESTART LOOP
CRASH_COUNT=0
CONSECUTIVE_FAST_CRASHES=0

log "=========================================="
log "Hermes Auto-Pilot ACTIVE"
log "Disk usage: $(du -sh ~/.hermes 2>/dev/null | cut -f1) used"
log "=========================================="

while true; do
    LAST_START=$(date +%s)
    CRASH_COUNT=$((CRASH_COUNT + 1))
    log "Hermes starting... (attempt #$CRASH_COUNT)"

    hermes gateway run
    EXIT_CODE=$?

    UPTIME=$(( $(date +%s) - LAST_START ))
    log "Hermes exited (code: $EXIT_CODE, uptime: ${UPTIME}s)"

    if [ $UPTIME -lt 30 ]; then
        CONSECUTIVE_FAST_CRASHES=$((CONSECUTIVE_FAST_CRASHES + 1))
        log "Fast crash #$CONSECUTIVE_FAST_CRASHES"

        if [ $CONSECUTIVE_FAST_CRASHES -ge 3 ]; then
            log "3 fast crashes — fresh restore..."
            pkill -f "hermes" 2>/dev/null || true
            sleep 2
            clean_partial_files
            rclone_restore
            write_hermes_config
            setup_hermes_auth
            CONSECUTIVE_FAST_CRASHES=0
            sleep 5
        else
            sleep 10
        fi
    else
        CONSECUTIVE_FAST_CRASHES=0
        sleep 5
    fi
done

cleanup
