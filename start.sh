#!/bin/bash
export PATH="/app/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
export TZ="Asia/Kolkata"

echo "=== [Hermes Koyeb 2-Phase Startup] ==="

# ══════════════════════════════════════════════════════════════
# PHASE 1 — INSTANT: Keep-alive server start करतो
#           Koyeb health check लगेच pass होतो (502 नाही)
# ══════════════════════════════════════════════════════════════
echo ">> [Phase 1] Starting keep-alive HTTP server instantly..."
if [ -f /app/keep_alive.py ]; then
    python3 /app/keep_alive.py &
    KEEPALIVE_PID=$!
    echo ">> Keep-alive PID: $KEEPALIVE_PID"
fi

# Port ready होण्यासाठी 1 सेकंद — Koyeb ला instant response मिळावा
sleep 1

mkdir -p ~/.hermes ~/.hermes/logs

# Env vars validate
: "${CUSTOM_API_KEY:?CUSTOM_API_KEY env var is not set.}"
export CUSTOM_API_KEY
export CUSTOM_API_BASE="${CUSTOM_API_BASE:-https://unknown44.onrender.com/v1/}"
export MODEL_PROVIDER="custom"
export MODEL_DEFAULT="${MODEL_DEFAULT:-auto}"
export FALLBACK_MODEL="${FALLBACK_MODEL:-$MODEL_DEFAULT}"

# rclone tuning
RCLONE_CHUNK_SIZE="${RCLONE_CHUNK_SIZE:-4M}"
RCLONE_TRANSFERS="${RCLONE_TRANSFERS:-2}"
RCLONE_BUFFER_SIZE="${RCLONE_BUFFER_SIZE:-8M}"
SYNC_INTERVAL_SECONDS="${SYNC_INTERVAL_SECONDS:-600}"
RCLONE_COMMON_FLAGS=(
    --exclude "cache/**" --exclude "audio_cache/**"
    --exclude "image_cache/**" --exclude "runtime/**"
    --exclude "state.db-wal" --exclude "state.db-shm"
    --drive-chunk-size "$RCLONE_CHUNK_SIZE"
    --transfers "$RCLONE_TRANSFERS"
    --checkers 4
    --buffer-size "$RCLONE_BUFFER_SIZE"
)

