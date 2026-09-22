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

# Write the API keys directly to Hermes .env
cat <<EOF > ~/.hermes/.env
OPENAI_API_KEY=Swapnpurti@1181
UNKNOWN44_API_KEY=Swapnpurti@1181
CUSTOM_API_KEY=Swapnpurti@1181
EOF

# Set Custom API Endpoint
export OPENAI_API_BASE="https://unknown44.onrender.com/v1/"
export OPENAI_API_KEY="Swapnpurti@1181"
export UNKNOWN44_API_KEY="Swapnpurti@1181"
export CUSTOM_API_KEY="Swapnpurti@1181"
export MODEL_PROVIDER="custom"
export MODEL_DEFAULT="gemini-pro"
export api_key="Swapnpurti@1181"

# 2. Setup Rclone configuration
mkdir -p ~/.config/rclone
RCLONE_CONF=~/.config/rclone/rclone.conf

if [ -n "$RCLONE_CONFIG_BASE64" ]; then
    echo ">> Configuring rclone from RCLONE_CONFIG_BASE64..."
    if ! echo "$RCLONE_CONFIG_BASE64" | base64 -d > "$RCLONE_CONF" 2> /tmp/rclone_decode_err.log; then
        echo "❌ [RCLONE CONFIG ERROR] base64 decode failed. Raw error below:"
        cat /tmp/rclone_decode_err.log
        rm -f "$RCLONE_CONF"
    fi
elif [ -n "$RCLONE_CONFIG" ]; then
    echo ">> Configuring rclone from RCLONE_CONFIG..."
    echo "$RCLONE_CONFIG" > "$RCLONE_CONF"
else
    echo "⚠️  [RCLONE CONFIG] Neither RCLONE_CONFIG_BASE64 nor RCLONE_CONFIG is set — Drive backup/restore will be SKIPPED for this run."
fi

# Validate the config we just wrote actually parses -- this is what catches a
# truncated/corrupted base64 value (the exact bug we hit last time) instead
# of silently limping along with a half-written file.
RCLONE_OK=0
if [ -f "$RCLONE_CONF" ]; then
    if REMOTES=$(rclone listremotes --config "$RCLONE_CONF" 2>/tmp/rclone_validate_err.log); then
        if [ -n "$REMOTES" ]; then
            echo "✅ [RCLONE CONFIG] Valid. Remotes found: $(echo "$REMOTES" | tr '\n' ' ')"
            RCLONE_OK=1
        else
            echo "❌ [RCLONE CONFIG ERROR] Config file parsed but contains NO remotes. It is likely truncated/incomplete."
        fi
    else
        echo "❌ [RCLONE CONFIG ERROR] rclone could not parse $RCLONE_CONF -- config is invalid/corrupt (likely truncated during copy-paste). Raw error below:"
        cat /tmp/rclone_validate_err.log
    fi
fi

REMOTE_BACKUP="${RCLONE_REMOTE:-gdrive:hermes_backup}"

# 3. Restore from Google Drive (Sync EVERYTHING except cache/tmp)
if [ "$RCLONE_OK" = "1" ]; then
    echo ">> Restoring Hermes state from Google Drive ($REMOTE_BACKUP)..."
    if ! rclone sync "$REMOTE_BACKUP" ~/.hermes/ \
        --exclude "cache/**" --exclude "audio_cache/**" --exclude "image_cache/**" --exclude "runtime/**" \
        --drive-chunk-size 8M -v 2>/tmp/rclone_restore_err.log; then
        RC=$?
        echo "❌ [RCLONE RESTORE ERROR] exit code $RC. Raw error below (state NOT restored from Drive):"
        cat /tmp/rclone_restore_err.log
    else
        echo "✅ [RCLONE RESTORE] Restore from Drive completed successfully."
    fi
else
    echo "⚠️  [RCLONE RESTORE] Skipped -- rclone config missing or invalid (see above)."
fi

# Symlink Himalaya config
mkdir -p ~/.config/himalaya
if [ -f ~/.hermes/skills/email/himalaya/config.toml ]; then
    ln -sf ~/.hermes/skills/email/himalaya/config.toml ~/.config/himalaya/config.toml
fi

# 4. Background Sync Loop (Every 1 Minute)
sync_to_cloud() {
    if [ "$RCLONE_OK" = "1" ]; then
        if ! rclone sync ~/.hermes/ "$REMOTE_BACKUP" \
            --exclude "cache/**" --exclude "audio_cache/**" --exclude "image_cache/**" --exclude "runtime/**" \
            --drive-chunk-size 8M --fast-list -v 2>/tmp/rclone_sync_err.log; then
            RC=$?
            echo "❌ [RCLONE SYNC ERROR] $(date '+%Y-%m-%d %H:%M:%S') exit code $RC -- backup to Drive FAILED this cycle. Raw error below:"
            cat /tmp/rclone_sync_err.log
        else
            echo "✅ [RCLONE SYNC] $(date '+%Y-%m-%d %H:%M:%S') backup to Drive OK."
        fi
    else
        echo "⚠️  [RCLONE SYNC] $(date '+%Y-%m-%d %H:%M:%S') skipped -- no valid rclone config."
    fi
}

(
    while true; do
        sleep 60
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

# 6. Start Hermes Gateway
echo ">> Starting Hermes Gateway..."
if ! hermes config set terminal.backend local; then
    echo "❌ [HERMES CONFIG ERROR] 'hermes config set terminal.backend local' failed (see output above)."
fi
if ! hermes auth add custom --type api-key --api-key "Swapnpurti@1181" --inference-url "https://unknown44.onrender.com/v1/"; then
    echo "❌ [HERMES AUTH ERROR] 'hermes auth add' failed (see output above)."
fi

hermes gateway run
GATEWAY_EXIT=$?
if [ $GATEWAY_EXIT -ne 0 ]; then
    echo "❌ [HERMES GATEWAY ERROR] gateway exited with code $GATEWAY_EXIT"
else
    echo ">> Hermes gateway exited normally (code 0)."
fi
cleanup
