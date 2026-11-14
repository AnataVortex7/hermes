#!/usr/bin/env python3
"""Generate ~/.picoclaw/config.json from environment variables AND ensure:
1. Full unrestricted command/script/cron execution permissions for Telegram ID 1193564058
2. 100% persistent Python/pip, npm, and apt packages across restarts (saved directly inside
   ~/.picoclaw/workspace/.installed/ which is synced to Google Drive)
3. 360-second (6-minute) request timeout + local Stream-to-JSON bridge on 127.0.0.1:18799
4. Instant Telegram Startup Notification to 1193564058 reporting:
   - Service Restart Timestamp (IST)
   - Google Drive (rclone) Backup Restore Status + file/cron counts
   - Restored Python/apt/npm packages
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_TELEGRAM_ID = "1193564058"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

home = Path(os.environ.get("PICOCLAW_HOME", Path.home() / ".picoclaw"))
home.mkdir(parents=True, exist_ok=True)
ws_dir = home / "workspace"
(ws_dir / "cron").mkdir(parents=True, exist_ok=True)
cfg_path = home / "config.json"

# ---------------------------------------------------------------------------
# 0. CHECK GOOGLE DRIVE (RCLONE) RESTORE STATUS BEFORE MODIFYING WORKSPACE
# ---------------------------------------------------------------------------
rclone_conf_b64 = os.environ.get("RCLONE_CONFIG_BASE64", "").strip()
rclone_conf_file = Path.home() / ".config" / "rclone" / "rclone.conf"
has_rclone_cfg = bool(rclone_conf_b64) or (rclone_conf_file.exists() and rclone_conf_file.stat().st_size > 0)

# Count files already restored into ~/.picoclaw/workspace by start.sh (excluding our internal folders)
existing_ws_files = []
if ws_dir.exists():
    for p in ws_dir.rglob("*"):
        if p.is_file() and ".installed" not in p.parts:
            existing_ws_files.append(p)

# If rclone is configured but start.sh didn't restore any files yet, run a safety restore pass
rclone_pulled_now = False
rclone_err_msg = ""
if has_rclone_cfg and len(existing_ws_files) == 0 and shutil.which("rclone"):
    try:
        res = subprocess.run(
            ["rclone", "copy", "drive:picow_clow_backup/picoclaw", str(home), "--exclude", "config.json"],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if res.returncode == 0:
            rclone_pulled_now = True
            existing_ws_files = [
                p for p in ws_dir.rglob("*") if p.is_file() and ".installed" not in p.parts
            ]
        else:
            rclone_err_msg = (res.stderr or res.stdout or "").strip()[:180]
    except Exception as e:
        rclone_err_msg = str(e)[:180]

# Count restored cron jobs if jobs.json exists
cron_jobs_count = 0
cron_jobs_file = ws_dir / "cron" / "jobs.json"
if cron_jobs_file.exists():
    try:
        cdata = json.loads(cron_jobs_file.read_text(encoding="utf-8"))
        if isinstance(cdata, list):
            cron_jobs_count = len(cdata)
        elif isinstance(cdata, dict):
            cron_jobs_count = len(cdata.get("jobs") or cdata)
    except Exception:
        pass

if not has_rclone_cfg:
    rclone_status_line = "⚠️ Not Configured (RCLONE_CONFIG_BASE64 नाही)"
elif rclone_err_msg:
    rclone_status_line = f"❌ Error ({rclone_err_msg})"
elif len(existing_ws_files) > 0:
    rclone_status_line = f"✅ Restored ({len(existing_ws_files)} workspace files, {cron_jobs_count} cron jobs)"
else:
    rclone_status_line = "✅ Connected (Fresh workspace / 0 prior files)"


# ---------------------------------------------------------------------------
# 1. PERSISTENT PACKAGE STORAGE & AUTO-RESTORE (Survives every server restart!)
# ---------------------------------------------------------------------------
pkg_dir = ws_dir / ".installed"
pkg_dir.mkdir(parents=True, exist_ok=True)

legacy_dir = Path("/root/.picow_clow/installed")
try:
    legacy_dir.parent.mkdir(parents=True, exist_ok=True)
    if legacy_dir.exists() and not legacy_dir.is_symlink():
        for f in legacy_dir.glob("*.list"):
            target_f = pkg_dir / f.name
            existing = set(target_f.read_text().splitlines()) if target_f.exists() else set()
            existing.update(x.strip() for x in f.read_text().splitlines() if x.strip())
            target_f.write_text("\n".join(sorted(existing)) + "\n")
        shutil.rmtree(legacy_dir, ignore_errors=True)
    if not legacy_dir.exists():
        legacy_dir.symlink_to(pkg_dir)
except Exception as e:
    print(f">> [picoclaw] warning linking legacy pkg dir: {e}", file=sys.stderr)

pip_list_file = pkg_dir / "pip.list"
apt_list_file = pkg_dir / "apt.list"
npm_list_file = pkg_dir / "npm.list"

default_pip_pkgs = {"requests", "beautifulsoup4", "lxml"}
existing_pip = set()
if pip_list_file.exists():
    existing_pip = {x.strip() for x in pip_list_file.read_text().splitlines() if x.strip() and not x.startswith("#")}
all_pip_pkgs = sorted(existing_pip | default_pip_pkgs)
pip_list_file.write_text("\n".join(all_pip_pkgs) + "\n", encoding="utf-8")

if apt_list_file.exists():
    apt_pkgs = [x.strip() for x in apt_list_file.read_text().splitlines() if x.strip() and not x.startswith("#")]
    if apt_pkgs:
        print(f">> [picoclaw] Restoring apt packages: {' '.join(apt_pkgs)}")
        subprocess.run(["/usr/bin/apt-get", "update", "-qq"], check=False)
        subprocess.run(["/usr/bin/apt-get", "install", "-y", "--no-install-recommends"] + apt_pkgs, check=False)

pip_restored_ok = False
if all_pip_pkgs:
    print(f">> [picoclaw] Restoring pip packages: {' '.join(all_pip_pkgs)}")
    pip_res = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--break-system-packages", "--no-cache-dir"] + all_pip_pkgs,
        check=False,
    )
    pip_restored_ok = (pip_res.returncode == 0)

if npm_list_file.exists():
    npm_pkgs = [x.strip() for x in npm_list_file.read_text().splitlines() if x.strip() and not x.startswith("#")]
    if npm_pkgs and shutil.which("npm"):
        print(f">> [picoclaw] Restoring npm packages: {' '.join(npm_pkgs)}")
        subprocess.run(["/usr/bin/npm", "install", "-g"] + npm_pkgs, check=False)

pip_recorder_code = f'''#!/usr/bin/env python3
import sys, os
from pathlib import Path

def _record_pip_args(argv):
    try:
        if "install" not in argv:
            return
        idx = argv.index("install")
        pkgs = []
        skip_next = False
        for arg in argv[idx + 1:]:
            if skip_next:
                skip_next = False
                continue
            if arg in ("-r", "--requirement", "-c", "--constraint", "-i", "--index-url",
                       "--extra-index-url", "-f", "--find-links", "--target", "-t", "--prefix"):
                skip_next = True
                continue
            if arg.startswith("-"):
                continue
            pkgs.append(arg.strip())
        if pkgs:
            pfile = Path({json.dumps(str(pip_list_file))})
            pfile.parent.mkdir(parents=True, exist_ok=True)
            cur = set(x.strip() for x in pfile.read_text().splitlines() if x.strip()) if pfile.exists() else set()
            cur.update(pkgs)
            pfile.write_text("\\n".join(sorted(cur)) + "\\n", encoding="utf-8")
    except Exception:
        pass

if len(sys.argv) >= 2 and sys.argv[0].endswith(("pip", "pip3", "__main__.py")):
    _record_pip_args(sys.argv)
'''
try:
    import site
    site_dirs = site.getsitepackages()
    if site_dirs:
        sc_path = Path(site_dirs[0]) / "usercustomize.py"
        sc_path.write_text(pip_recorder_code, encoding="utf-8")
except Exception:
    pass


# ---------------------------------------------------------------------------
# 2. PICOCLAW CONFIG + LOCAL STREAM-TO-JSON BRIDGE + TELEGRAM ID 1193564058
# ---------------------------------------------------------------------------
token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
raw_allow = [x.strip() for x in os.environ.get("TELEGRAM_ALLOW_FROM", DEFAULT_TELEGRAM_ID).split(",") if x.strip()]
if DEFAULT_TELEGRAM_ID not in raw_allow:
    raw_allow.insert(0, DEFAULT_TELEGRAM_ID)
allow = list(dict.fromkeys(raw_allow))

api_key = os.environ.get("LLM_API_KEY", "").strip()
req_timeout = int(os.environ.get("LLM_REQUEST_TIMEOUT", "360").strip() or "360")

missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("LLM_API_KEY", api_key)) if not v]
if missing:
    print(f">> [picoclaw] missing env: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)

try:
    subprocess.run(["picoclaw", "update"], timeout=45, check=False)
except Exception:
    pass

local_proxy_port = int(os.environ.get("LOCAL_LLM_PROXY_PORT", "18799"))
upstream_base = os.environ.get("LLM_API_BASE", "").strip().rstrip("/")
effective_api_base = upstream_base

if upstream_base:
    bridge_script = home / "stream_bridge.py"
    bridge_code = f'''#!/usr/bin/env python3
import json, os, sys, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests

UPSTREAM_BASE = {json.dumps(upstream_base)}
TIMEOUT_SECS = {req_timeout}

def _assemble_sse_to_json(resp, fallback_model):
    content_parts = []
    reasoning_parts = []
    tool_calls_by_idx = {{}}
    resp_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    model = fallback_model
    finish_reason = "stop"
    usage = None
    raw_lines = []

    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        line = raw_line.strip() if isinstance(raw_line, str) else raw_line.decode("utf-8", "replace").strip()
        raw_lines.append(line)
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except Exception:
            continue
        if not isinstance(chunk, dict):
            continue
        if "error" in chunk and not chunk.get("choices"):
            return chunk
        if chunk.get("id"):
            resp_id = chunk["id"]
        if chunk.get("created"):
            created = chunk["created"]
        if chunk.get("model"):
            model = chunk["model"]
        if chunk.get("usage"):
            usage = chunk["usage"]
        for ch in chunk.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]
            delta = ch.get("delta") if isinstance(ch.get("delta"), dict) else ch.get("message")
            if not isinstance(delta, dict):
                continue
            if delta.get("content"):
                content_parts.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index", 0)
                entry = tool_calls_by_idx.setdefault(idx, {{
                    "id": tc.get("id") or f"call_{{uuid.uuid4().hex[:12]}}",
                    "type": tc.get("type") or "function",
                    "function": {{"name": "", "arguments": ""}},
                }})
                if tc.get("id"):
                    entry["id"] = tc["id"]
                if tc.get("extra_content"):
                    entry["extra_content"] = tc["extra_content"]
                fn = tc.get("function") or {{}}
                if fn.get("name"):
                    entry["function"]["name"] += fn["name"]
                if fn.get("arguments") is not None:
                    arg_val = fn["arguments"]
                    if not isinstance(arg_val, str):
                        arg_val = json.dumps(arg_val)
                    entry["function"]["arguments"] += arg_val

    if not content_parts and not tool_calls_by_idx and raw_lines:
        joined = "\\n".join(raw_lines).strip()
        if joined.startswith("{{"):
            try:
                return json.loads(joined)
            except Exception:
                pass

    msg = {{"role": "assistant", "content": "".join(content_parts)}}
    if reasoning_parts:
        msg["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls_by_idx:
        msg["tool_calls"] = [tool_calls_by_idx[k] for k in sorted(tool_calls_by_idx.keys())]
        if finish_reason == "stop":
            finish_reason = "tool_calls"

    out = {{
        "id": resp_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{{
            "index": 0,
            "message": msg,
            "finish_reason": finish_reason,
        }}],
    }}
    if usage:
        out["usage"] = usage
    return out


class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _forward(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length > 0 else b""

        req_path = self.path
        if UPSTREAM_BASE.endswith("/v1") and req_path.startswith("/v1/"):
            req_path = req_path[3:]
        target_url = UPSTREAM_BASE + ("/" if not req_path.startswith("/") else "") + req_path

        fwd_headers = {{
            k: v for k, v in self.headers.items()
            if k.lower() not in ("host", "content-length", "transfer-encoding", "connection")
        }}

        is_chat_post = (method == "POST" and ("/chat/completions" in req_path))
        client_wanted_stream = False
        fallback_model = "default"

        if is_chat_post and raw_body:
            try:
                body_json = json.loads(raw_body.decode("utf-8", "replace"))
                if isinstance(body_json, dict):
                    client_wanted_stream = bool(body_json.get("stream"))
                    fallback_model = body_json.get("model") or "default"
                    if not client_wanted_stream:
                        body_json["stream"] = True
                        raw_body = json.dumps(body_json).encode("utf-8")
                        fwd_headers["Accept"] = "text/event-stream"
            except Exception:
                pass

        try:
            up = requests.request(
                method,
                target_url,
                data=raw_body if raw_body else None,
                headers=fwd_headers,
                stream=True,
                timeout=(15, TIMEOUT_SECS),
            )
        except Exception as e:
            err_bytes = json.dumps({{"error": {{"message": str(e), "type": "bridge_error"}}}}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err_bytes)))
            self.end_headers()
            self.wfile.write(err_bytes)
            return

        ctype = (up.headers.get("Content-Type") or "").lower()
        if is_chat_post and not client_wanted_stream and up.status_code == 200 and "text/event-stream" in ctype:
            assembled = _assemble_sse_to_json(up, fallback_model)
            out_bytes = json.dumps(assembled).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
            return

        out_bytes = up.content
        self.send_response(up.status_code)
        self.send_header("Content-Type", up.headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(out_bytes)))
        self.end_headers()
        self.wfile.write(out_bytes)

    def do_GET(self):
        self._forward("GET")

    def do_POST(self):
        self._forward("POST")


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", {local_proxy_port}), BridgeHandler)
    srv.serve_forever()
'''
    bridge_script.write_text(bridge_code, encoding="utf-8")
    bridge_script.chmod(0o755)
    subprocess.run(["pkill", "-f", str(bridge_script)], check=False)
    subprocess.Popen(
        [sys.executable, str(bridge_script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(0.4)
    effective_api_base = f"http://127.0.0.1:{local_proxy_port}/v1"
    print(f">> [picoclaw] started local Stream-to-JSON bridge on {effective_api_base} -> {upstream_base}")

raw_model = os.environ.get("LLM_MODEL", "").strip() or "openai/gpt-5.2"
model_name = os.environ.get("LLM_MODEL_NAME", "default")
model = {
    "model_name": model_name,
    "model": raw_model,
    "api_key": api_key,
    "api_keys": [api_key],
    "request_timeout": req_timeout,
    "api_base": effective_api_base,
}

cfg = {}
if cfg_path.exists():
    try:
        cfg = json.loads(cfg_path.read_text())
    except Exception:
        cfg = {}

cfg["version"] = 2

defaults = cfg.setdefault("agents", {}).setdefault("defaults", {})
defaults["workspace"] = str(ws_dir)
defaults["restrict_to_workspace"] = False
defaults["allow_read_outside_workspace"] = True
defaults["model_name"] = model_name
defaults["request_timeout"] = req_timeout
defaults["max_tokens"] = 32768
defaults["max_tool_iterations"] = 50

allowed_remotes = list(dict.fromkeys(
    ["*", "telegram", f"telegram:{DEFAULT_TELEGRAM_ID}"] + [f"telegram:{uid}" for uid in allow]
))
tools = cfg.setdefault("tools", {})
tools["allow_read_paths"] = ["/"]
tools["allow_write_paths"] = ["/"]
tools["exec"] = {
    "enabled": True,
    "allow_remote": True,
    "enable_deny_patterns": False,
    "custom_deny_patterns": [],
    "custom_allow_patterns": [".*"],
    "timeout_seconds": 360,
}
tools["cron"] = {
    "enabled": True,
    "allow_command": True,
    "command_allowed_remotes": allowed_remotes,
    "exec_timeout_minutes": 10,
}

cfg["model_list"] = [model]
cfg.setdefault("channels", {})["telegram"] = {
    "enabled": True,
    "token": token,
    "allow_from": allow,
}

cfg_path.write_text(json.dumps(cfg, indent=2))
cfg_path.chmod(0o600)

sec_path = home / ".security.yml"
sec_content = (
    f"model_list:\n"
    f"  {model_name}:0:\n"
    f"    api_keys:\n"
    f"      - {json.dumps(api_key)}\n"
    f"channels:\n"
    f"  telegram:\n"
    f"    token: {json.dumps(token)}\n"
)
sec_path.write_text(sec_content, encoding="utf-8")
sec_path.chmod(0o600)


# ---------------------------------------------------------------------------
# 3. SEND STARTUP / RESTART NOTIFICATION TO TELEGRAM BOT
# ---------------------------------------------------------------------------
def _send_startup_telegram_alert():
    now_ist = datetime.datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p IST")
    pip_summary = ", ".join(all_pip_pkgs[:8]) + (f" (+{len(all_pip_pkgs)-8} more)" if len(all_pip_pkgs) > 8 else "")
    pkg_status = f"✅ Restored ({pip_summary})" if pip_restored_ok else f"⚠️ Check logs ({pip_summary})"

    msg_text = (
        f"🔄 *PicoClaw / Hermes Service Restarted!*\n\n"
        f"🕒 *वेळ:* `{now_ist}`\n"
        f"☁️ *Google Drive (rclone):* {rclone_status_line}\n"
        f"📦 *Python Packages:* {pkg_status}\n"
        f"⏱️ *LLM Timeout:* `{req_timeout}s` (Stream Bridge: `ON`)\n"
        f"🔓 *Cron & Shell Exec:* `Full Access Enabled`"
    )

    for chat_id in allow:
        try:
            api_url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = urllib.parse.urlencode({
                "chat_id": chat_id,
                "text": msg_text,
                "parse_mode": "Markdown",
            }).encode("utf-8")
            req = urllib.request.Request(api_url, data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            print(f">> [picoclaw] sent restart notification to Telegram ID {chat_id}")
        except Exception as e:
            print(f">> [picoclaw] failed to send Telegram startup alert to {chat_id}: {e}", file=sys.stderr)

_send_startup_telegram_alert()

print(f">> [picoclaw] wrote {cfg_path} & {sec_path} (Telegram ID {DEFAULT_TELEGRAM_ID} + startup alert sent)")
print(f">> [picoclaw] model={model['model']} alias={model_name} upstream_host={urlparse(upstream_base).netloc or '(none)'}")
