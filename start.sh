#!/bin/bash

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export TZ="Asia/Kolkata"

# ============================================================
# HERMES AUTO-PILOT — Lightweight (512MB RAM / 2GB Disk)
# Drive connected असेल तरच backup/restore
# Cache/temp कधीच backup होत नाही
# Restart नंतर auto-restore, processes बंद होत नाहीत
# ============================================================

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# ── 1. KEEP-ALIVE (सगळ्यात आधी — कधीच बंद होणार नाही)
if [ -f /app/keep_alive.py ]; then
    python3 /app/keep_alive.py &
    KEEPALIVE_PID=$!
    log "Keep-alive started (PID: $KEEPALIVE_PID)"
fi

# ── 2. ENVIRONMENT CHECK
if [ -z "$OPENAI_API_KEY" ]; then
    log "ERROR: OPENAI_API_KEY not set — exiting."
    wait $KEEPALIVE_PID
    exit 1
fi

export OPENAI_API_BASE="${OPENAI_API_BASE:-https://unknown44.onrender.com/v1/}"
export UNKNOWN44_API_KEY="${UNKNOWN44_API_KEY:-${OPENAI_API_KEY}}"
export CUSTOM_API_KEY="${CUSTOM_API_KEY:-${OPENAI_API_KEY}}"
export MODEL_PROVIDER="${MODEL_PROVIDER:-custom}"
export MODEL_DEFAULT="${MODEL_DEFAULT:-gemini-pro}"
export api_key="${OPENAI_API_KEY}"

log "Environment ready."

# ── 3. RCLONE SETUP
mkdir -p ~/.config/rclone ~/.hermes

if [ -n "$RCLONE_CONFIG_BASE64" ]; then
    echo "$RCLONE_CONFIG_BASE64" | base64 -d > ~/.config/rclone/rclone.conf
    log "Rclone config loaded from RCLONE_CONFIG_BASE64."
elif [ -n "$RCLONE_CONFIG" ]; then
    echo "$RCLONE_CONFIG" > ~/.config/rclone/rclone.conf
    log "Rclone config loaded from RCLONE_CONFIG."
fi

REMOTE_BACKUP="${RCLONE_REMOTE:-gdrive:hermes_backup}"

# Drive खरंच connect आहे का ते check करतो — एकदाच
DRIVE_OK=false
check_drive() {
    if [ ! -f ~/.config/rclone/rclone.conf ]; then
        return 1
    fi
    if timeout 15s rclone lsd "$REMOTE_BACKUP" > /dev/null 2>&1; then
        return 0
    else
        return 1
    fi
}

if check_drive; then
    DRIVE_OK=true
    log "Google Drive connected — backup/restore enabled."
else
    log "Google Drive not available — running without backup."
fi

