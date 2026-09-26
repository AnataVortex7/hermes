#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export TZ="Asia/Kolkata"

echo "=== [Hermes Koyeb Instant Startup & Background Sync] ==="

# 1. START KEEP-ALIVE SERVER INSTANTLY
if [ -f /app/keep_alive.py ]; then
  echo ">> Starting keep-alive HTTP server immediately..."
  python3 /app/keep_alive.py &
fi

mkdir -p ~/.hermes

# 2. Setup Rclone configuration
mkdir -p ~/.config/rclone
if [ -n "$RCLONE_CONFIG_BASE64" ]; then
  echo ">> Configuring rclone from RCLONE_CONFIG_BASE64..."
  echo "$RCLONE_CONFIG_BASE64" | base64 -d > ~/.config/rclone/rclone.conf
elif [ -n "$RCLONE_CONFIG" ]; then
  echo ">> Configuring rclone from RCLONE_CONFIG..."
  echo "$RCLONE_CONFIG" > ~/.config/rclone/rclone.conf
fi

REMOTE_BACKUP="${RCLONE_REMOTE:-gdrive:hermes_backup}"

# rclone_sync — bash arrays नाहीत (sh compatible), excludes function मध्ये hardcode
rclone_sync() {
  rclone "$@" \
    --exclude "cache/**" \
    --exclude "audio_cache/**" \
    --exclude "image_cache/**" \
    --exclude "runtime/**" \
    --exclude "hermes-agent/**" \
    --exclude "tools/**" \
    --exclude "installs/**" \
    --exclude "node_modules/**" \
    --exclude ".env"
}

# 3. Restore from Google Drive
if [ -f ~/.config/rclone/rclone.conf ]; then
  echo ">> Restoring Hermes state from Google Drive ($REMOTE_BACKUP)..."
  rclone_sync sync "$REMOTE_BACKUP" ~/.hermes/ --drive-chunk-size 8M || echo ">> Restore skipped."
fi

# Coalesce env vars
RESOLVED_BASE_URL="${OPENAI_BASE_URL:-${OPENAI_API_BASE:-${CUSTOM_BASE_URL:-${CUSTOM_API_BASE:-}}}}"
RESOLVED_MODEL="${HERMES_MODEL:-${LLM_MODEL:-${MODEL_DEFAULT:-}}}"

# 4. Write .env from env vars
{
  echo "OPENAI_API_KEY=${CUSTOM_API_KEY}"
  echo "UNKNOWN44_API_KEY=${CUSTOM_API_KEY}"
  echo "CUSTOM_API_KEY=${CUSTOM_API_KEY}"

  [ -n "$RESOLVED_BASE_URL" ]          && echo "OPENAI_BASE_URL=${RESOLVED_BASE_URL}"
  [ -n "$OPENROUTER_API_KEY" ]         && echo "OPENROUTER_API_KEY=${OPENROUTER_API_KEY}"
  if [ -n "$RESOLVED_MODEL" ]; then
    echo "HERMES_MODEL=${RESOLVED_MODEL}"
    echo "LLM_MODEL=${RESOLVED_MODEL}"
  fi

  [ -n "$TELEGRAM_BOT_TOKEN" ]         && echo "TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}"
  [ -n "$TELEGRAM_ALLOWED_USERS" ]     && echo "TELEGRAM_ALLOWED_USERS=${TELEGRAM_ALLOWED_USERS}"
  [ -n "$TELEGRAM_HOME_CHANNEL" ]      && echo "TELEGRAM_HOME_CHANNEL=${TELEGRAM_HOME_CHANNEL}"
  [ -n "$TELEGRAM_HOME_CHANNEL_NAME" ] && echo "TELEGRAM_HOME_CHANNEL_NAME=${TELEGRAM_HOME_CHANNEL_NAME}"

  [ -n "$DISCORD_BOT_TOKEN" ]          && echo "DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}"
  [ -n "$DISCORD_ALLOWED_USERS" ]      && echo "DISCORD_ALLOWED_USERS=${DISCORD_ALLOWED_USERS}"

  [ -n "$SLACK_BOT_TOKEN" ]            && echo "SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN}"
  [ -n "$SLACK_APP_TOKEN" ]            && echo "SLACK_APP_TOKEN=${SLACK_APP_TOKEN}"
  [ -n "$SLACK_ALLOWED_USERS" ]        && echo "SLACK_ALLOWED_USERS=${SLACK_ALLOWED_USERS}"
} > ~/.hermes/.env

