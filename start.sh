#!/bin/bash

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export TZ="Asia/Kolkata"

# ============================================================
# HERMES AUTO-PILOT — एकदा deploy, कधीच manually उघडायचं नाही
# ============================================================

log() {
    echo ">> [$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# ── 1. KEEP-ALIVE SERVER (सगळ्यात आधी start करा — Koyeb health check साठी)
if [ -f /app/keep_alive.py ]; then
    log "Keep-alive server starting..."
    python3 /app/keep_alive.py &
    KEEPALIVE_PID=$!
    log "Keep-alive PID: $KEEPALIVE_PID"
fi

# ── 2. ENVIRONMENT VARIABLES CHECK
if [ -z "$OPENAI_API_KEY" ]; then
    log "ERROR: OPENAI_API_KEY environment variable not set! Koyeb dashboard मध्ये add करा."
    # Keep-alive चालू ठेवा जेणेकरून Koyeb unhealthy mark करणार नाही
    wait $KEEPALIVE_PID
    exit 1
fi

export OPENAI_API_BASE="${OPENAI_API_BASE:-https://unknown44.onrender.com/v1/}"
export UNKNOWN44_API_KEY="${UNKNOWN44_API_KEY:-${OPENAI_API_KEY}}"
export CUSTOM_API_KEY="${CUSTOM_API_KEY:-${OPENAI_API_KEY}}"
export MODEL_PROVIDER="${MODEL_PROVIDER:-custom}"
export MODEL_DEFAULT="${MODEL_DEFAULT:-gemini-pro}"
export api_key="${OPENAI_API_KEY}"

log "Environment loaded. API Base: $OPENAI_API_BASE"

# ── 3. RCLONE SETUP
mkdir -p ~/.config/rclone

if [ -n "$RCLONE_CONFIG_BASE64" ]; then
    log "Configuring rclone from RCLONE_CONFIG_BASE64..."
    echo "$RCLONE_CONFIG_BASE64" | base64 -d > ~/.config/rclone/rclone.conf
elif [ -n "$RCLONE_CONFIG" ]; then
    log "Configuring rclone from RCLONE_CONFIG..."
    echo "$RCLONE_CONFIG" > ~/.config/rclone/rclone.conf
fi

REMOTE_BACKUP="${RCLONE_REMOTE:-gdrive:hermes_backup}"
RCLONE_AVAILABLE=false
if [ -f ~/.config/rclone/rclone.conf ]; then
    RCLONE_AVAILABLE=true
    log "Rclone configured. Remote: $REMOTE_BACKUP"
else
    log "WARNING: No rclone config — Google Drive sync disabled."
fi

# ── RCLONE SYNC HELPER (reusable)
rclone_restore() {
    if [ "$RCLONE_AVAILABLE" = true ]; then
        log "Restoring from Google Drive..."
        rclone sync "$REMOTE_BACKUP" ~/.hermes/ \
            --exclude "cache/**" \
            --exclude "audio_cache/**" \
            --exclude "image_cache/**" \
            --exclude "runtime/**" \
            --exclude "*.partial" \
            --ignore-checksum \
            --copy-links \
            --ignore-errors \
            --drive-chunk-size 8M \
            --transfers 4 \
            2>&1 | grep -v "NOTICE:" || true
        log "Restore done."
    fi
}

rclone_backup() {
    if [ "$RCLONE_AVAILABLE" = true ]; then
        rclone sync ~/.hermes/ "$REMOTE_BACKUP" \
            --exclude "cache/**" \
            --exclude "audio_cache/**" \
            --exclude "image_cache/**" \
            --exclude "runtime/**" \
            --exclude "*.partial" \
            --ignore-checksum \
            --copy-links \
            --ignore-errors \
            --drive-chunk-size 8M \
            --fast-list \
            2>&1 | grep -v "NOTICE:" || true
    fi
}

# ── 4. HERMES STATE RESTORE करा
mkdir -p ~/.hermes

# Corrupted .partial files नेहमी delete करा (crash मुळे येतात)
clean_partial_files() {
    log "Cleaning corrupted .partial files..."
    find ~/.hermes -name "*.partial" -delete 2>/dev/null || true
    log "Cleanup done."
}

rclone_restore
clean_partial_files

# ── 5. HERMES .env WRITE
cat > ~/.hermes/.env <<EOF
OPENAI_API_KEY=${OPENAI_API_KEY}
UNKNOWN44_API_KEY=${UNKNOWN44_API_KEY}
CUSTOM_API_KEY=${CUSTOM_API_KEY}
EOF
log "Hermes .env written."

# ── 6. HIMALAYA SYMLINK
mkdir -p ~/.config/himalaya
if [ -f ~/.hermes/skills/email/himalaya/config.toml ]; then
    ln -sf ~/.hermes/skills/email/himalaya/config.toml ~/.config/himalaya/config.toml
    log "Himalaya config linked."
fi

# ── 7. HERMES ONE-TIME CONFIG (idempotent — परत run केला तरी चालेल)
setup_hermes() {
    log "Configuring Hermes auth..."
    hermes config set terminal.backend local 2>/dev/null || true
    hermes auth add custom \
        --type api-key \
        --api-key "${OPENAI_API_KEY}" \
        --inference-url "${OPENAI_API_BASE}" 2>/dev/null || true
    log "Hermes config done."
}

setup_hermes

# ── 8. BACKGROUND SYNC (दर 1 मिनिट)
(
    while true; do
        sleep 60
        rclone_backup
    done
) &
SYNC_PID=$!
log "Background sync started (PID: $SYNC_PID)"

# ── 9. WATCHDOG (दर 2 मिनिट — silent crash detect करतो)
(
    while true; do
        sleep 120
        if ! pgrep -x "hermes" > /dev/null 2>&1 && ! pgrep -f "hermes gateway" > /dev/null 2>&1; then
            log "[WATCHDOG] Hermes process missing! Main restart loop ला signal..."
            # Main loop आपोआप restart करेल — फक्त log करतो
        else
            log "[WATCHDOG] Hermes OK."
        fi
    done
) &
WATCHDOG_PID=$!
log "Watchdog started (PID: $WATCHDOG_PID)"

# ── 10. GRACEFUL SHUTDOWN
cleanup() {
    log "Shutdown signal received. Final sync करतोय..."
    kill $SYNC_PID 2>/dev/null || true
    kill $WATCHDOG_PID 2>/dev/null || true
    pkill -f "hermes gateway" 2>/dev/null || true
    rclone_backup
    log "Shutdown complete."
    exit 0
}
trap cleanup SIGTERM SIGINT EXIT

# ── 11. HERMES GATEWAY — AUTO-RESTART LOOP
# Crash झाला, corrupted state आला, काहीही झालं — आपोआप restart
CRASH_COUNT=0
CONSECUTIVE_FAST_CRASHES=0
LAST_START_TIME=0

log "=========================================="
log "Hermes Auto-Pilot ACTIVE"
log "Bot आता automatic चालेल — manually काही करायची गरज नाही"
log "=========================================="

while true; do
    LAST_START_TIME=$(date +%s)
    CRASH_COUNT=$((CRASH_COUNT + 1))

    log "Hermes Gateway starting... (attempt #$CRASH_COUNT)"

    hermes gateway run
    EXIT_CODE=$?

    NOW=$(date +%s)
    UPTIME=$((NOW - LAST_START_TIME))

    log "Hermes exited (code: $EXIT_CODE, uptime: ${UPTIME}s)"

    # जर 30 seconds पेक्षा कमी वेळात crash झाला → corrupted state असेल
    if [ $UPTIME -lt 30 ]; then
        CONSECUTIVE_FAST_CRASHES=$((CONSECUTIVE_FAST_CRASHES + 1))
        log "Fast crash detected (#$CONSECUTIVE_FAST_CRASHES)"

        if [ $CONSECUTIVE_FAST_CRASHES -ge 3 ]; then
            log "3 fast crashes! State corrupted असेल — Google Drive वरून fresh restore..."
            pkill -f "hermes" 2>/dev/null || true
            sleep 2

            # Corrupted state wipe करा
            clean_partial_files

            # Fresh restore from Google Drive
            rclone_restore
            clean_partial_files

            # Hermes config पुन्हा setup
            setup_hermes

            CONSECUTIVE_FAST_CRASHES=0
            log "Fresh restore done. Restarting Hermes..."
            sleep 5
        else
            log "Waiting 10s before retry..."
            sleep 10
        fi
    else
        # Normal crash — लगेच restart
        CONSECUTIVE_FAST_CRASHES=0
        log "Restarting in 5s..."
        sleep 5
    fi
done

# (इथे कधीच येणार नाही, पण safety साठी)
cleanup
