#!/bin/bash

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export TZ="Asia/Kolkata"

echo "=== [Hermes Koyeb Startup] ==="

# 1. KEEP-ALIVE SERVER - INSTANT START
if [ -f /app/keep_alive.py ]; then
    echo ">> Starting keep-alive server..."
    python3 /app/keep_alive.py &
fi

# 2. Dirs setup
mkdir -p ~/.hermes ~/.ssh ~/.local/bin

# 3. Hermes .env - real keys env vars मधून
cat > ~/.hermes/.env << ENVEOF
OPENAI_API_KEY=${OPENAI_API_KEY}
UNKNOWN44_API_KEY=${OPENAI_API_KEY}
CUSTOM_API_KEY=${OPENAI_API_KEY}
ENVEOF

# 4. SSH Private Key setup
if [ -n "$SSH_PRIVATE_KEY" ]; then
    echo ">> Setting up SSH private key..."
    printf '%s\n' "$SSH_PRIVATE_KEY" > ~/.ssh/hermes_runner_key
    chmod 600 ~/.ssh/hermes_runner_key
    echo ">> SSH key ready."
fi

# 5. websocat install - WebSocket SSH tunnel साठी
if ! command -v websocat &>/dev/null; then
    echo ">> Installing websocat..."
    curl -fsSL \
        https://github.com/vi/websocat/releases/download/v1.13.0/websocat.x86_64-unknown-linux-musl \
        -o ~/.local/bin/websocat \
        && chmod +x ~/.local/bin/websocat \
        && echo ">> websocat ready." \
        || echo "!! websocat install failed - SSH backend may not work"
fi

# 6. SSH config - ProxyCommand WebSocket tunnel
# PRIMARY आणि FALLBACK दोन्ही define करतो
cat > ~/.ssh/config << SSHEOF
# Primary Tool Runner
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

# Fallback Tool Runner
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

# 7. Env vars
export OPENAI_API_BASE="${OPENAI_API_BASE:-https://unknown44.onrender.com/v1/}"
export OPENAI_API_KEY="${OPENAI_API_KEY}"
export MODEL_PROVIDER="custom"
export MODEL_DEFAULT="gemini-pro"

# 8. Rclone setup
mkdir -p ~/.config/rclone
if [ -n "$RCLONE_CONFIG_BASE64" ]; then
    echo ">> Configuring rclone..."
    echo "$RCLONE_CONFIG_BASE64" | base64 -d > ~/.config/rclone/rclone.conf
elif [ -n "$RCLONE_CONFIG" ]; then
    echo "$RCLONE_CONFIG" > ~/.config/rclone/rclone.conf
fi

REMOTE_BACKUP="${RCLONE_REMOTE:-gdrive:hermes_backup}"

# 9. Restore from Google Drive
if [ -f ~/.config/rclone/rclone.conf ]; then
    echo ">> Restoring from Google Drive..."
    rclone sync "$REMOTE_BACKUP" ~/.hermes/ \
        --exclude "cache/**" \
        --exclude "audio_cache/**" \
        --exclude "image_cache/**" \
        --exclude "runtime/**" \
        --drive-chunk-size 8M || echo ">> Restore skipped."
fi

# Symlink Himalaya
mkdir -p ~/.config/himalaya
if [ -f ~/.hermes/skills/email/himalaya/config.toml ]; then
    ln -sf ~/.hermes/skills/email/himalaya/config.toml ~/.config/himalaya/config.toml
fi

# 10. Background Sync - every 60s
sync_to_cloud() {
    if [ -f ~/.config/rclone/rclone.conf ]; then
        rclone sync ~/.hermes/ "$REMOTE_BACKUP" \
            --exclude "cache/**" \
            --exclude "audio_cache/**" \
            --exclude "image_cache/**" \
            --exclude "runtime/**" \
            --drive-chunk-size 8M \
            --fast-list || true
    fi
}
(
    while true; do
        sleep 60
        sync_to_cloud
    done
) &
SYNC_PID=$!

# 11. Fallback monitor script
cat > /app/ssh_fallback.sh << 'FALLBACK'
#!/bin/bash

PRIMARY="hermes-runner-primary"
FALLBACK="hermes-runner-fallback"
CURRENT_FILE="/tmp/hermes_current_backend"

check_host() {
    local host="$1"
    timeout 10 ssh \
        -o BatchMode=yes \
        "$host" \
        "echo ok" 2>/dev/null | grep -q "ok"
}

set_backend() {
    local host="$1"
    hermes config set terminal.backend ssh 2>/dev/null || true
    hermes config set terminal.ssh_host "$host" 2>/dev/null || true
    hermes config set terminal.ssh_key ~/.ssh/hermes_runner_key 2>/dev/null || true
    echo "$host" > "$CURRENT_FILE"
}

set_local() {
    hermes config set terminal.backend local 2>/dev/null || true
    echo "local" > "$CURRENT_FILE"
}

get_current() {
    cat "$CURRENT_FILE" 2>/dev/null || echo ""
}

echo "[$(date '+%H:%M:%S')] Checking backends..."

if check_host "$PRIMARY"; then
    if [ "$(get_current)" != "$PRIMARY" ]; then
        echo "[$(date '+%H:%M:%S')] >> PRIMARY active"
        set_backend "$PRIMARY"
    fi
elif check_host "$FALLBACK"; then
    if [ "$(get_current)" != "$FALLBACK" ]; then
        echo "[$(date '+%H:%M:%S')] !! PRIMARY down - switching to FALLBACK"
        set_backend "$FALLBACK"
    fi
else
    if [ "$(get_current)" != "local" ]; then
        echo "[$(date '+%H:%M:%S')] !! Both down - LOCAL backend"
        set_local
    fi
fi
FALLBACK
chmod +x /app/ssh_fallback.sh

# 12. Hermes auth setup
hermes auth add custom \
    --type api-key \
    --api-key "${OPENAI_API_KEY}" \
    --inference-url "${OPENAI_API_BASE:-https://unknown44.onrender.com/v1/}" || true

# 13. Initial backend check + set
echo ">> Setting up SSH backend..."
bash /app/ssh_fallback.sh

# 14. Background monitor - every 2 min
(
    while true; do
        sleep 120
        bash /app/ssh_fallback.sh
    done
) &
MONITOR_PID=$!

# 15. Graceful shutdown
cleanup() {
    echo ">> Shutting down - final sync..."
    kill $SYNC_PID 2>/dev/null || true
    kill $MONITOR_PID 2>/dev/null || true
    sync_to_cloud
    exit 0
}
trap cleanup SIGTERM SIGINT EXIT

# 16. Start Hermes Gateway
echo ">> Starting Hermes Gateway..."
hermes gateway run || echo ">> Hermes gateway exited."

cleanup