# 5. Patch config.yaml
#
# Problems:
# A) System python3 ला PyYAML नाही → Hermes चा स्वतःचा Python वापरतो
#    (Hermes Python: /root/.hermes/tools/python-*/bin/python3 — yaml built-in,
#    आता Dockerfile मध्ये आपण pip install pyyaml केलं आहे)
# B) Drive मध्ये corrupt config.yaml आहे (जुन्या patches नंतर sync झाला):
#    "model: auto\n  provider: 'auto'\n  per_platform: ..." — scalar + orphaned sub-keys
#    Fix: आधी regex ने corruption clean करतो, मग yaml ने parse + patch

# Hermes Python शोधतो (yaml available असतो)
HERMES_PY=$(ls /root/.hermes/tools/python-*/bin/python3 2>/dev/null | sort -V | tail -1)
PATCH_PY="${HERMES_PY:-python3}"
echo ">> Config patch using: $PATCH_PY"

"$PATCH_PY" - << 'PYEOF'
import os, re, sys

cfg_path = os.path.expanduser("~/.hermes/config.yaml")
if not os.path.exists(cfg_path):
    print("config.yaml not found, skipping patch")
    sys.exit(0)

content = open(cfg_path).read()

# --- STEP 1: Pre-fix corruption ---
# Drive मधून restore होणारा corrupt config असा दिसतो:
#   model: auto          <- scalar value (मागच्या patch ने set केला)
#     provider: "auto"   <- orphaned indented sub-key (yaml invalid)
#     per_platform: ...
# Fix: "model: <scalar>\n  <indented lines>" → "model: <scalar>\n"
# (कोणत्याही indentation वर "model:" सापडलं तरी चालेल, फक्त top-level नाही)
content = re.sub(
    r'^([ \t]*model\s*:[^\n\S]*\S[^\n]*)\n((?:[ \t]+[^\n]*\n)*)',
    lambda m: m.group(1) + '\n',
    content,
    flags=re.MULTILINE
)

# --- STEP 2: YAML parse + patch ---
try:
    import yaml

    cfg = yaml.safe_load(content)

    if not isinstance(cfg, dict):
        print("config.yaml unexpected format, using regex fallback")
        raise ValueError("not a dict")

    # model → auto (nested dict असो किंवा scalar)
    cfg["model"] = "auto"

    # language → en
    if not isinstance(cfg.get("ui"), dict):
        cfg["ui"] = {}
    cfg["ui"]["language"] = "en"

    open(cfg_path, "w").write(
        yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False)
    )
    print("config.yaml patched via yaml: model=auto, language=en")

except Exception as e:
    # Fallback: yaml failed (still corrupt or no yaml module)
    # Step 1 ने corruption already clean केली, आता regex ने model + language patch

    # language
    if re.search(r'^ui\s*:', content, re.MULTILINE):
        content = re.sub(r'(language\s*:)\s*\S+', r'\1 en', content)
    elif 'language:' in content:
        content = re.sub(r'language:\s*\S+', 'language: en', content)
    else:
        content += '\nui:\n  language: en\n'

    # model (corruption already removed in step 1, just ensure value is auto)
    if re.search(r'^model\s*:', content, re.MULTILINE):
        content = re.sub(r'^model\s*:[^\n]*', 'model: auto', content, flags=re.MULTILINE)
    else:
        content += '\nmodel: auto\n'

    open(cfg_path, "w").write(content)
    print("config.yaml patched via regex fallback (yaml error: " + str(e) + ")")
PYEOF

# --- STEP 3: Verify — patch नंतरही parse fail होत असेल तर broken copy बाजूला ठेवून log कर ---
if ! "$PATCH_PY" -c "
import sys
try:
    import yaml
    yaml.safe_load(open('$HOME/.hermes/config.yaml').read())
except ImportError:
    sys.exit(0)  # yaml module नाहीच, verify करता येत नाही — Dockerfile fix लावा
except Exception as e:
    print(e)
    sys.exit(1)
" 2>/tmp/config_check.err; then
  echo "⚠️  config.yaml अजूनही invalid आहे patch नंतर:"
  cat /tmp/config_check.err
  cp ~/.hermes/config.yaml "$HOME/.hermes/config.yaml.broken.$(date +%s)" 2>/dev/null || true
  echo "⚠️  Broken copy backup केली. Telegram/plugin loading fail होत राहील जोपर्यंत हे मॅन्युअली फिक्स होत नाही."
