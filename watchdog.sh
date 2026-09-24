#!/bin/bash
# ============================================================
# Hermes Watchdog — safe, low-RAM, exponential backoff
# ============================================================

WATCHDOG_CHECK_INTERVAL="${WATCHDOG_CHECK_INTERVAL:-30}"
GATEWAY_MAX_RESTARTS="${GATEWAY_MAX_RESTARTS:-8}"

# Exponential backoff: 5s, 10s, 20s, 40s, 60s, 60s, 60s...
BACKOFF_DELAYS=(5 10 20 40 60 60 60 60)

RESTART_COUNT=0
GATEWAY_PID=""
GATEWAY_PID_FILE="/tmp/hermes_gateway.pid"
WATCHDOG_LOG="$HOME/.hermes/logs/watchdog.log"
mkdir -p "$(dirname "$WATCHDOG_LOG")"

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [WATCHDOG] $*"
    echo "$msg" | tee -a "$WATCHDOG_LOG"
}

get_delay() {
    local idx=$1
    local max_idx=$(( ${#BACKOFF_DELAYS[@]} - 1 ))
    if [ "$idx" -gt "$max_idx" ]; then idx=$max_idx; fi
    echo "${BACKOFF_DELAYS[$idx]}"
}

start_gateway() {
    # आधीचा process clean करा
    if [ -n "$GATEWAY_PID" ] && kill -0 "$GATEWAY_PID" 2>/dev/null; then
        log "🛑 Killing old gateway PID $GATEWAY_PID..."
        kill -TERM "$GATEWAY_PID" 2>/dev/null || true
        sleep 2
        kill -9 "$GATEWAY_PID" 2>/dev/null || true
    fi

    log "▶️  Starting Hermes Gateway (restart #$RESTART_COUNT)..."
    hermes gateway run &
    GATEWAY_PID=$!
    echo "$GATEWAY_PID" > "$GATEWAY_PID_FILE"
    log "Gateway PID: $GATEWAY_PID"
}

is_gateway_alive() {
    [ -n "$GATEWAY_PID" ] && kill -0 "$GATEWAY_PID" 2>/dev/null
}

log "🐕 Watchdog started. Check interval: ${WATCHDOG_CHECK_INTERVAL}s, Max restarts: $GATEWAY_MAX_RESTARTS"

# पहिला start
start_gateway

while true; do
    sleep "$WATCHDOG_CHECK_INTERVAL"

    if is_gateway_alive; then
        # Gateway alive — restart count हळूहळू reset करतो
        # (stable राहिल्यावर backoff counter कमी होतो)
        if [ "$RESTART_COUNT" -gt 0 ]; then
            RESTART_COUNT=$(( RESTART_COUNT - 1 ))
            log "✅ Gateway stable. Cooling down restart count → $RESTART_COUNT"
        fi
        continue
    fi

    # Gateway down
    RESTART_COUNT=$(( RESTART_COUNT + 1 ))
    log "💥 Gateway is DOWN! (crash count: $RESTART_COUNT / $GATEWAY_MAX_RESTARTS)"

    if [ "$RESTART_COUNT" -ge "$GATEWAY_MAX_RESTARTS" ]; then
        log "🚨 Too many crashes. Container restart trigger करतो..."
        # keep_alive.py kill केल्यावर Koyeb health check fail होतो → container restart
        pkill -f "keep_alive.py" 2>/dev/null || true
        sleep 10
        exit 1
    fi

    DELAY=$(get_delay $(( RESTART_COUNT - 1 )))
    log "⏳ Waiting ${DELAY}s before restart (backoff)..."
    sleep "$DELAY"

    start_gateway
done
