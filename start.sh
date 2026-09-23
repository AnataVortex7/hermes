#!/bin/bash
export PATH="/app/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
export TZ="Asia/Kolkata"

echo "=== [Hermes Koyeb Instant Startup & Background Sync] ==="

# 1. START KEEP-ALIVE SERVER INSTANTLY
if [ -f /app/keep_alive.py ]; then
    echo ">> Starting keep-alive HTTP server immediately..."
    python3 /app/keep_alive.py &
fi

# Ensure Hermes config dir exists
mkdir -p ~/.hermes

# Custom API credentials -- NO hardcoded secrets here anymore. CUSTOM_API_KEY
# must be set as an env var on the platform (Koyeb dashboard -> Environment
# Variables). The old manual ~/.hermes/.env write is gone too -- it wasn't
# read by anything; `hermes auth add custom` further below is the actual
# mechanism that registers the credential with Hermes.
: "${CUSTOM_API_KEY:?CUSTOM_API_KEY env var is not set. Add it in the platform environment variables (no more hardcoded key in this script).}"
export CUSTOM_API_KEY
export CUSTOM_API_BASE="${CUSTOM_API_BASE:-https://unknown44.onrender.com/v1/}"
export MODEL_PROVIDER="custom"

# Model: "auto" on your custom API, unless you set MODEL_DEFAULT yourself on
# the platform -- in which case that value wins. Fallback uses the same
# model/API by default too; set FALLBACK_MODEL separately only if you want a
# different model for the fallback than for the primary.
export MODEL_DEFAULT="${MODEL_DEFAULT:-auto}"
export FALLBACK_MODEL="${FALLBACK_MODEL:-$MODEL_DEFAULT}"

# 512MB-RAM tuning knobs -- override any of these as env vars if you deploy
# on a bigger box later; left untouched they keep rclone's memory footprint
# small so sync doesn't compete with the gateway process for RAM.
RCLONE_CHUNK_SIZE="${RCLONE_CHUNK_SIZE:-4M}"
RCLONE_TRANSFERS="${RCLONE_TRANSFERS:-2}"
RCLONE_BUFFER_SIZE="${RCLONE_BUFFER_SIZE:-8M}"
SYNC_INTERVAL_SECONDS="${SYNC_INTERVAL_SECONDS:-60}"
RCLONE_COMMON_FLAGS=(--exclude "cache/**" --exclude "audio_cache/**" --exclude "image_cache/**" --exclude "runtime/**" \
    --drive-chunk-size "$RCLONE_CHUNK_SIZE" --transfers "$RCLONE_TRANSFERS" --checkers 4 --buffer-size "$RCLONE_BUFFER_SIZE")

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
        "${RCLONE_COMMON_FLAGS[@]}" -v 2>/tmp/rclone_restore_err.log; then
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

# 3b. Replay any packages/tools that were installed during a previous run.
# The apt/pip/npm wrappers in /app/bin log every successful `install` into
# ~/.hermes/installed/*.list, which is part of the Drive sync above, so this
# list survives a redeploy even though the container filesystem itself does
# not.
INSTALLED_DIR=~/.hermes/installed
mkdir -p "$INSTALLED_DIR"

if [ -s "$INSTALLED_DIR/apt.list" ]; then
    echo ">> Reinstalling previously installed apt packages: $(tr '\n' ' ' < "$INSTALLED_DIR/apt.list")"
    if ! apt-get update -qq; then
        echo "❌ [APT REPLAY ERROR] 'apt-get update' failed -- skipping apt replay this run."
    else
        if ! xargs -a "$INSTALLED_DIR/apt.list" apt-get install -y; then
            echo "❌ [APT REPLAY ERROR] one or more apt packages failed to reinstall (see output above)."
        else
            echo "✅ [APT REPLAY] apt packages restored."
        fi
    fi
fi

if [ -s "$INSTALLED_DIR/pip.list" ]; then
    echo ">> Reinstalling previously installed pip packages: $(tr '\n' ' ' < "$INSTALLED_DIR/pip.list")"
    if ! xargs -a "$INSTALLED_DIR/pip.list" pip install; then
        echo "❌ [PIP REPLAY ERROR] one or more pip packages failed to reinstall (see output above)."
    else
        echo "✅ [PIP REPLAY] pip packages restored."
    fi
fi

if [ -s "$INSTALLED_DIR/npm.list" ]; then
    echo ">> Reinstalling previously installed npm global packages: $(tr '\n' ' ' < "$INSTALLED_DIR/npm.list")"
    if ! xargs -a "$INSTALLED_DIR/npm.list" npm install -g; then
        echo "❌ [NPM REPLAY ERROR] one or more npm packages failed to reinstall (see output above)."
    else
        echo "✅ [NPM REPLAY] npm global packages restored."
    fi
fi

# 3c. Restart anything the user/agent asked to keep running long-term.
# Drop an executable .sh into ~/.hermes/autostart/ (it syncs to Drive like
# everything else) and it will be relaunched in the background on every boot.
AUTOSTART_DIR=~/.hermes/autostart
mkdir -p "$AUTOSTART_DIR"
AUTOSTART_LOG_DIR=~/.hermes/logs/autostart
mkdir -p "$AUTOSTART_LOG_DIR"
shopt -s nullglob
for script in "$AUTOSTART_DIR"/*.sh; do
    name="$(basename "$script" .sh)"
    chmod +x "$script"
    echo ">> Autostarting $name (log: $AUTOSTART_LOG_DIR/$name.log)..."
    nohup "$script" >> "$AUTOSTART_LOG_DIR/$name.log" 2>&1 &
done
shopt -u nullglob

# 4. Background Sync Loop (Every 1 Minute)
sync_to_cloud() {
    if [ "$RCLONE_OK" = "1" ]; then
        if ! rclone sync ~/.hermes/ "$REMOTE_BACKUP" \
            "${RCLONE_COMMON_FLAGS[@]}" --fast-list -v 2>/tmp/rclone_sync_err.log; then
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
        sleep "$SYNC_INTERVAL_SECONDS"
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
if ! hermes auth add custom --type api-key --api-key "$CUSTOM_API_KEY" --inference-url "$CUSTOM_API_BASE"; then
    echo "❌ [HERMES AUTH ERROR] 'hermes auth add' failed (see output above)."
fi

# Permanent fallback: same custom API/endpoint, used automatically whenever
# the primary model call fails (rate limit, 5xx, auth hiccup, etc). This is
# written to ~/.hermes/config.yaml, which is part of the Drive backup, so
# once set it survives every future restart without needing this block again
# -- it's re-run each boot only to also cover a brand-new volume with no
# Drive backup yet.
if ! hermes config set fallback_providers "[{\"provider\":\"custom\",\"base_url\":\"$CUSTOM_API_BASE\",\"api_key\":\"$CUSTOM_API_KEY\",\"model\":\"$FALLBACK_MODEL\"}]"; then
    echo "❌ [HERMES FALLBACK ERROR] 'hermes config set fallback_providers' failed (see output above)."
else
    echo "✅ [HERMES FALLBACK] fallback model set to '$FALLBACK_MODEL' on the custom API."
fi

hermes gateway run
GATEWAY_EXIT=$?
if [ $GATEWAY_EXIT -ne 0 ]; then
    echo "❌ [HERMES GATEWAY ERROR] gateway exited with code $GATEWAY_EXIT"
else
    echo ">> Hermes gateway exited normally (code 0)."
fi
cleanup