else
  echo "✅ config.yaml valid आहे."
fi

# Symlink Himalaya config
mkdir -p ~/.config/himalaya
if [ -f ~/.hermes/skills/email/himalaya/config.toml ]; then
  ln -sf ~/.hermes/skills/email/himalaya/config.toml ~/.config/himalaya/config.toml
fi

# Fix script permissions after restore
if [ -d ~/.hermes/scripts ]; then
  chmod +x ~/.hermes/scripts/*.sh 2>/dev/null || true
fi

# 6. STARTUP NOTIFICATION
FIRST_USER=$(echo "${TELEGRAM_ALLOWED_USERS}" | cut -d',' -f1 | tr -d ' ')
if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$FIRST_USER" ]; then
  echo ">> Sending Telegram startup notification to ${FIRST_USER}..."
  curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${FIRST_USER}" \
    --data-urlencode "text=🟢 Hermes is back online!
⏰ $(date '+%d %b %Y %I:%M %p IST')

✅ Drive restore complete
🚀 Gateway connecting now...

📋 Check all pending tasks." > /dev/null 2>&1
  echo ">> Notification sent."
fi

# Signal keep-alive that startup is complete
echo ">> Signaling startup complete..."
curl -s "http://localhost:${PORT:-10000}/startup-ready" || true

# ── Cache Clean Function ──
clean_cache() {
  echo ">> [Cache Clean] Running scheduled cache cleanup..."
  rm -rf ~/.hermes/cache/* 2>/dev/null || true
  rm -rf ~/.hermes/audio_cache/* 2>/dev/null || true
  rm -rf ~/.hermes/image_cache/* 2>/dev/null || true
  rm -rf ~/.hermes/tmp/* 2>/dev/null || true
  find ~/.hermes -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
  find ~/.hermes -name "*.pyc" -delete 2>/dev/null || true
  rm -rf /root/.cache/pip 2>/dev/null || true
  find /tmp -maxdepth 1 -type f -mmin +30 -delete 2>/dev/null || true
  rm -rf /root/.npm/_cacache 2>/dev/null || true
  echo ">> [Cache Clean] Done. RAM free: $(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)MB"
}

# 7. Background Sync + Cache Clean Loop
sync_to_cloud() {
  if [ -f ~/.config/rclone/rclone.conf ]; then
    rclone_sync sync ~/.hermes/ "$REMOTE_BACKUP" --drive-chunk-size 8M --fast-list || true
  fi
}

# Patch नंतर लगेच एकदा push करतो — जेणेकरून fixed config.yaml Drive वर लगेच
# save होईल आणि पुढच्या restart ला तोच जुना corrupt copy परत restore होणार नाही.
echo ">> Pushing (possibly fixed) config.yaml back to Drive..."
sync_to_cloud

(
  LOOP_COUNT=0
  while true; do
    sleep 300
    sync_to_cloud
    LOOP_COUNT=$(( LOOP_COUNT + 1 ))
    if [ $(( LOOP_COUNT % 12 )) -eq 0 ]; then
      clean_cache
    fi
  done
) &
SYNC_PID=$!

# 8. Trap for graceful shutdown
cleanup() {
  echo ">> Container shutting down. Performing final sync..."
  kill $SYNC_PID 2>/dev/null || true
  sync_to_cloud
  exit 0
}
trap cleanup SIGTERM SIGINT EXIT

# 9. Start Hermes Dashboard (port 9119)
# FIX: 0.0.0.0 वर bind केलं की auth gate engage होतो आणि auth provider
# नसल्यामुळे dashboard bind refuse करतो (log मधला "Refusing to bind
# dashboard to 0.0.0.0" error). 127.0.0.1 (loopback) वर bind केलं की auth
# शिवाय चालतं — मग keep_alive.py मधला /dashboard reverse-proxy route
# त्याच्यापर्यंत पोहोचवतो, त्यामुळे बाहेरून एकाच public port (10000) वरून
# dashboard access होतो.
echo ">> Starting Hermes Dashboard on 127.0.0.1:9119 (proxied via /dashboard)..."
hermes dashboard --host 127.0.0.1 --port 9119 --no-open &
DASHBOARD_PID=$!
echo ">> Dashboard PID: $DASHBOARD_PID"

sleep 3

# 10. Start Watchdog (manages Hermes Gateway)
echo ">> Starting Watchdog..."
/app/watchdog.sh &

wait
