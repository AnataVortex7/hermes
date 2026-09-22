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
mkdir -p ~/.hermes ~/.ssh ~/.local/bin

# Write the API keys directly to Hermes .env
cat <<ENVEOF > ~/.hermes/.env
OPENAI_API_KEY=${OPENAI_API_KEY}
UNKNOWN44_API_KEY=${OPENAI_API_KEY}
CUSTOM_API_KEY=${OPENAI_API_KEY}
ENVEOF

# Set Custom API Endpoint
export OPENAI_API_BASE="${OPENAI_API_BASE:-https://unknown44.onrender.com/v1/}"
export OPENAI_API_KEY="${OPENAI_API_KEY}"
export UNKNOWN44_API_KEY="${OPENAI_API_KEY}"
export CUSTOM_API_KEY="${OPENAI_API_KEY}"
export MODEL_PROVIDER="custom"
export MODEL_DEFAULT="gemini-pro"
export api_key="${OPENAI_API_KEY}"

# ── NEW: SSH Private Key setup ──────────────────────────────
if [ -n "$SSH_PRIVATE_KEY" ]; then
    echo ">> Setting up SSH private key..."
    printf '%s\n' "$SSH_PRIVATE_KEY" > ~/.ssh/hermes_runner_key
    chmod 600 ~/.ssh/hermes_runner_key
    echo ">> SSH key ready."
fi

# ── NEW: websocat install (WebSocket SSH tunnel साठी) ───────
if ! command -v websocat &>/dev/null; then
    echo ">> Installing websocat..."
    curl -fsSL \
        https://github.com/vi/websocat/releases/download/v1.13.0/websocat.x86_64-unknown-linux-musl \
        -o ~/.local/bin/websocat \
        && chmod +x ~/.local/bin/websocat \
        && echo ">> websocat ready." \
        || echo "!! websocat install failed - SSH backend may not work"
fi

# ── NEW: SSH config - WebSocket ProxyCommand ────────────────
if [ -n "$TERMINAL_SSH_HOST_PRIMARY" ]; then
    cat > ~/.ssh/config << SSHEOF
Host hermes-runner-primary
    HostName localhost
    User root
    Port 22
    IdentityFile ~/.ssh/hermes_runner_key
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    ServerAliveInterval 60
    ServerAliveCountMax 5
    ConnectTimeout 10
    ProxyCommand websocat --binary wss://${TERMINAL_SSH_HOST_PRIMARY}/ssh

Host hermes-runner-fallback
    HostName localhost
    User root
    Port 22
    IdentityFile ~/.ssh/hermes_runner_key
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    ServerAliveInterval 60
    ServerAliveCountMax 5
    ConnectTimeout 10
    ProxyCommand websocat --binary wss://${TERMINAL_SSH_HOST_FALLBACK}/ssh

Host *
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    ConnectTimeout 10
SSHEOF
    chmod 600 ~/.ssh/config
    echo ">> SSH config ready."
fi
# ────────────────────────────────────────────────────────────

# 2. Setup Rclone configuration
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
    echo ">> Configuring rclone from RCLONE_CONFIG..."
    echo "$RCLONE_CONFIG" > "$RCLONE_CONF"
else
    echo "⚠️  [RCLONE CONFIG] Not set — Drive backup/restore will be SKIPPED."
fi

RCLONE_OK=0
if [ -f "$RCLONE_CONF" ]; then
    if REMOTES=$(rclone listremotes --config "$RCLONE_CONF" 2>/tmp/rclone_validate_err.log); then
        if [ -n "$REMOTES" ]; then
            echo "✅ [RCLONE CONFIG] Valid. Remotes: $(echo "$REMOTES" | tr '\n' ' ')"
            RCLONE_OK=1
        else
            echo "❌ [RCLONE CONFIG ERROR] No remotes found - config truncated/incomplete."
        fi
    else
        echo "❌ [RCLONE CONFIG ERROR] Cannot parse config:"
        cat /tmp/rclone_validate_err.log
    fi
fi

REMOTE_BACKUP="${RCLONE_REMOTE:-gdrive:hermes_backup}"

# 3. Restore from Google Drive
if [ "$RCLONE_OK" = "1" ]; then
    echo ">> Restoring Hermes state from Google Drive ($REMOTE_BACKUP)..."
    if ! rclone sync "$REMOTE_BACKUP" ~/.hermes/ \
        --exclude "cache/**" --exclude "audio_cache/**" \
        --exclude "image_cache/**" --exclude "runtime/**" \
        --drive-chunk-size 8M -v 2>/tmp/rclone_restore_err.log; then
        echo "❌ [RCLONE RESTORE ERROR] exit code $?:"
        cat /tmp/rclone_restore_err.log
    else
        echo "✅ [RCLONE RESTORE] Completed successfully."
    fi