# ══════════════════════════════════════════════════════════════
# PHASE 2 — BACKGROUND: Drive restore + Gateway start
#           हे background मध्ये होतं, keep-alive server alive राहतो
# ══════════════════════════════════════════════════════════════
(
    echo ">> [Phase 2] Background restore starting..."

    # Rclone setup
    mkdir -p ~/.config/rclone
    RCLONE_CONF=~/.config/rclone/rclone.conf

    if [ -n "$RCLONE_CONFIG_BASE64" ]; then
        echo ">> Configuring rclone from RCLONE_CONFIG_BASE64..."
        if ! echo "$RCLONE_CONFIG_BASE64" | base64 -d > "$RCLONE_CONF" 2>/tmp/rclone_decode_err.log; then
            echo "❌ [RCLONE CONFIG ERROR] base64 decode failed:"
            cat /tmp/rclone_decode_err.log
            rm -f "$RCLONE_CONF"
        fi
    elif [ -n "$RCLONE_CONFIG" ]; then
        echo "$RCLONE_CONFIG" > "$RCLONE_CONF"
    else
        echo "⚠️  [RCLONE] No config set — Drive sync skipped."
    fi

    RCLONE_OK=0
    REMOTE_BACKUP="${RCLONE_REMOTE:-gdrive:hermes_backup}"

    if [ -f "$RCLONE_CONF" ]; then
        if REMOTES=$(rclone listremotes --config "$RCLONE_CONF" 2>/tmp/rclone_validate_err.log) && [ -n "$REMOTES" ]; then
            echo "✅ [RCLONE CONFIG] Valid. Remotes: $(echo "$REMOTES" | tr '\n' ' ')"
            RCLONE_OK=1
        else
            echo "❌ [RCLONE CONFIG ERROR] Invalid or empty config."
            cat /tmp/rclone_validate_err.log 2>/dev/null
        fi
    fi

    # Drive वरून restore
    if [ "$RCLONE_OK" = "1" ]; then
        echo ">> Restoring from Drive ($REMOTE_BACKUP) — gateway नंतर येईल..."
        RESTORE_START=$(date +%s)
        if rclone sync "$REMOTE_BACKUP" ~/.hermes/ \
            "${RCLONE_COMMON_FLAGS[@]}" -v 2>/tmp/rclone_restore_err.log; then
            RESTORE_END=$(date +%s)
            RESTORE_SEC=$(( RESTORE_END - RESTORE_START ))
            echo "✅ [RCLONE RESTORE] Done in ${RESTORE_SEC}s."
        else
            echo "❌ [RCLONE RESTORE ERROR] Partial restore (gateway will start with whatever restored):"
            cat /tmp/rclone_restore_err.log
        fi
    else
        echo "⚠️  [RCLONE RESTORE] Skipped."
    fi

    # Himalaya symlink
    mkdir -p ~/.config/himalaya
    if [ -f ~/.hermes/skills/email/himalaya/config.toml ]; then
        ln -sf ~/.hermes/skills/email/himalaya/config.toml ~/.config/himalaya/config.toml
    fi

    # Package replay
    INSTALLED_DIR=~/.hermes/installed
    mkdir -p "$INSTALLED_DIR"

    if [ -s "$INSTALLED_DIR/apt.list" ]; then
        echo ">> Reinstalling apt packages..."
        apt-get update -qq && xargs -a "$INSTALLED_DIR/apt.list" apt-get install -y \
            && echo "✅ [APT REPLAY]" || echo "❌ [APT REPLAY ERROR]"
    fi
    if [ -s "$INSTALLED_DIR/pip.list" ]; then
        echo ">> Reinstalling pip packages..."
        xargs -a "$INSTALLED_DIR/pip.list" pip install \
            && echo "✅ [PIP REPLAY]" || echo "❌ [PIP REPLAY ERROR]"
    fi
    if [ -s "$INSTALLED_DIR/npm.list" ]; then
        echo ">> Reinstalling npm packages..."
        xargs -a "$INSTALLED_DIR/npm.list" npm install -g \
            && echo "✅ [NPM REPLAY]" || echo "❌ [NPM REPLAY ERROR]"
    fi

    # Autostart scripts
    AUTOSTART_DIR=~/.hermes/autostart
    mkdir -p "$AUTOSTART_DIR"
    AUTOSTART_LOG_DIR=~/.hermes/logs/autostart
    mkdir -p "$AUTOSTART_LOG_DIR"
    shopt -s nullglob
    for script in "$AUTOSTART_DIR"/*.sh; do
        name="$(basename "$script" .sh)"
        chmod +x "$script"
        echo ">> Autostarting $name..."
        nohup "$script" >> "$AUTOSTART_LOG_DIR/$name.log" 2>&1 &
    done
    shopt -u nullglob

    # Hermes config
    echo ">> Configuring Hermes auth..."
    hermes config set terminal.backend local || echo "❌ [HERMES CONFIG ERROR]"
    hermes auth add custom --type api-key \
        --api-key "$CUSTOM_API_KEY" \
        --inference-url "$CUSTOM_API_BASE" || echo "❌ [HERMES AUTH ERROR]"
    hermes config set fallback_providers \
        "[{\"provider\":\"custom\",\"base_url\":\"$CUSTOM_API_BASE\",\"api_key\":\"$CUSTOM_API_KEY\",\"model\":\"$FALLBACK_MODEL\"}]" \
        && echo "✅ [HERMES FALLBACK] Set to '$FALLBACK_MODEL'." \
        || echo "❌ [HERMES FALLBACK ERROR]"

    # keep_alive.py ला signal — "restore झालं, gateway येतोय"
    curl -sf http://localhost:${PORT:-10000}/startup-ready > /dev/null 2>&1 || true
    echo ">> [Phase 2] Restore + config done. Starting watchdog + gateway..."

    # Background sync loop
    sync_to_cloud() {
        if [ "$RCLONE_OK" = "1" ]; then
            if rclone sync ~/.hermes/ "$REMOTE_BACKUP" \
                "${RCLONE_COMMON_FLAGS[@]}" --fast-list -v 2>/tmp/rclone_sync_err.log; then
                echo "✅ [RCLONE SYNC] $(date '+%Y-%m-%d %H:%M:%S') OK."
            else
                echo "❌ [RCLONE SYNC ERROR] $(date '+%Y-%m-%d %H:%M:%S'):"
                cat /tmp/rclone_sync_err.log
            fi
        fi
    }

    (
        while true; do
            sleep "$SYNC_INTERVAL_SECONDS"
            sync_to_cloud
        done
    ) &
    SYNC_PID=$!

    cleanup() {
        echo ">> Shutdown — final sync..."
        kill $SYNC_PID 2>/dev/null || true
        sync_to_cloud
        exit 0
    }
    trap cleanup SIGTERM SIGINT

    # Watchdog start करतो (gateway त्याच्या आत चालतो)
    chmod +x /app/watchdog.sh
    /app/watchdog.sh

    # Watchdog exit झाला (max restarts) — final cleanup
    cleanup

) &
BACKGROUND_PID=$!

echo ">> [Phase 1] Keep-alive running (PID $KEEPALIVE_PID), background restore PID: $BACKGROUND_PID"
echo ">> Container is healthy. Restore happening in background..."

# Keep-alive server ला foreground मध्ये wait करतो
# (हा process चालू असेपर्यंत Koyeb container alive राहतो)
wait $KEEPALIVE_PID
