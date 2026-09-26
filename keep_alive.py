import http.server
import json
import os
import subprocess
import time
import threading
import urllib.request
import urllib.error

last_cpu_times = [0, 0]

STATS_CACHE_SECONDS = int(os.environ.get("STATS_CACHE_SECONDS", "10"))
_stats_cache = {"time": 0.0, "data": None}

# Dashboard proxy target
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 9119

# Startup state
_startup_state = {"phase": "waiting", "message": "Restoring state from Google Drive..."}
_startup_lock = threading.Lock()

def set_startup_ready():
    with _startup_lock:
        _startup_state["phase"] = "ready"
        _startup_state["message"] = "Hermes is fully operational."

def get_startup_phase():
    with _startup_lock:
        return _startup_state.copy()

def get_cpu():
    global last_cpu_times
    try:
        with open('/proc/stat') as f:
            line = f.readline()
        fields = [float(x) for x in line.strip().split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        total = sum(fields)
        diff_total = total - last_cpu_times[0]
        diff_idle = idle - last_cpu_times[1]
        last_cpu_times[0] = total
        last_cpu_times[1] = idle
        if diff_total > 0:
            return min(100.0, round(100.0 * (1.0 - (diff_idle / diff_total)) * 10.0, 1))
    except Exception:
        pass
    return 0.0

def get_stats():
    now = time.time()
    if _stats_cache["data"] is not None and (now - _stats_cache["time"]) < STATS_CACHE_SECONDS:
        return _stats_cache["data"]
    data = _compute_stats()
    _stats_cache["time"] = now
    _stats_cache["data"] = data
    return data

def _compute_stats():
    total_mem = 512.0
    used_mem = 350.0
    try:
        with open('/proc/meminfo') as f:
            mem_info = {}
            for line in f:
                parts = line.split(':')
                if len(parts) == 2:
                    mem_info[parts[0].strip()] = int(parts[1].strip().split()[0])
            total_kb = mem_info.get('MemTotal', 512 * 1024)
            avail_kb = mem_info.get('MemAvailable', mem_info.get('MemFree', 0))
            used_kb = total_kb - avail_kb
            used_mem = round(used_kb / 1024, 1)
    except Exception:
        pass

    mem_percent = round((used_mem / total_mem) * 100, 1)
    total_disk_mb = 2000.0
    used_mb = 0.0
    try:
        result = subprocess.check_output(['du', '-sm', '/root', '/app']).decode('utf-8')
        used_mb = sum(int(line.split()[0]) for line in result.splitlines())
    except Exception:
        pass

    disk_percent = round((used_mb / total_disk_mb) * 100, 1)
    cpu_percent = get_cpu()

    load_avg = [0.0, 0.0, 0.0]
    try:
        with open('/proc/loadavg') as f:
            load_avg = [float(x) for x in f.read().split()[:3]]
    except Exception:
        pass

    uptime_str = "0h 0m"
    try:
        with open('/proc/uptime') as f:
            uptime_seconds = float(f.read().split()[0])
            hours = int(uptime_seconds // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            uptime_str = f"{hours}h {minutes}m"
    except Exception:
        pass

    processes = []
    try:
        ps_out = subprocess.check_output(['ps', '-eo', 'pid,comm,%mem,%cpu', '--sort=-%mem']).decode('utf-8').splitlines()
        for line in ps_out[1:8]:
            parts = line.split()
            if len(parts) >= 4:
                processes.append({'pid': parts[0], 'command': parts[1], 'mem': parts[2], 'cpu': parts[3]})
    except Exception:
        pass

    phase_info = get_startup_phase()

    return {
        "memory": {"total_mb": 512, "used_mb": used_mem, "free_mb": max(0, round(512 - used_mem, 1)), "percent": min(100.0, mem_percent)},
        "disk": {"total_mb": 2000, "used_mb": used_mb, "free_mb": max(0, round(2000 - used_mb, 1)), "percent": min(100.0, disk_percent)},
        "cpu": {"percent": cpu_percent, "load_avg": load_avg},
        "uptime": uptime_str,
        "processes": processes,
        "startup": phase_info
    }

def is_dashboard_alive():
    """9119 वर dashboard चालू आहे का check करतो"""
    try:
        req = urllib.request.Request(f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/", method="HEAD")
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False

def proxy_to_dashboard(handler, path):
    """Request 9119 ला forward करतो"""
    target_url = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}{path}"
    try:
        # Request body वाचतो (POST साठी)
        content_length = int(handler.headers.get('Content-Length', 0))
        body = handler.rfile.read(content_length) if content_length > 0 else None

        req = urllib.request.Request(target_url, data=body, method=handler.command)

        # Headers copy करतो (hop-by-hop सोडून)
        skip_headers = {'host', 'connection', 'transfer-encoding', 'keep-alive'}
        for key, val in handler.headers.items():
            if key.lower() not in skip_headers:
                req.add_header(key, val)

        with urllib.request.urlopen(req, timeout=30) as resp:
            handler.send_response(resp.status)
            # Response headers copy
            for key, val in resp.headers.items():
                if key.lower() not in {'transfer-encoding', 'connection'}:
                    handler.send_header(key, val)
            handler.end_headers()
            handler.wfile.write(resp.read())
    except urllib.error.HTTPError as e:
        handler.send_response(e.code)
        handler.end_headers()
        handler.wfile.write(e.read())
    except Exception as e:
        # Dashboard अजून चालू नाही
        handler.send_response(503)
        handler.send_header('Content-type', 'text/html')
        handler.end_headers()
        handler.wfile.write(f"""
        <html><body style="background:#0f172a;color:#f8fafc;font-family:sans-serif;padding:40px;text-align:center;">
        <h2>⏳ Dashboard Starting...</h2>
        <p style="color:#94a3b8">Hermes Dashboard अजून चालू होत आहे. 10-15 seconds मध्ये refresh करा.</p>
        <p style="color:#64748b;font-size:12px">Error: {str(e)}</p>
        <script>setTimeout(()=>location.reload(), 5000);</script>
        </body></html>
        """.encode('utf-8'))

# Startup banner
STARTUP_BANNER = """
<div id="startup-banner" style="
    background: linear-gradient(135deg, #1e3a5f, #0f2744);
    border: 1px solid #38bdf8;
    border-radius: 12px;
    padding: 20px 24px;
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    gap: 16px;
">
  <div style="font-size: 32px;">⏳</div>
  <div>
    <div style="color: #38bdf8; font-weight: 700; font-size: 16px;" id="banner-title">
      Restoring from Google Drive...
    </div>
    <div style="color: #94a3b8; font-size: 13px; margin-top: 4px;" id="banner-sub">
      Hermes history, skills, and state are being restored. Gateway will start automatically once ready.
    </div>
  </div>
</div>
"""

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hermes Agent Live Stats (Koyeb Free Tier)</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
        body { background-color: #0f172a; color: #f8fafc; padding: 20px; min-height: 100vh; }
        .container { max-width: 900px; margin: 0 auto; }
        header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; border-bottom: 1px solid #1e293b; padding-bottom: 16px; }
        h1 { font-size: 24px; color: #38bdf8; display: flex; align-items: center; gap: 10px; }
        .status-badge { background-color: #065f46; color: #34d399; padding: 4px 12px; border-radius: 9999px; font-size: 12px; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }
        .status-dot { width: 8px; height: 8px; background-color: #34d399; border-radius: 50%; animation: pulse 2s infinite; }
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .card { background-color: #1e293b; border-radius: 12px; padding: 20px; border: 1px solid #334155; }
        .card h2 { font-size: 14px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; display: flex; justify-content: space-between; }
        .value { font-size: 26px; font-weight: 700; color: #f8fafc; margin-bottom: 8px; }
        .subtext { font-size: 13px; color: #64748b; }
        .progress-bar { width: 100%; height: 8px; background-color: #334155; border-radius: 4px; overflow: hidden; margin-top: 12px; }
        .progress-fill { height: 100%; background-color: #38bdf8; transition: width 0.5s ease; border-radius: 4px; }
        .table-card { background-color: #1e293b; border-radius: 12px; padding: 20px; border: 1px solid #334155; }
        .table-card h2 { font-size: 16px; color: #f8fafc; margin-bottom: 16px; }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }
        th, td { padding: 10px 12px; border-bottom: 1px solid #334155; }
        th { color: #94a3b8; font-weight: 600; font-size: 12px; text-transform: uppercase; }
        td { color: #e2e8f0; }
        tr:last-child td { border-bottom: none; }
        footer { text-align: center; margin-top: 24px; color: #64748b; font-size: 12px; }
        .dashboard-btn { display: inline-block; margin-top: 16px; padding: 10px 24px; background: linear-gradient(135deg, #38bdf8, #0ea5e9); color: #0f172a; border-radius: 8px; font-weight: 700; text-decoration: none; font-size: 14px; }
        .dashboard-btn:hover { opacity: 0.9; }
        #startup-banner { transition: opacity 0.5s; }
        #startup-banner.hidden { opacity: 0; pointer-events: none; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>⚡ Hermes Agent Live Stats</h1>
            <div class="status-badge"><span class="status-dot"></span> 0.1 vCPU / 512 MB RAM / 2000 MB Disk</div>
        </header>

        """ + STARTUP_BANNER + """

        <!-- Dashboard Quick Link -->
        <div style="background:#1e293b;border-radius:12px;padding:16px 20px;margin-bottom:24px;border:1px solid #334155;display:flex;align-items:center;justify-content:space-between;">
          <div>
            <div style="color:#f8fafc;font-weight:700;font-size:15px;">🖥️ Hermes Web Dashboard</div>
            <div style="color:#94a3b8;font-size:13px;margin-top:4px;">Sessions, Skills, API Keys, Jobs सगळं इथे</div>
          </div>
          <div id="dash-status" style="display:flex;align-items:center;gap:12px;">
            <span id="dash-badge" style="font-size:12px;color:#64748b;">Checking...</span>
            <a href="/ui/" target="_blank" class="dashboard-btn">Open Dashboard →</a>
          </div>
        </div>

        <div class="grid">
            <div class="card">
                <h2>RAM Usage (512 MB) <span id="mem-pct">0%</span></h2>
                <div class="value" id="mem-val">0 MB / 512 MB</div>
                <div class="subtext" id="mem-free">Free: 512 MB</div>
                <div class="progress-bar"><div class="progress-fill" id="mem-bar" style="width: 0%;"></div></div>
            </div>
            <div class="card">
                <h2>Disk Storage (2000 MB) <span id="disk-pct">0%</span></h2>
                <div class="value" id="disk-val">0 MB / 2000 MB</div>
                <div class="subtext" id="disk-free">Free: 2000 MB</div>
                <div class="progress-bar"><div class="progress-fill" id="disk-bar" style="width: 0%; background-color: #34d399;"></div></div>
            </div>
            <div class="card">
                <h2>vCPU Load (0.1 vCPU) <span id="cpu-pct">0%</span></h2>
                <div class="value" id="cpu-val">0%</div>
                <div class="subtext" id="load-avg">Load Avg: 0.00, 0.00, 0.00</div>
                <div class="progress-bar"><div class="progress-fill" id="cpu-bar" style="width: 0%; background-color: #f43f5e;"></div></div>
            </div>
        </div>
        <div class="table-card">
            <h2>System Info & Top Processes</h2>
            <div style="margin-bottom: 16px; font-size: 14px; color: #94a3b8;">
                System Uptime: <strong id="uptime" style="color: #f8fafc;">...</strong>
            </div>
            <table>
                <thead><tr><th>PID</th><th>Command</th><th>RAM %</th><th>CPU %</th></tr></thead>
                <tbody id="proc-table">
                    <tr><td colspan="4" style="text-align: center; color: #64748b;">Loading...</td></tr>
                </tbody>
            </table>
        </div>
        <footer>Auto-refreshing every 2 seconds • Koyeb Free Tier Monitoring</footer>
    </div>
    <script>
        function updateStats() {
            fetch('/api/stats')
                .then(res => res.json())
                .then(data => {
                    document.getElementById('mem-val').innerText = data.memory.used_mb + ' MB / 512 MB';
                    document.getElementById('mem-pct').innerText = data.memory.percent + '%';
                    document.getElementById('mem-free').innerText = 'Free: ' + data.memory.free_mb + ' MB';
                    document.getElementById('mem-bar').style.width = data.memory.percent + '%';
                    document.getElementById('disk-val').innerText = data.disk.used_mb + ' MB / 2000 MB';
                    document.getElementById('disk-pct').innerText = data.disk.percent + '%';
                    document.getElementById('disk-free').innerText = 'Free: ' + data.disk.free_mb + ' MB';
                    document.getElementById('disk-bar').style.width = data.disk.percent + '%';
                    document.getElementById('cpu-val').innerText = data.cpu.percent + '%';
                    document.getElementById('cpu-pct').innerText = data.cpu.percent + '%';
                    document.getElementById('load-avg').innerText = 'Load Avg: ' + data.cpu.load_avg.join(', ');
                    document.getElementById('cpu-bar').style.width = data.cpu.percent + '%';
                    document.getElementById('uptime').innerText = data.uptime;

                    if (data.startup && data.startup.phase === 'ready') {
                        var banner = document.getElementById('startup-banner');
                        if (banner) banner.classList.add('hidden');
                    } else if (data.startup) {
                        var title = document.getElementById('banner-title');
                        if (title) title.innerText = data.startup.message || 'Restoring...';
                    }

                    var tbody = document.getElementById('proc-table');
                    tbody.innerHTML = '';
                    data.processes.forEach(function(p) {
                        var tr = document.createElement('tr');
                        tr.innerHTML = '<td>' + p.pid + '</td><td>' + p.command + '</td><td>' + p.mem + '%</td><td>' + p.cpu + '%</td>';
                        tbody.appendChild(tr);
                    });
                })
                .catch(function(err) { console.error('Stats fetch error:', err); });
        }

        // Dashboard status check
        function checkDashboard() {
            fetch('/ui/').then(res => {
                var badge = document.getElementById('dash-badge');
                if (res.ok || res.status === 200) {
                    badge.innerHTML = '<span style="color:#34d399;">● Online</span>';
                } else {
                    badge.innerHTML = '<span style="color:#f59e0b;">● Starting...</span>';
                }
            }).catch(() => {
                document.getElementById('dash-badge').innerHTML = '<span style="color:#f43f5e;">● Offline</span>';
            });
        }

        setInterval(updateStats, 2000);
        setInterval(checkDashboard, 5000);
        updateStats();
        checkDashboard();
    </script>
</body>
</html>
"""

SILENT_ROUTES = {'/', '/api/stats'}

class PingHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        request_line = args[0] if args else ''
        for route in SILENT_ROUTES:
            if f'GET {route} ' in request_line or f'HEAD {route} ' in request_line:
                return
        super().log_message(format, *args)

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_HEAD(self):
        if self.path in ('/', '/memory', '/api/stats'):
            self.send_response(200)
            self.end_headers()
        else:
            proxy_to_dashboard(self, self.path)

    def _handle(self):
        if self.path == '/memory' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode('utf-8'))
        elif self.path == '/api/stats':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(get_stats()).encode('utf-8'))
        elif self.path == '/startup-ready':
            set_startup_ready()
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"OK")
        elif self.path.startswith('/ui') or self.path.startswith('/api/') and not self.path == '/api/stats':
            # /ui/* आणि /api/* (stats सोडून) → 9119 ला proxy
            proxy_to_dashboard(self, self.path.replace('/ui', '', 1) if self.path.startswith('/ui') else self.path)
        else:
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"Hermes Agent is Alive and Running!")

port = int(os.environ.get("PORT", 10000))
httpd = http.server.HTTPServer(('0.0.0.0', port), PingHandler)
print(f"Ping server running on port {port} | Dashboard proxy: /ui/ → 127.0.0.1:9119")
httpd.serve_forever()
