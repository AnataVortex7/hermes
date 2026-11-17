import http.server
import base64
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request

from sysstats import Stats
import restore_progress

# ── Accurate container stats (cgroup based, see sysstats.py) ──
STATS = Stats()
STATS.start()


def get_stats():
    STATS.touch()                      # somebody is looking -> sample fast; idle again ~15 s after the last request
    snap = STATS.snapshot()
    snap["platform"] = _platform()
    snap["startup"] = get_startup_phase()
    return snap


def _platform():
    env = os.environ
    if env.get("RENDER"):
        return "Render"
    if env.get("KOYEB_SERVICE_NAME") or env.get("KOYEB_APP_NAME"):
        return "Koyeb"
    if env.get("FLY_APP_NAME"):
        return "Fly.io"
    return "Container"


# ── PicoClaw route ──
# /picoclaw        -> PicoClaw web UI on 127.0.0.1:PICOCLAW_UI_PORT (18800) if something listens there,
#                     otherwise a built-in status page (gateway health/ready + LLM proxy).
# /picoclaw/status.json -> same status as JSON.
# Only ONE public port is exposed by the host, so everything is reached through this server.
# Optional protection: UI_BASIC_AUTH="user:password" (recommended - the route is public otherwise).
PICOCLAW_PREFIX = "/picoclaw"
PICOCLAW_HOST = "127.0.0.1"
PICOCLAW_UI_PORT = int(os.environ.get("PICOCLAW_UI_PORT", "18800"))
PICOCLAW_HEALTH_PORT = int(os.environ.get("PICOCLAW_HEALTH_PORT", "18790"))
LLM_PROXY_PORT = int(os.environ.get("LOCAL_LLM_PROXY_PORT", "18799"))
UI_BASIC_AUTH = os.environ.get("UI_BASIC_AUTH", "").strip()

HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}

# ── Startup state — gateway ला signal करायला वापरतो ──
# "waiting" : Drive restore अजून चालू आहे
# "ready"   : Restore झालं, gateway restart झालं
_startup_state = {"phase": "waiting", "message": "Restoring state from Google Drive..."}
_startup_lock = threading.Lock()


def set_startup_ready():
    with _startup_lock:
        _startup_state["phase"] = "ready"
        _startup_state["message"] = "Hermes is fully operational."
    # also persisted in the progress file: if this process is restarted, the dashboard must not fall back to "restoring"
    restore_progress.update_state(stage="ready", ready=True, message="Hermes is fully operational.")


def get_startup_phase():
    """{"phase": waiting|ready, "message", "restore": {live restore progress written by restore_progress.py}}"""
    with _startup_lock:
        out = _startup_state.copy()
    st = restore_progress.read_state()
    if st.get("ready"):
        out["phase"] = "ready"
    if out["phase"] != "ready" and st.get("stage"):
        out["message"] = st.get("message") or out["message"]
    restore = {k: st[k] for k in (
        "stage", "message", "attempt", "attempts", "phase", "phases", "label", "percent", "bytes_done",
        "bytes_total", "files_done", "files_total", "speed_bps", "eta_seconds", "elapsed_seconds", "estimated",
        "current", "error") if k in st}
    if restore:
        age = max(0, int(time.time() - st.get("updated", time.time())))
        restore["age_seconds"] = age
        restore["stalled"] = st.get("stage") in ("scanning", "restoring") and age > 30   # no update for 30s
    out["restore"] = restore
    out["clean_start"] = bool(st.get("clean_start"))
    return out


def port_open(port, timeout=0.5):
    try:
        with socket.create_connection((PICOCLAW_HOST, port), timeout=timeout):
            return True
    except OSError:
        return False


