#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export TZ="Asia/Kolkata"
echo "=== [Hermes Web Dashboard & Gateway + Keep Alive] ==="

PORT="${PORT:-10000}"

# 1. Google Drive Restore (Keep your history)
if [ -n "$RCLONE_CONFIG_BASE64" ]; then
    mkdir -p ~/.config/rclone
    echo "$RCLONE_CONFIG_BASE64" | base64 -d > ~/.config/rclone/rclone.conf
fi
REMOTE_BACKUP="${RCLONE_REMOTE:-gdrive:hermes_backup}"
if [ -f ~/.config/rclone/rclone.conf ]; then
    mkdir -p ~/.hermes
    rclone sync "$REMOTE_BACKUP" ~/.hermes/ --exclude "cache/**" || true
fi

# 2. Setup Node.js Reverse Proxy (Routes / to keep_alive and /dashboard to Hermes)
echo ">> Setting up Smart Router Proxy..."
mkdir -p /tmp/proxy && cd /tmp/proxy
npm install http-proxy > /dev/null 2>&1

cat << 'EOF' > server.js
const http = require('http');
const httpProxy = require('http-proxy');

const proxy = httpProxy.createProxyServer({ ws: true });
const PORT = process.env.PORT || 10000;
const PASSWORD = process.env.PASSWORD || "admin";

proxy.on('error', function (err, req, res) {
  if (res && res.writeHead) { res.writeHead(500, {'Content-Type': 'text/plain'}); res.end('Proxy Error'); }
});

const server = http.createServer((req, res) => {
    // Protected Dashboard Route
    if (req.url.startsWith('/dashboard')) {
        const auth = req.headers['authorization'];
        if (!auth) {
            res.setHeader('WWW-Authenticate', 'Basic realm="Hermes Dashboard"');
            res.writeHead(401);
            return res.end('Auth required');
        }
        const b64auth = (auth || '').split(' ')[1] || '';
        const [login, password] = Buffer.from(b64auth, 'base64').toString().split(':');
        if (password !== PASSWORD) {
            res.setHeader('WWW-Authenticate', 'Basic realm="Hermes Dashboard"');
            res.writeHead(401);
            return res.end('Access denied');
        }
        
        req.url = req.url.replace('/dashboard', '') || '/';
        proxy.web(req, res, { target: 'http://127.0.0.1:9119' });
    } else {
        // Keep Alive Stats
        proxy.web(req, res, { target: 'http://127.0.0.1:10001' });
    }
});

server.on('upgrade', (req, socket, head) => {
    if (req.url.startsWith('/dashboard')) {
        req.url = req.url.replace('/dashboard', '') || '/';
        proxy.ws(req, socket, head, { target: 'http://127.0.0.1:9119' });
    } else {
        proxy.ws(req, socket, head, { target: 'http://127.0.0.1:10001' });
    }
});

server.listen(PORT, () => console.log(`Routing Proxy running on port ${PORT}...`));
EOF

# Start Proxy
node server.js &
cd /app

# 3. Start Keep Alive Server (Hidden on port 10001)
if [ -f /app/keep_alive.py ]; then
    echo ">> Starting keep-alive HTTP server on internal port 10001..."
    export PORT=10001
    python3 /app/keep_alive.py &
fi

# 4. Start Hermes Web Dashboard (Hidden on port 9119)
echo ">> Starting Hermes Web Dashboard on internal port 9119..."
hermes dashboard --host 127.0.0.1 --port 9119 &

# 5. Background Sync Loop
sync_to_cloud() {
    if [ -f ~/.config/rclone/rclone.conf ]; then
        rclone sync ~/.hermes/ "$REMOTE_BACKUP" --exclude "cache/**" || true
    fi
}
(while true; do sleep 60; sync_to_cloud; done) &
SYNC_PID=$!
trap 'kill $SYNC_PID; sync_to_cloud; exit 0' SIGTERM SIGINT EXIT

# 6. Start Hermes Gateway
echo ">> Starting Hermes Gateway..."
hermes gateway run

