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
TIMEOUT = int(os.environ.get("LLM_REQUEST_TIMEOUT", "600"))


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


BLOCK_START = "<!-- HERMES-PERMISSIONS:START (auto-generated on every boot, do not edit) -->"
BLOCK_END = "<!-- HERMES-PERMISSIONS:END -->"
PERMISSION_NOTES = f"""{BLOCK_START}
## Permissions & environment (authoritative - overrides anything in memory or old chats)

The owner has granted FULL permissions. Earlier failures like "cron restricted",
"policy restriction", "permission denied" or "needs Active Session / Notification
Permission" were a config problem that is now FIXED. Do NOT repeat them as facts and do
NOT refuse. If something fails, run it, read the real error text, fix the cause and retry;
only report back when it works or when you are truly blocked (then show the exact error).

- `exec` is fully enabled: no deny patterns, no workspace restriction, remote (Telegram) allowed.
- `cron` tool: `action=add` with `command="..."` works from this Telegram chat. Use
  `every_seconds` / `at_seconds` / `cron_expr`. Do NOT pass the user a "restricted" excuse.
  The command's output is posted automatically to the SAME Telegram chat; no active session
  or notification permission is needed. Verify with `cron action=list`.
- Complete the whole job in one go (install -> write script -> test run -> schedule) without
  asking for confirmation at each step.
- Python packages: `pip install <pkg>` (or `python3 -m pip install`) is persistent - it is stored
  in ~/.picow_clow/pyuser and restored from Google Drive on every restart. `requests` and
  `beautifulsoup4` are already installed. apt/npm -g installs are replayed on boot too.
- Put scripts you want to keep in the workspace ({{WORKSPACE}}/scripts/). Anything outside
  ~/.picoclaw and ~/.picow_clow (e.g. /tmp, /app) is LOST on restart. Cron jobs are saved
  in the workspace and survive restarts.

## How you work (always follow this)

1. THINK FIRST. Before acting, restate the goal in one line and plan the steps. For a bug or
   error, find the ROOT CAUSE (read the exact error, logs, the file/config involved) instead
   of guessing or retrying the same thing.
2. DO IT YOURSELF. Use `exec`, file tools, `cron`, pip/apt/npm to actually do the work. Do not
   give the user a list of manual steps if you can run them. Never say "I can't" or "restricted"
   before you have tried and seen a real error.
3. FIX CODE PROPERLY. When code or a script fails: read it, change the minimal thing that fixes
   the cause, save it, and run it again. Repeat until it works. Do not hand over untested code.
4. TEST EVERYTHING before reporting success:
   - run the script once manually and check the real output (exit code + stdout/stderr);
   - for cron: `cron action=list` to confirm it exists, and, if possible, a short test job
     (e.g. every_seconds=60) to prove the output reaches this chat, then set the real schedule
     and remove the test job;
   - for installs: import the package / run `--version` to confirm.
5. HANDLE FAILURES. If a test fails, go back to step 1 with the new error. Try a different
   approach after 2 identical failures. Only stop when it works, or when you are truly blocked
   (missing token, site down, etc.) - then show the exact error and what you already tried.
6. REPORT BRIEFLY. Final message: what was done, what you tested and the result, and where files
   live. No long apologies, no repeated questions. Reply in the same language the user writes in.
7. STAY SAFE ON THE DATA. Full permission is for getting the job done: do not delete the
   workspace, ~/.picoclaw, ~/.picow_clow or the user's data unless explicitly asked.
{BLOCK_END}
"""


