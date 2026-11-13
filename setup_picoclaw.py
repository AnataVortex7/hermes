import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

STREAM_PROXY_CODE = r'''import http.client
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_BASE = os.environ.get("PICOCLAW_UPSTREAM_API_BASE", "https://api.openai.com/v1").rstrip("/")
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("LOCAL_LLM_PROXY_PORT", "18799"))
TIMEOUT = int(os.environ.get("LLM_REQUEST_TIMEOUT", "360"))


def build_upstream_url(req_path: str) -> str:
    parsed_base = urllib.parse.urlparse(UPSTREAM_BASE)
    base_path = parsed_base.path.rstrip("/")
    if base_path.endswith("/v1") and req_path.startswith("/v1/"):
        req_path = req_path[3:]
    elif base_path.endswith("/v1") and req_path == "/v1":
        req_path = ""
    return f"{UPSTREAM_BASE}{req_path}"


def sse_to_chat_completion(raw_lines):
    content_parts = []
    reasoning_parts = []
    tool_calls = {}
    model = ""
    comp_id = "chatcmpl-proxy"
    finish_reason = "stop"
    usage = None

    for raw in raw_lines:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        if chunk.get("id"):
            comp_id = chunk["id"]
        if chunk.get("model"):
            model = chunk["model"]
        if chunk.get("usage"):
            usage = chunk["usage"]

        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                entry = tool_calls.setdefault(
                    idx,
                    {
                        "id": tc.get("id") or f"call_{idx}",
                        "type": tc.get("type") or "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if tc.get("id"):
                    entry["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    entry["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    entry["function"]["arguments"] += fn["arguments"]

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]
        if finish_reason == "stop":
            finish_reason = "tool_calls"

    result = {
        "id": comp_id,
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage:
        result["usage"] = usage
    return result


class LiveStreamProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[stream_proxy] {fmt % args}\n")
        sys.stderr.flush()

    def _handle_request(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length > 0 else b""

        client_wants_stream = False
        if body and "json" in (self.headers.get("Content-Type") or "").lower():
            try:
                payload = json.loads(body.decode("utf-8"))
                if isinstance(payload, dict):
                    client_wants_stream = bool(payload.get("stream", False))
                    if self.path.rstrip("/").endswith("/chat/completions"):
                        payload["stream"] = True
                        body = json.dumps(payload).encode("utf-8")
            except Exception:
                pass

        target_url = build_upstream_url(self.path)
        parsed = urllib.parse.urlparse(target_url)
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        upstream_path = parsed.path + (f"?{parsed.query}" if parsed.query else "")

        fwd_headers = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in ("host", "content-length", "accept-encoding", "connection"):
                continue
            fwd_headers[k] = v
        if body:
            fwd_headers["Content-Length"] = str(len(body))

        conn = None
        try:
            conn = conn_cls(parsed.hostname, port, timeout=TIMEOUT)
            conn.request(self.command, upstream_path, body=body if body else None, headers=fwd_headers)
            resp = conn.getresponse()
            ctype = (resp.getheader("Content-Type") or "").lower()

            if resp.status == 200 and "text/event-stream" in ctype:
                if client_wants_stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while True:
                        line = resp.readline()
                        if not line:
                            break
                        self.wfile.write(line)
                        self.wfile.flush()
                else:
                    lines = []
                    while True:
                        line = resp.readline()
                        if not line:
                            break
                        lines.append(line)
                    out = json.dumps(sse_to_chat_completion(lines)).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(out)
            else:
                raw = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.getheader("Content-Type") or "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)
        except Exception as exc:
            err = json.dumps({"error": {"message": f"stream_proxy error: {exc}"}}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(err)
        finally:
            if conn:
                conn.close()

    def do_GET(self):
        self._handle_request()

    def do_POST(self):
        self._handle_request()


if __name__ == "__main__":
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), LiveStreamProxyHandler)
    print(f"Live stream proxy listening on http://{LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM_BASE}", flush=True)
    server.serve_forever()
'''


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"Error: required environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return val


def wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> None:
    picoclaw_home = Path(
        os.environ.get("PICOCLAW_HOME", os.path.expanduser("~/.picoclaw"))
    ).expanduser()
    workspace = os.environ.get(
        "PICOCLAW_WORKSPACE", str(picoclaw_home / "workspace")
    ).strip()

    picoclaw_home.mkdir(parents=True, exist_ok=True)
    Path(workspace).mkdir(parents=True, exist_ok=True)

    # Remove stale PID files restored from Drive backup so gateway doesn't fail singleton check
    for pid_file in picoclaw_home.rglob("*.pid"):
        try:
            pid_file.unlink()
            print(f"Removed stale PID file: {pid_file}")
        except OSError as exc:
            print(f"Warning: could not remove {pid_file}: {exc}", file=sys.stderr)

    tg_token = require_env("TELEGRAM_BOT_TOKEN")
    allow_from_raw = require_env("TELEGRAM_ALLOW_FROM")
    allow_from = [u.strip() for u in allow_from_raw.split(",") if u.strip()]
    if not allow_from:
        print("Error: TELEGRAM_ALLOW_FROM must contain at least one user ID.", file=sys.stderr)
        sys.exit(1)

    api_key = require_env("LLM_API_KEY")
    model_name = os.environ.get("LLM_MODEL_NAME", "default").strip() or "default"
    model = os.environ.get("LLM_MODEL", "openai/gpt-5.2").strip() or "openai/gpt-5.2"
    upstream_base = os.environ.get("LLM_API_BASE", "").strip() or "https://api.openai.com/v1"

    try:
        request_timeout = int(os.environ.get("LLM_REQUEST_TIMEOUT", "360").strip())
    except ValueError:
        request_timeout = 360

    try:
        proxy_port = int(os.environ.get("LOCAL_LLM_PROXY_PORT", "18799").strip())
    except ValueError:
        proxy_port = 18799

    proxy_script = picoclaw_home / "stream_proxy.py"
    proxy_script.write_text(STREAM_PROXY_CODE, encoding="utf-8")
    proxy_log = open("/tmp/stream_proxy.log", "a", encoding="utf-8")

    proxy_env = os.environ.copy()
    proxy_env["PICOCLAW_UPSTREAM_API_BASE"] = upstream_base
    proxy_env["LOCAL_LLM_PROXY_PORT"] = str(proxy_port)
    proxy_env["LLM_REQUEST_TIMEOUT"] = str(request_timeout)

    subprocess.Popen(
        [sys.executable, str(proxy_script)],
        env=proxy_env,
        stdout=proxy_log,
        stderr=proxy_log,
        start_new_session=True,
    )

    if not wait_for_port("127.0.0.1", proxy_port):
        print("Error: local stream proxy failed to start. Check /tmp/stream_proxy.log", file=sys.stderr)
        sys.exit(1)

    model_entry = {
        "model_name": model_name,
        "model": model,
        "api_key": api_key,
        "api_base": f"http://127.0.0.1:{proxy_port}/v1",
        "request_timeout": request_timeout,
    }

    cfg = {
        "agents": {
            "defaults": {
                "workspace": workspace,
                "model_name": model_name,
                "restrict_to_workspace": False,
            }
        },
        "model_list": [model_entry],
        "channels": {
            "telegram": {
                "enabled": True,
                "token": tg_token,
                "allow_from": allow_from,
                "streaming": {
                    "enabled": True,
                },
            }
        },
        "tools": {
            "exec": {
                "enable_deny_patterns": False,
                "allow_remote": True,
            },
            "cron": {
                "allow_command": True,
            },
        },
    }

    config_path = picoclaw_home / "config.json"
    config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"Generated unrestricted PicoClaw config at {config_path} (live stream proxy -> {upstream_base})")


if __name__ == "__main__":
    main()