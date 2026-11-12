#!/usr/bin/env python3
"""Generate ~/.picoclaw/config.json from environment variables AND launch a local
streaming-to-JSON keepalive proxy on 127.0.0.1:18799.

Required env:
  TELEGRAM_BOT_TOKEN   token from @BotFather
  TELEGRAM_ALLOW_FROM  comma separated numeric Telegram user IDs (from @userinfobot)
  LLM_API_KEY          API key for your LLM provider

Optional env:
  LLM_MODEL            provider/model string      (default: openai/gpt-5.2)
  LLM_MODEL_NAME       alias used by the agent    (default: default)
  LLM_API_BASE         custom OpenAI-compatible base URL
  LLM_REQUEST_TIMEOUT  HTTP timeout in seconds    (default: 360 -> 6 minutes)
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

home = Path(os.environ.get("PICOCLAW_HOME", Path.home() / ".picoclaw"))
home.mkdir(parents=True, exist_ok=True)
cfg_path = home / "config.json"

token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
allow = [x.strip() for x in os.environ.get("TELEGRAM_ALLOW_FROM", "").split(",") if x.strip()]
api_key = os.environ.get("LLM_API_KEY", "").strip()
req_timeout = int(os.environ.get("LLM_REQUEST_TIMEOUT", "360").strip() or "360")

missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("LLM_API_KEY", api_key)) if not v]
if missing:
    print(f">> [picoclaw] missing env: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)

if not allow:
    print(">> [picoclaw] TELEGRAM_ALLOW_FROM is empty - refusing to start an open bot.", file=sys.stderr)
    sys.exit(1)

# Write and start the local Stream-to-JSON bridge on 127.0.0.1:18799 if LLM_API_BASE is set
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
    """Reads an SSE stream (`data: ...`) from upstream and reconstructs a standard
    non-streaming OpenAI chat.completion JSON response for PicoClaw."""
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
cfg.setdefault("agents", {}).setdefault("defaults", {})
cfg["agents"]["defaults"]["workspace"] = str(home / "workspace")
cfg["agents"]["defaults"]["model_name"] = model_name
cfg["agents"]["defaults"]["request_timeout"] = req_timeout
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

print(f">> [picoclaw] wrote {cfg_path} & {sec_path} (telegram allow_from: {len(allow)} user(s), request_timeout: {req_timeout}s)")
print(f">> [picoclaw] model={model['model']} alias={model_name} upstream_host={urlparse(upstream_base).netloc or '(none)'}")