def _fetch(port, path, timeout=2):
    try:
        with urllib.request.urlopen(f"http://{PICOCLAW_HOST}:{port}{path}", timeout=timeout) as r:
            body = r.read(4000).decode("utf-8", "replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001
        return None, ""


def picoclaw_status():
    h_code, h_body = _fetch(PICOCLAW_HEALTH_PORT, "/health")
    r_code, r_body = _fetch(PICOCLAW_HEALTH_PORT, "/ready")
    return {
        "gateway_listening": port_open(PICOCLAW_HEALTH_PORT),
        "health": {"http": h_code, "body": h_body.strip()[:500]},
        "ready": {"http": r_code, "body": r_body.strip()[:500]},
        "ready_ok": r_code == 200,
        "llm_proxy_listening": port_open(LLM_PROXY_PORT),
        "web_ui_listening": port_open(PICOCLAW_UI_PORT),
        "web_ui_port": PICOCLAW_UI_PORT,
    }


STATUS_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>PicoClaw status</title>
<style>
body{background:#0f172a;color:#f8fafc;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;padding:20px;max-width:760px;margin:0 auto}
h1{color:#38bdf8;font-size:22px}.card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:16px;margin:14px 0}
.ok{color:#34d399}.bad{color:#f43f5e}.muted{color:#94a3b8;font-size:13px}pre{white-space:pre-wrap;word-break:break-word;color:#cbd5e1;font-size:12px}
a{color:#38bdf8}
</style></head><body>
<h1>🦞 PicoClaw</h1><div class="muted"><a href="/memory">← Live stats</a></div>
<div class="card"><div>Gateway: <b id="gw">...</b></div><div>LLM guard proxy: <b id="px">...</b></div>
<div>Web UI (port <span id="uip">?</span>): <b id="ui">...</b></div></div>
<div class="card"><div class="muted">/ready</div><pre id="ready">...</pre><div class="muted">/health</div><pre id="health">...</pre></div>
<div class="muted" id="hint"></div>
<script>
function set(id,ok,t){var e=document.getElementById(id);e.textContent=t;e.className=ok?'ok':'bad';}
function upd(){fetch('/picoclaw/status.json').then(function(r){return r.json()}).then(function(s){
 set('gw',s.ready_ok,s.ready_ok?'ready ✅':(s.gateway_listening?'running, not ready yet':'not running ❌'));
 set('px',s.llm_proxy_listening,s.llm_proxy_listening?'up ✅':'down ❌');
 set('ui',s.web_ui_listening,s.web_ui_listening?'up ✅ (reload this page to open it)':'not running');
 document.getElementById('uip').textContent=s.web_ui_port;
 document.getElementById('ready').textContent='HTTP '+s.ready.http+'  '+s.ready.body;
 document.getElementById('health').textContent='HTTP '+s.health.http+'  '+s.health.body;
 document.getElementById('hint').textContent=s.web_ui_listening?'':'No PicoClaw web UI is listening here. Start it on this port and this route will show it automatically.';
}).catch(function(){});}
setInterval(upd,3000);upd();
</script></body></html>"""


# ── /memory page ──
STARTUP_BANNER = """
<div id="startup-banner" class="hidden">
  <div class="sb-top">
    <div class="sb-icon" id="banner-icon">&#9203;</div>
    <div class="sb-text">
      <div class="sb-title" id="banner-title">Starting up...</div>
      <div class="sb-sub" id="banner-sub"></div>
    </div>
    <div class="sb-pct" id="banner-pct"></div>
  </div>
  <div class="sb-bar"><div class="sb-fill" id="banner-bar" style="width:0%"></div></div>
  <div class="sb-grid" id="banner-grid">
    <div><span>Restored</span><b id="sb-done">-</b></div>
    <div><span>Remaining</span><b id="sb-left">-</b></div>
    <div><span>Speed</span><b id="sb-speed">-</b></div>
    <div><span>Time left</span><b id="sb-eta">-</b></div>
  </div>
  <div class="sb-line" id="banner-files"></div>
  <div class="sb-err" id="banner-err"></div>
</div>
"""

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes Live Stats</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
body { background-color: #0f172a; color: #f8fafc; padding: 20px; min-height: 100vh; }
.container { max-width: 900px; margin: 0 auto; }
header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 24px; border-bottom: 1px solid #1e293b; padding-bottom: 16px; }
h1 { font-size: 24px; color: #38bdf8; display: flex; align-items: center; gap: 10px; }
.status-badge { background-color: #065f46; color: #34d399; padding: 4px 12px; border-radius: 9999px; font-size: 12px; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }
.status-dot { width: 8px; height: 8px; background-color: #34d399; border-radius: 50%; animation: pulse 2s infinite; }
@keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin-bottom: 24px; }
.card { background-color: #1e293b; border-radius: 12px; padding: 20px; border: 1px solid #334155; }
.card h2 { font-size: 14px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; display: flex; justify-content: space-between; }
.value { font-size: 24px; font-weight: 700; color: #f8fafc; margin-bottom: 8px; }
.subtext { font-size: 13px; color: #64748b; line-height: 1.6; }
.progress-bar { width: 100%; height: 8px; background-color: #334155; border-radius: 4px; overflow: hidden; margin-top: 12px; }
.progress-fill { height: 100%; background-color: #38bdf8; transition: width 0.5s ease; border-radius: 4px; }
.table-card { background-color: #1e293b; border-radius: 12px; padding: 20px; border: 1px solid #334155; overflow-x: auto; }
.table-card h2 { font-size: 16px; color: #f8fafc; margin-bottom: 16px; }
table { width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }
th, td { padding: 10px 12px; border-bottom: 1px solid #334155; }
th { color: #94a3b8; font-weight: 600; font-size: 12px; text-transform: uppercase; }
td { color: #e2e8f0; }
tr:last-child td { border-bottom: none; }
footer { text-align: center; margin-top: 24px; color: #64748b; font-size: 12px; line-height: 1.6; }
#startup-banner { background: linear-gradient(135deg,#1e3a5f,#0f2744); border: 1px solid #38bdf8; border-radius: 12px; padding: 18px 20px; margin-bottom: 20px; }
#startup-banner.hidden { display: none !important; }
#startup-banner.bad { border-color: #f43f5e; background: linear-gradient(135deg,#3b1418,#1f0b0e); }
#startup-banner.good { border-color: #34d399; }
.sb-top { display: flex; align-items: center; gap: 14px; }
.sb-icon { font-size: 30px; }
.sb-text { flex: 1; min-width: 0; }
.sb-title { color: #38bdf8; font-weight: 700; font-size: 16px; }
#startup-banner.bad .sb-title { color: #fb7185; }
.sb-sub { color: #94a3b8; font-size: 13px; margin-top: 3px; word-break: break-word; }
.sb-pct { font-size: 30px; font-weight: 800; color: #f8fafc; white-space: nowrap; }
.sb-bar { width: 100%; height: 12px; background: #334155; border-radius: 6px; overflow: hidden; margin: 14px 0 12px; position: relative; }
.sb-fill { height: 100%; background: #38bdf8; border-radius: 6px; transition: width 0.8s ease; }
#startup-banner.good .sb-fill { background: #34d399; }
#startup-banner.bad .sb-fill { background: #f43f5e; }
.sb-bar.indet .sb-fill { width: 35% !important; position: absolute; animation: sbslide 1.4s ease-in-out infinite; }
@keyframes sbslide { 0% { left: -35%; } 100% { left: 100%; } }
.sb-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; }
.sb-grid div { background: rgba(15,23,42,0.55); border-radius: 8px; padding: 8px 10px; }
.sb-grid span { display: block; color: #94a3b8; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
.sb-grid b { color: #f8fafc; font-size: 15px; }
.sb-line { color: #94a3b8; font-size: 12px; margin-top: 10px; word-break: break-word; }
.sb-err { color: #fecdd3; font-size: 12px; margin-top: 8px; white-space: pre-wrap; word-break: break-word; }
.dash-link { display: inline-block; margin-top: 10px; color: #38bdf8; font-size: 13px; text-decoration: none; }
.dash-link:hover { text-decoration: underline; }
.warn { color: #f43f5e; font-weight: 700; }
.alert { background: #3b1418; border: 1px solid #f43f5e; border-radius: 10px; padding: 10px 14px; margin-bottom: 16px; color: #fecdd3; font-size: 14px; line-height: 1.7; }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>⚡ Hermes Live Stats <span id="platform" style="font-size:14px;color:#94a3b8"></span></h1>
    <div class="status-badge"><span class="status-dot"></span> <span id="limits">...</span></div>
  </header>

""" + STARTUP_BANNER + """

  <div id="alerts"></div>
  <div class="grid">
    <div class="card">
      <h2>RAM <span id="mem-pct">0%</span></h2>
      <div class="value" id="mem-val">...</div>
      <div class="subtext" id="mem-sub"></div>
      <div class="progress-bar"><div class="progress-fill" id="mem-bar" style="width:0%"></div></div>
    </div>
    <div class="card">
      <h2>Disk <span id="disk-pct">0%</span></h2>
      <div class="value" id="disk-val">...</div>
      <div class="subtext" id="disk-sub"></div>
      <div class="progress-bar"><div class="progress-fill" id="disk-bar" style="width:0%;background-color:#34d399"></div></div>
    </div>
    <div class="card">
      <h2>CPU <span id="cpu-pct">0%</span></h2>
      <div class="value" id="cpu-val">...</div>
      <div class="subtext" id="cpu-sub"></div>
      <div class="progress-bar"><div class="progress-fill" id="cpu-bar" style="width:0%;background-color:#f43f5e"></div></div>
    </div>
  </div>
  <div class="table-card">
    <h2>Container uptime &amp; top processes</h2>
    <div style="margin-bottom:16px;font-size:14px;color:#94a3b8;">Container uptime: <strong id="uptime" style="color:#f8fafc;">...</strong></div>
    <table>
      <thead><tr><th>PID</th><th>Command</th><th>RAM (MB)</th><th>RAM %</th><th>CPU % of limit</th></tr></thead>
      <tbody id="proc-table"><tr><td colspan="5" style="text-align:center;color:#64748b;">Loading...</td></tr></tbody>
    </table>
  </div>
  <div class="table-card" style="margin-top:16px;">
    <h2>Top by CPU</h2>
    <table>
      <thead><tr><th>PID</th><th>Command</th><th>CPU % of limit</th><th>RAM (MB)</th></tr></thead>
      <tbody id="cpu-table"><tr><td colspan="4" style="text-align:center;color:#64748b;">Loading...</td></tr></tbody>
    </table>
    <a class="dash-link" href="/picoclaw" target="_blank">Open PicoClaw →</a>
  </div>
  <footer id="foot">Auto-refreshing every 2 seconds</footer>
</div>
<script>
function $(id){return document.getElementById(id);}
function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + ' B';
  var u = ['KB', 'MB', 'GB', 'TB'], i = -1;
  do { n = n / 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return n.toFixed(n >= 100 ? 0 : 1) + ' ' + u[i];
}
function fmtEta(s) {
  if (s === null || s === undefined) return 'calculating...';
  if (Math.round(s) <= 1) return 'almost done';
  return fmtDur(s);
}
function fmtDur(s) {
  s = Math.round(Number(s) || 0);
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + ('0' + (s % 60)).slice(-2) + 's';
  return Math.floor(s / 3600) + 'h ' + ('0' + Math.floor((s % 3600) / 60)).slice(-2) + 'm';
}
function renderStartup(st) {
  var b = $('startup-banner');
  if (!st || st.phase === 'ready') { b.classList.add('hidden'); return; }
  b.classList.remove('hidden');
  var r = st.restore || {}, stage = r.stage || '';
  var bar = document.querySelector('.sb-bar'), fill = $('banner-bar');
  var title = st.message || 'Starting up...', sub = '', icon = '\u23f3', pct = '', cls = '', indet = false, showGrid = false;
  if (stage === 'scanning' || !stage) {
    title = stage ? 'Scanning Google Drive backup...' : (st.message || 'Starting up...');
    sub = 'Measuring how much has to be restored, so the progress and time left are accurate.'; indet = true; icon = '🔎';
  } else if (stage === 'restoring') {
    var p = Math.max(0, Math.min(100, Number(r.percent) || 0));
    title = 'Restoring from Google Drive' + (r.phases > 1 ? ' (' + r.phase + '/' + r.phases + ': ' + r.label + ')' : '');
    sub = 'History, skills and state are coming back. PicoClaw starts automatically when this reaches 100%.' + (r.attempt > 1 ? ' (attempt ' + r.attempt + ')' : '');
    pct = (r.estimated ? '~' : '') + p.toFixed(p < 10 ? 1 : 0) + '%'; fill.style.width = p + '%'; showGrid = true;
  } else if (stage === 'restored' || stage === 'packages') {
    title = stage === 'packages' ? 'Re-installing saved packages...' : 'Restore complete, finishing up...';
    sub = 'Almost there.'; pct = '100%'; fill.style.width = '100%'; cls = 'good'; icon = '\u2705';
  } else if (stage === 'failed' || stage === 'retrying') {
    title = stage === 'retrying' ? (st.message || 'Retrying restore...') : (r.message || 'Restore failed');
    sub = 'PicoClaw is not started yet.'; cls = 'bad'; icon = '\u26a0\ufe0f'; fill.style.width = (Number(r.percent) || 0) + '%';
  } else if (stage === 'clean_start') {
    title = st.message || 'Starting with a clean history'; cls = 'bad'; icon = '🧹'; indet = true;
  } else {
    sub = st.message || ''; indet = true;
  }
  if (r.stalled) { sub = 'No progress update for ' + r.age_seconds + 's - Google Drive may be slow or rclone is stuck. ' + sub; }
  b.className = cls;
  bar.className = 'sb-bar' + (indet ? ' indet' : '');
  $('banner-icon').textContent = icon;
  $('banner-title').textContent = title;
  $('banner-sub').textContent = sub;
  $('banner-pct').textContent = pct;
  $('banner-grid').style.display = showGrid ? '' : 'none';
  if (showGrid) {
    var left = Math.max(0, (r.bytes_total || 0) - (r.bytes_done || 0));
    $('sb-done').textContent = fmtBytes(r.bytes_done) + ' / ' + fmtBytes(r.bytes_total);
    $('sb-left').textContent = fmtBytes(left);
    $('sb-speed').textContent = (r.speed_bps ? fmtBytes(r.speed_bps) + '/s' : '-');
    $('sb-eta').textContent = r.estimated ? 'unknown' : fmtEta(r.eta_seconds);
  }
  var cur = (r.current || []).map(function(c){ return c.name + ' ' + c.percent + '%'; }).join('  |  ');
  $('banner-files').textContent = (showGrid ? 'Files: ' + (r.files_done || 0) + ' / ' + (r.files_total || 0) + ' | elapsed ' + fmtDur(r.elapsed_seconds) : '') + (cur && showGrid ? ' | now: ' + cur : '');
  $('banner-err').textContent = (cls === 'bad' && r.error) ? r.error : '';
}
function updateStats() {
  return fetch('/api/stats', {cache: 'no-store'}).then(function(r){return r.json();}).then(function(d) {
    var m=d.memory, k=d.disk, c=d.cpu;
    $('platform').textContent = '(' + d.platform + ')';
    $('limits').textContent = c.limit_cores + ' vCPU / ' + m.total_mb + ' MB RAM / ' + k.total_mb + ' MB disk';
    $('mem-val').textContent = m.used_mb + ' MB / ' + m.total_mb + ' MB';
    $('mem-pct').textContent = m.percent + '%';
    $('mem-bar').style.width = m.percent + '%';
    var ms = 'Free: ' + m.free_mb + ' MB • page cache (not counted): ' + m.cache_mb + ' MB';
    if (m.peak_mb) ms += ' • peak: ' + m.peak_mb + ' MB';
    $('mem-sub').textContent = ms;
    if (m.oom_kills) { var w=document.createElement('div'); w.className='warn'; w.textContent='⚠ OOM kills so far: ' + m.oom_kills + ' (a process was killed for using too much RAM)'; $('mem-sub').appendChild(w); }
    $('disk-val').textContent = k.used_mb + ' MB / ' + k.total_mb + ' MB';
    $('disk-pct').textContent = k.percent + '%';
    $('disk-bar').style.width = k.percent + '%';
    var parts = Object.keys(k.parts || {}).map(function(p){return p + ' ' + k.parts[p] + ' MB';}).join(' • ');
    $('disk-sub').textContent = 'Free: ' + k.free_mb + ' MB' + (parts ? ' • ' + parts : '');
    $('cpu-val').textContent = c.percent + '% of ' + c.limit_cores + ' vCPU';
    $('cpu-pct').textContent = c.percent + '%';
    $('cpu-bar').style.width = c.percent + '%';
    $('cpu-sub').textContent = 'Using ' + c.cores_used + ' vCPU • avg 1m/5m/15m: ' + c.avg['1m'] + '% / ' + c.avg['5m'] + '% / ' + c.avg['15m'] + '% • throttled: ' + c.throttled_percent + '%';
    $('uptime').textContent = d.uptime;
    $('foot').textContent = 'Auto-refreshing every 2 seconds (paused while this tab is hidden) • only the container plan allotment, not the host • RAM/CPU from ' + m.source + ' (limits: ' + m.limit_source + '; ' + c.limit_source + ') • disk limit: ' + k.limit_source;
    renderStartup(d.startup);
    var tbody = $('proc-table'); tbody.innerHTML = '';
    d.processes.forEach(function(p) {
      var tr = document.createElement('tr');
      [p.pid, p.command, p.rss_mb, p.mem + '%', p.cpu + '%'].forEach(function(v){ var td=document.createElement('td'); td.textContent=v; tr.appendChild(td); });
      tbody.appendChild(tr);
    });
    var ct = $('cpu-table'); ct.innerHTML = '';
    (d.processes_cpu || []).forEach(function(p) {
      var tr = document.createElement('tr');
      [p.pid, p.command, p.cpu + '%', p.rss_mb].forEach(function(v){ var td=document.createElement('td'); td.textContent=v; tr.appendChild(td); });
      ct.appendChild(tr);
    });
    var al = $('alerts'); al.innerHTML = '';
    var alerts = (d.alerts || []).slice();
    if (d.startup && d.startup.clean_start) { alerts.push('Drive restore failed, so this run started with a CLEAN history. Drive sync is OFF for this run (the old backup on Drive is safe). Fix Drive and restart to get the history back.'); }
    if (alerts.length) {
      var box = document.createElement('div'); box.className = 'alert';
      alerts.forEach(function(a, i){ if (i) box.appendChild(document.createElement('br')); box.appendChild(document.createTextNode('\u26a0 ' + a)); });
      al.appendChild(box);
    }
    document.title = (alerts.length ? '\u26a0 ' : '') + 'Hermes Live Stats';
  }).catch(function(err) { console.error('Stats fetch error:', err); });
}
function loop() {
  if (document.hidden) { setTimeout(loop, 2000); return; }      // hidden tab: no requests -> server goes idle
  updateStats().then(function(){ setTimeout(loop, 2000); }, function(){ setTimeout(loop, 2000); });
}
loop();
</script>
</body>
</html>
"""

SILENT_ROUTES = {'/', '/api/stats', '/picoclaw/status.json'}


def _is_picoclaw_path(path):
    return path == PICOCLAW_PREFIX or path.startswith((PICOCLAW_PREFIX + "/", PICOCLAW_PREFIX + "?"))


_ABS_LINK_RE = re.compile(r'''(\s(?:src|href|action)=["'])/(?!/|picoclaw/)''', re.I)


def rewrite_html_links(html):
    """Best effort: make root-absolute links of the proxied UI work under /picoclaw/ (JS-built URLs are not touched)."""
    return _ABS_LINK_RE.sub(lambda m: m.group(1) + PICOCLAW_PREFIX + "/", html)


class PingHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        request_line = args[0] if args else ''
        for route in SILENT_ROUTES:
            if f'GET {route} ' in request_line or f'HEAD {route} ' in request_line:
                return
        super().log_message(format, *args)

    # ── helpers ──
    def _send(self, code, ctype, body, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self):
        if not UI_BASIC_AUTH:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.lower().startswith("basic "):
            try:
                if base64.b64decode(hdr[6:]).decode("utf-8") == UI_BASIC_AUTH:
                    return True
            except Exception:  # noqa: BLE001
                pass
        self._send(401, "text/plain", "Authentication required", {"WWW-Authenticate": 'Basic realm="PicoClaw"'})
        return False

    # ── /picoclaw ──
    def _picoclaw(self, method):
        if not self._authorized():
            return
        path = self.path.split("?", 1)[0]
        query = self.path[len(path):]
        if path == PICOCLAW_PREFIX + "/status.json":
            self._send(200, "application/json", json.dumps(picoclaw_status()))
            return
        ui_up = port_open(PICOCLAW_UI_PORT)
        if not ui_up:
            if path in (PICOCLAW_PREFIX, PICOCLAW_PREFIX + "/") and method in ("GET", "HEAD"):
                self._send(200, "text/html; charset=utf-8", STATUS_PAGE)
            else:
                self._send(503, "text/plain", f"PicoClaw web UI is not running on port {PICOCLAW_UI_PORT}")
            return
        if "websocket" in (self.headers.get("Upgrade", "") or "").lower():
            self._send(501, "text/plain", "WebSocket is not supported through this route; open the UI via Tailscale "
                                          f"(http://<hostname>:{PICOCLAW_UI_PORT}/).")
            return
        upstream_path = path[len(PICOCLAW_PREFIX):] or "/"
        url = f"http://{PICOCLAW_HOST}:{PICOCLAW_UI_PORT}{upstream_path}{query}"
        body = None
        length = self.headers.get("Content-Length")
        if length:
            try:
                body = self.rfile.read(int(length))
            except Exception:  # noqa: BLE001
                body = None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._relay(resp.status, resp.getheaders(), resp.read())
        except urllib.error.HTTPError as e:
            data = b""
            try:
                data = e.read()
            except Exception:  # noqa: BLE001
                pass
            self._relay(e.code, list(e.headers.items()) if e.headers else [], data)
        except Exception as e:  # noqa: BLE001
            self._send(502, "text/plain", f"PicoClaw UI proxy error: {e}")

    def _relay(self, status, headers, data):
        ctype = ""
        out = []
        for k, v in headers:
            if k.lower() in HOP_BY_HOP_HEADERS:
                continue
            if k.lower() == "content-type":
                ctype = v
            out.append((k, v))
        if "text/html" in ctype.lower():
            data = rewrite_html_links(data.decode("utf-8", "replace")).encode("utf-8")
        self.send_response(status)
        for k, v in out:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ── routes ──
    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if _is_picoclaw_path(self.path):
            self._picoclaw("GET")
        elif route == '/memory':
            self._send(200, 'text/html; charset=utf-8', HTML_PAGE)
        elif route == '/api/stats':
            self._send(200, 'application/json', json.dumps(get_stats()), {"Cache-Control": "no-store"})
        elif route == '/startup-ready':
            # start.sh calls this when the restore is finished. Only from this machine: a stranger must not be able
            # to hide the "restoring" banner by opening the URL.
            if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                self._send(403, 'text/plain', b"forbidden")
                return
            set_startup_ready()
            self._send(200, 'text/plain', b"OK")
        else:
            self._send(200, 'text/html', b"Hermes Agent is Alive and Running!")

    def _write_method(self, method):
        if _is_picoclaw_path(self.path):
            self._picoclaw(method)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        self._write_method("POST")

    def do_PUT(self):
        self._write_method("PUT")

    def do_DELETE(self):
        self._write_method("DELETE")

    def do_PATCH(self):
        self._write_method("PATCH")

    def do_HEAD(self):
        if _is_picoclaw_path(self.path):
            self._picoclaw("HEAD")
        else:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()


if __name__ == "__main__":
    try:    # file-manager API for the tgstorage "Machines" page: 127.0.0.1 only, reaches the tailnet via tailscale serve (see start.sh)
        import fsd; fsd.start()
    except Exception as e:
        print(f"fsd: not started: {e}", flush=True)
    port = int(os.environ.get("PORT", 10000))
    # Threaded: a slow request (UI proxy, many pollers) must never block the platform's health check on "/".
    http.server.ThreadingHTTPServer.daemon_threads = True
    httpd = http.server.ThreadingHTTPServer(('0.0.0.0', port), PingHandler)
    print(f"Ping server running on port {port} with /memory stats page and /picoclaw route...")
    httpd.serve_forever()