# ── फक्त हे backup होतील (cache/temp कधीच नाही)
BACKUP_INCLUDES=(
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

# Env मधून extra folders — EXTRA_BACKUP_INCLUDES="--include=memory/**"
EXTRA_INCLUDES="${EXTRA_BACKUP_INCLUDES:-}"

# ── CLEANUP — cache/temp/bytecode delete
clean_junk() {
    rm -rf ~/.hermes/cache \
           ~/.hermes/audio_cache \
           ~/.hermes/image_cache \
           ~/.hermes/tmp \
           ~/.hermes/runtime/tmp 2>/dev/null || true
    find ~/.hermes -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    find ~/.hermes -name "*.pyc" -delete 2>/dev/null || true
    find ~/.hermes -name "*.partial" -delete 2>/dev/null || true
}

# ── RESTORE — Drive available असेल तरच, एकदाच
rclone_restore() {
    if [ "$DRIVE_OK" != "true" ]; then
        log "Drive not available — restore skipped."
        return 0
    fi

    log "Restoring from Google Drive..."
    timeout 90s rclone copy "$REMOTE_BACKUP" ~/.hermes/ \
        "${BACKUP_INCLUDES[@]}" \
        $EXTRA_INCLUDES \
        --ignore-checksum \
        --ignore-errors \
        --transfers 8 \
        2>&1 | grep -E "^(ERROR|CRITICAL)" || true

    local EXIT=$?
    if [ $EXIT -eq 124 ]; then
        log "Restore timeout (90s) — continuing with what was downloaded."
    elif [ $EXIT -ne 0 ]; then
        log "Restore completed with some errors — continuing."
    else
        log "Restore complete."
    fi

    find ~/.hermes -name "*.partial" -delete 2>/dev/null || true
}

# ── BACKUP — Drive available असेल तरच, background मध्ये
rclone_backup() {
    if [ "$DRIVE_OK" != "true" ]; then
        return 0
    fi

    timeout 90s rclone sync ~/.hermes/ "$REMOTE_BACKUP" \
        "${BACKUP_INCLUDES[@]}" \
        $EXTRA_INCLUDES \
        --ignore-checksum \
        --ignore-errors \
        --fast-list \
        2>&1 | grep -E "^(ERROR|CRITICAL)" || true
}

# ── HERMES CONFIG
write_hermes_config() {
    # Drive वरचं .env असेल तर आधी त्यातून values read करा (fallback म्हणून)
    DRIVE_API_KEY="" DRIVE_API_BASE="" DRIVE_MODEL="" DRIVE_PROVIDER=""
    if [ -f ~/.hermes/.env ]; then
        DRIVE_API_KEY=$(grep "^OPENAI_API_KEY=" ~/.hermes/.env | cut -d= -f2- | tr -d '"')
        DRIVE_API_BASE=$(grep "^OPENAI_API_BASE=" ~/.hermes/.env | cut -d= -f2- | tr -d '"')
        DRIVE_MODEL=$(grep "^MODEL_DEFAULT=" ~/.hermes/.env | cut -d= -f2- | tr -d '"')
        DRIVE_PROVIDER=$(grep "^MODEL_PROVIDER=" ~/.hermes/.env | cut -d= -f2- | tr -d '"')
    fi

    # Env variable असेल → तेच वापर (override)
    # Env variable नसेल → Drive वरचं वापर (fallback)
    FINAL_API_KEY="${OPENAI_API_KEY:-$DRIVE_API_KEY}"
    FINAL_API_BASE="${OPENAI_API_BASE:-$DRIVE_API_BASE}"
    FINAL_MODEL="${MODEL_DEFAULT:-$DRIVE_MODEL}"
    FINAL_PROVIDER="${MODEL_PROVIDER:-$DRIVE_PROVIDER}"
    FINAL_UNKNOWN44="${UNKNOWN44_API_KEY:-$FINAL_API_KEY}"
    FINAL_CUSTOM="${CUSTOM_API_KEY:-$FINAL_API_KEY}"

    # Log काय वापरतोय ते
    [ -n "$OPENAI_API_BASE" ] && log "API base: ENV → $FINAL_API_BASE" || log "API base: DRIVE → $FINAL_API_BASE"
    [ -n "$MODEL_DEFAULT" ]   && log "Model: ENV → $FINAL_MODEL"      || log "Model: DRIVE → $FINAL_MODEL"

    # .env write करा merged values सह
    cat > ~/.hermes/.env <<EOF
OPENAI_API_KEY=${FINAL_API_KEY}
UNKNOWN44_API_KEY=${FINAL_UNKNOWN44}
CUSTOM_API_KEY=${FINAL_CUSTOM}
OPENAI_API_BASE=${FINAL_API_BASE}
MODEL_PROVIDER=${FINAL_PROVIDER}
MODEL_DEFAULT=${FINAL_MODEL}
EOF

    # Runtime exports पण update करा
    export OPENAI_API_BASE="$FINAL_API_BASE"
    export MODEL_DEFAULT="$FINAL_MODEL"
    export MODEL_PROVIDER="$FINAL_PROVIDER"
    export UNKNOWN44_API_KEY="$FINAL_UNKNOWN44"
    export CUSTOM_API_KEY="$FINAL_CUSTOM"

    # Backup — env updated असेल तर Drive वर लगेच push करा
    if [ "$DRIVE_OK" = "true" ]; then
        rclone_backup
        log "Config synced to Drive."
    fi

    # Cron schedule override (env मधून)
    if [ -n "$HERMES_CRON_SCHEDULE" ] && [ -d ~/.hermes/cron ]; then
        find ~/.hermes/cron -name "*.json" | while read f; do
            python3 -c "
import json
try:
    with open('$f') as fp:
        d = json.load(fp)
    if 'schedule' in d:
        d['schedule'] = '$HERMES_CRON_SCHEDULE'
        with open('$f', 'w') as fp:
            json.dump(d, fp, indent=2)
except:
    pass
" 2>/dev/null || true
        done
        log "Cron schedules updated."
    fi

    # config.yaml
    mkdir -p ~/.hermes
    CONFIG_YAML=~/.hermes/config.yaml
    if [ -f "$CONFIG_YAML" ]; then
        grep -q "journal_mode" "$CONFIG_YAML" || printf "\ndatabase:\n  journal_mode: delete\n" >> "$CONFIG_YAML"
        grep -q "context_length" "$CONFIG_YAML" || printf "\nmodel:\n  context_length: 128000\n" >> "$CONFIG_YAML"
    else
        cat > "$CONFIG_YAML" <<YAMLEOF
database:
  journal_mode: delete
model:
  context_length: 128000
YAMLEOF
    fi

    # Himalaya symlink
    mkdir -p ~/.config/himalaya
    if [ -f ~/.hermes/skills/email/himalaya/config.toml ]; then
        ln -sf ~/.hermes/skills/email/himalaya/config.toml ~/.config/himalaya/config.toml
    fi
}

setup_hermes_auth() {
    hermes config set terminal.backend local 2>/dev/null || true
    hermes auth add custom \
        --type api-key \
        --api-key "${FINAL_API_KEY:-$OPENAI_API_KEY}" \
        --inference-url "${FINAL_API_BASE:-$OPENAI_API_BASE}" 2>/dev/null || true
    log "Hermes auth configured (base: ${FINAL_API_BASE:-$OPENAI_API_BASE})"
}

# ── TELEGRAM NOTIFICATION
# Hermes official env vars वापरतो:
# TELEGRAM_BOT_TOKEN — BotFather कडून मिळालेला token
# TELEGRAM_ALLOWED_USERS — comma-separated user IDs (तुम्हाला notification जातो)
# हे दोन्ही Koyeb env मध्ये आधीच set आहेत — नवीन काही लागत नाही

send_telegram() {
    local MSG="$1"

    # Token नाही — silently skip
    [ -z "$TELEGRAM_BOT_TOKEN" ] && return 0

    # TELEGRAM_ALLOWED_USERS मधून पहिला user ID घेतो (owner)
    # Format: "123456789" किंवा "123456789,987654321"
    local OWNER_ID
    OWNER_ID=$(echo "$TELEGRAM_ALLOWED_USERS" | cut -d',' -f1 | tr -d ' ')
    [ -z "$OWNER_ID" ] && return 0

    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${OWNER_ID}&text=${MSG}&parse_mode=HTML" \
        > /dev/null 2>&1 || true
}

# ── 4. STARTUP SEQUENCE
rclone_restore        # Drive असेल तर restore, नाहीतर skip
clean_junk            # Cache/temp cleanup (restore नंतर)
write_hermes_config   # Config write
setup_hermes_auth     # Auth setup

# ── 5. BACKGROUND SYNC — दर 60s, Drive असेल तरच, silent
(
    while true; do
        sleep 60
        rclone_backup
    done
) &
SYNC_PID=$!

# ── 6. WATCHDOG — Hermes process बंद झाला तर log, restart loop handle करेल
(
    while true; do
        sleep 120
        if ! pgrep -f "hermes gateway" > /dev/null 2>&1; then
            log "[WATCHDOG] Hermes not running — restart loop will handle."
        fi
    done
) &
WATCHDOG_PID=$!

# ── 7. GRACEFUL SHUTDOWN — SIGTERM/SIGINT वर final backup
cleanup() {
    log "Shutdown signal — final backup..."
    kill $SYNC_PID $WATCHDOG_PID 2>/dev/null || true
    pkill -f "hermes gateway" 2>/dev/null || true
    rclone_backup
    log "Shutdown complete."
    exit 0
}
trap cleanup SIGTERM SIGINT EXIT

log "=========================================="
log "Hermes Auto-Pilot ACTIVE"
log "Drive: ${DRIVE_OK} | Remote: ${REMOTE_BACKUP}"
log "Disk: $(du -sh ~/.hermes 2>/dev/null | cut -f1 || echo '0') used"
log "=========================================="

# ── 8. AUTO-RESTART LOOP — Hermes crash झाल्यावर restart
CRASH_COUNT=0
CONSECUTIVE_FAST_CRASHES=0
FIRST_START=true

while true; do
    LAST_START=$(date +%s)
    CRASH_COUNT=$((CRASH_COUNT + 1))
    log "Starting Hermes (attempt #$CRASH_COUNT)..."

    hermes gateway run &
    HERMES_PID=$!

    # Hermes 5 seconds मध्ये stable झाला तर online notification पाठव
    sleep 5
    if kill -0 $HERMES_PID 2>/dev/null; then
        if [ "$FIRST_START" = "true" ]; then
            send_telegram "✅ <b>Hermes is online</b>
🕐 $(date '+%H:%M IST')
💾 Disk: $(du -sh ~/.hermes 2>/dev/null | cut -f1 || echo '?')
🔗 Drive: ${DRIVE_OK}"
            FIRST_START=false
        else
            send_telegram "🔄 <b>Hermes restarted</b> (attempt #${CRASH_COUNT})
🕐 $(date '+%H:%M IST')"
        fi
    fi

    wait $HERMES_PID
    EXIT_CODE=$?

    UPTIME=$(( $(date +%s) - LAST_START ))
    log "Hermes exited (code: $EXIT_CODE, ran for ${UPTIME}s)"

    if [ $UPTIME -lt 30 ]; then
        CONSECUTIVE_FAST_CRASHES=$((CONSECUTIVE_FAST_CRASHES + 1))

        if [ $CONSECUTIVE_FAST_CRASHES -ge 3 ]; then
            log "3 fast crashes — re-restoring from Drive..."
            clean_junk
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