else
    echo "⚠️  [RCLONE RESTORE] Skipped — invalid config."
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
            --exclude "cache/**" --exclude "audio_cache/**" \
            --exclude "image_cache/**" --exclude "runtime/**" \
            --drive-chunk-size 8M --fast-list -v 2>/tmp/rclone_sync_err.log; then
            echo "❌ [RCLONE SYNC ERROR] $(date '+%Y-%m-%d %H:%M:%S') exit code $? — backup FAILED:"
            cat /tmp/rclone_sync_err.log
        else
            echo "✅ [RCLONE SYNC] $(date '+%Y-%m-%d %H:%M:%S') backup OK."
        fi
    else
        echo "⚠️  [RCLONE SYNC] $(date '+%Y-%m-%d %H:%M:%S') skipped — no valid config."
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
    kill $MONITOR_PID 2>/dev/null || true
    sync_to_cloud
    exit 0
}
trap cleanup SIGTERM SIGINT EXIT

# 6. Start Hermes Gateway
echo ">> Starting Hermes Gateway..."

# ── NEW: SSH Backend check + set ────────────────────────────
check_ssh() {
    timeout 10 ssh -o BatchMode=yes "$1" "echo ok" 2>/dev/null | grep -q "ok"
}

if [ -n "$TERMINAL_SSH_HOST_PRIMARY" ]; then
    if check_ssh "hermes-runner-primary"; then
        echo "✅ [SSH] PRIMARY active: ${TERMINAL_SSH_HOST_PRIMARY}"
        hermes config set terminal.backend ssh || true
        hermes config set terminal.ssh_host hermes-runner-primary || true
        hermes config set terminal.ssh_key ~/.ssh/hermes_runner_key || true
    elif [ -n "$TERMINAL_SSH_HOST_FALLBACK" ] && check_ssh "hermes-runner-fallback"; then
        echo "✅ [SSH] FALLBACK active: ${TERMINAL_SSH_HOST_FALLBACK}"
        hermes config set terminal.backend ssh || true
        hermes config set terminal.ssh_host hermes-runner-fallback || true
        hermes config set terminal.ssh_key ~/.ssh/hermes_runner_key || true
    else
        echo "⚠️  [SSH] Both down — using local backend"
        hermes config set terminal.backend local || true
    fi

    # Background monitor - दर 2 मिनिटांनी recheck
    (
        while true; do
            sleep 120
            if check_ssh "hermes-runner-primary"; then
                CURR=$(hermes config get terminal.ssh_host 2>/dev/null || echo "")
                if [ "$CURR" != "hermes-runner-primary" ]; then
                    echo ">> [SSH MONITOR] PRIMARY back — switching"
                    hermes config set terminal.ssh_host hermes-runner-primary || true
                    hermes config set terminal.backend ssh || true
                fi
            elif check_ssh "hermes-runner-fallback"; then
                CURR=$(hermes config get terminal.ssh_host 2>/dev/null || echo "")
                if [ "$CURR" != "hermes-runner-fallback" ]; then
                    echo ">> [SSH MONITOR] FALLBACK active"
                    hermes config set terminal.ssh_host hermes-runner-fallback || true
                    hermes config set terminal.backend ssh || true
                fi
            else
                echo "⚠️  [SSH MONITOR] Both down — local backend"
                hermes config set terminal.backend local || true
            fi
        done
    ) &
    MONITOR_PID=$!
else
    # SSH env var नाही → local (जुन्यासारखाच)
    if ! hermes config set terminal.backend local; then
        echo "❌ [HERMES CONFIG ERROR] terminal.backend local failed."
    fi
fi
# ────────────────────────────────────────────────────────────

if ! hermes auth add custom \
    --type api-key \
    --api-key "${OPENAI_API_KEY}" \
    --inference-url "${OPENAI_API_BASE:-https://unknown44.onrender.com/v1/}"; then
    echo "❌ [HERMES AUTH ERROR] auth add failed."
fi

hermes gateway run
GATEWAY_EXIT=$?
if [ $GATEWAY_EXIT -ne 0 ]; then
    echo "❌ [HERMES GATEWAY ERROR] exited with code $GATEWAY_EXIT"
else
    echo ">> Hermes gateway exited normally (code 0)."
fi
cleanup