def write_permission_notes(workspace: Path) -> None:
    """Idempotently (re)write the permissions block in USER.md, keeping the rest of the file."""
    (workspace / "scripts").mkdir(parents=True, exist_ok=True)
    block = PERMISSION_NOTES.replace("{WORKSPACE}", str(workspace))
    user_md = workspace / "USER.md"
    existing = user_md.read_text(encoding="utf-8") if user_md.exists() else ""
    if BLOCK_START in existing and BLOCK_END in existing:
        head = existing.split(BLOCK_START)[0]
        tail = existing.split(BLOCK_END, 1)[1]
        existing = (head.rstrip() + "\n\n" if head.strip() else "") + tail.lstrip()
    new_text = (existing.rstrip() + "\n\n" if existing.strip() else "") + block
    user_md.write_text(new_text, encoding="utf-8")


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

    # Minimum 10 minutes for every LLM request (custom APIs can be slow with a 250k context).
    # The env var can only RAISE it: an old LLM_REQUEST_TIMEOUT=360 on Koyeb must not lower it.
    MIN_TIMEOUT = 600
    try:
        request_timeout = int(os.environ.get("LLM_REQUEST_TIMEOUT", "").strip() or MIN_TIMEOUT)
    except ValueError:
        request_timeout = MIN_TIMEOUT
    request_timeout = max(request_timeout, MIN_TIMEOUT)

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

    def env_int(name: str, default: int) -> int:
        try:
            v = int(os.environ.get(name, "").strip() or default)
            return v if v > 0 else default
        except ValueError:
            return default

    # Context window of the custom API (tokens the model can see). Without this PicoClaw
    # assumes max_tokens*4 (= only ~32k) and summarizes the history far too early.
    context_window = env_int("LLM_CONTEXT_WINDOW", 250000)
    # Max OUTPUT tokens per reply (not the context size).
    max_tokens = env_int("LLM_MAX_TOKENS", 32768)

    cron_allowed_remotes = [
        x.strip()
        for x in os.environ.get("CRON_COMMAND_ALLOWED_REMOTES", "telegram").split(",")
        if x.strip()
    ]

    cfg = {
        "agents": {
            "defaults": {
                "workspace": workspace,
                "model_name": model_name,
                # docs: Security Sandbox -> "Disabling Restrictions"
                "restrict_to_workspace": False,
                "allow_read_outside_workspace": True,
                # let the agent finish multi-step jobs (install -> write script -> cron.add)
                "max_tool_iterations": 50,
                # sub-agents (default 5 min) get the same >=10 min
                "subturn": {"default_timeout_minutes": MIN_TIMEOUT // 60},
                "context_window": context_window,
                "max_tokens": max_tokens,
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
            # path allow-lists (only relevant if restrict_to_workspace is ever turned on)
            "allow_read_paths": ["/"],
            "allow_write_paths": ["/"],
            # docs: Command Execution Settings
            "exec": {
                "enabled": True,                 # master switch
                "allow_remote": True,            # Telegram/Discord/... may run commands
                "enable_deny_patterns": False,   # no built-in 41 deny regexes
                "custom_deny_patterns": [],
                "timeout_seconds": MIN_TIMEOUT,  # default is only 60s; >=10 min
            },
            "cron": {
                "enabled": True,
                "allow_command": True,           # no command_confirm needed
                # Telegram is NOT an "internal" channel in PicoClaw; without this list
                # cron.add(command=...) fails with:
                #   "scheduling command execution is restricted to internal
                #    channels or configured remote channels"
                # Entries: "telegram" (whole channel), "telegram:<chat_id>", or "*".
                "command_allowed_remotes": cron_allowed_remotes,
                "exec_timeout_minutes": MIN_TIMEOUT // 60,
            },
        },
        # docs: Subprocess Isolation (off by default; keep it explicitly off so the
        # agent's commands see the real filesystem, installed packages and $HOME)
        "isolation": {"enabled": False},
    }

    write_permission_notes(Path(workspace))

    config_path = picoclaw_home / "config.json"
    config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"Generated unrestricted PicoClaw config at {config_path} (live stream proxy -> {upstream_base})")
    print(f"context_window={context_window} max_tokens={max_tokens} request_timeout={request_timeout}s")


if __name__ == "__main__":
    main()
