#!/usr/bin/env python3
"""Telegram ``/log`` command: zip ALL server + PicoClaw logs and send the zip to the owner.

How it works (no change to PicoClaw itself):
  PicoClaw does not know ``/log``, so it passes the text on to the agent, i.e. it becomes an LLM request that goes
  through llm_proxy.py. The proxy checks "is the LAST message a user message that is exactly /log?" (tool results
  and normal chat never match). If so it calls run() here, answers PicoClaw with a short text and never contacts
  the real LLM, so the command costs no tokens and cannot disturb a running job. The zip goes straight to
  Telegram (sendDocument) with the bot token the proxy already has - no second getUpdates poller, so no 409
  "terminated by other getUpdates request" conflict with PicoClaw.

What is in the zip: 00_info.txt (time, PicoClaw build, non-secret settings, file list) + every log file under
/tmp (picoclaw.log, setup_picoclaw.log, stream_proxy.log, restore.log, ram_guard.log, server.log = the container
output Render/Koyeb shows, ...), /tmp/tailscale and ~/.picoclaw/logs. Secrets (bot token, API keys, bearer tokens,
values of *KEY*/*TOKEN*/*SECRET*... env vars) are replaced with <redacted>.

Env: LOG_COMMAND=0 turns the command off.
"""
import glob
import http.client
import json
import os
import re
import tempfile
import threading
import time
import uuid
import zipfile

MAX_ZIP_BYTES = 45 * 1024 * 1024          # Telegram bots may upload up to 50 MB
HOME = os.path.expanduser("~")
LOG_PATTERNS = (
    "/tmp/*.log",
    "/tmp/tailscale/*.log",
    os.path.join(HOME, ".picoclaw", "logs", "*"),
    os.path.join(HOME, ".picoclaw", "*.log"),
)
SHOWN_ENV = ("LLM_MODEL", "LLM_CONTEXT_WINDOW", "LLM_MAX_TOKENS", "LLM_SUMMARIZE_TOKEN_PERCENT",
             "LLM_SUMMARIZE_MESSAGE_THRESHOLD", "LLM_MAX_TOOL_ITERATIONS", "LLM_GUARD", "RAM_GUARD", "TZ")

COMMAND_RE = re.compile(r"^/logs?(?:@\w+)?\s*$", re.I)

_lock = threading.Lock()


# --------------------------------------------------------------------------- command detection
def last_user_text(payload):
    """Text of the LAST message of a chat request if (and only if) it is a user message, else None."""
    msgs = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(msgs, list) or not msgs or not isinstance(msgs[-1], dict):
        return None
    msg = msgs[-1]
    if msg.get("role") != "user":
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        return "".join(parts) if parts else None
    return None


def is_log_command(payload):
    if os.environ.get("LOG_COMMAND", "1").strip() == "0":
        return False
    text = last_user_text(payload)
    return bool(text and COMMAND_RE.match(text.strip()))


# --------------------------------------------------------------------------- redaction
_PATTERNS = (
    (re.compile(rb"bot\d{6,}:[A-Za-z0-9_-]{20,}"), b"bot<redacted>"),
    (re.compile(rb"(bearer\s+)[A-Za-z0-9._~+/=-]{12,}", re.I), rb"\1<redacted>"),
    (re.compile(rb"\b(?:sk-|AIza|ghp_|tskey-)[A-Za-z0-9_-]{12,}"), b"<redacted>"),
    (re.compile(rb"((?:api[_-]?key|access[_-]?token|auth[_-]?key|secret|password|passwd)[\"']?\s*[:=]\s*[\"']?)"
                rb"[^\s\"',}&]{6,}", re.I), rb"\1<redacted>"),
)


def _secret_regex():
    vals = []
    for name, val in os.environ.items():
        if len(val) >= 8 and re.search(r"KEY|TOKEN|SECRET|PASS|AUTH|CONFIG_BASE64|CLIENT_ID", name, re.I):
            vals.append(re.escape(val.encode("utf-8", "replace")))
    vals.sort(key=len, reverse=True)
    return re.compile(b"|".join(vals)) if vals else None


def redact(line, secret_re=None):
    if secret_re is not None:
        line = secret_re.sub(b"<redacted>", line)
    for pat, repl in _PATTERNS:
        line = pat.sub(repl, line)
    return line


# --------------------------------------------------------------------------- bundle
def collect_files():
    found = {}
    for pattern in LOG_PATTERNS:
        for path in glob.glob(pattern):
            try:
                if os.path.isfile(path) and os.path.getsize(path) > 0:
                    found[os.path.realpath(path)] = (path, os.path.getsize(path))
            except OSError:
                continue
    return sorted(found.values())


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _info_text(files, tail_limit):
    lines = [
        "hermes log bundle",
        "time: " + time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "picoclaw build: " + (_read_text("/etc/picoclaw-version") or "unknown"),
        "",
        "settings (non-secret):",
    ]
    for name in SHOWN_ENV:
        lines.append(f"  {name}={os.environ.get(name, '(default)')}")
    lines += ["", "files:"]
    for path, size in files:
        cut = f"  (last {tail_limit} bytes only)" if tail_limit and size > tail_limit else ""
        lines.append(f"  {path}  {size} bytes{cut}")
    return "\n".join(lines) + "\n"


def build_zip(files, dest, tail_limit=None):
    """Write `files` (redacted) to the zip `dest`. tail_limit=N keeps only the last N bytes of bigger files."""
    secret_re = _secret_regex()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("00_info.txt", _info_text(files, tail_limit))
        for path, size in files:
            try:
                with open(path, "rb") as src, zf.open(path.lstrip("/"), "w", force_zip64=True) as out:
                    if tail_limit and size > tail_limit:
                        src.seek(size - tail_limit)
                        src.readline()                      # drop the cut-off first line
                        out.write(f"[... older part cut, last {tail_limit} bytes shown ...]\n".encode("utf-8"))
                    for line in src:                        # streamed line by line: RAM stays tiny
                        out.write(redact(line, secret_re))
            except OSError:
                continue
    return os.path.getsize(dest)


# --------------------------------------------------------------------------- Telegram upload
def send_document(token, chat_id, path, caption, timeout=180):
    """sendDocument as a streamed multipart upload (the file is never fully loaded into RAM)."""
    boundary = "----hermes" + uuid.uuid4().hex

    def field(name, value):
        return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode("utf-8")

    head = field("chat_id", chat_id) + field("caption", caption[:1000]) + (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
        f"filename=\"{os.path.basename(path)}\"\r\nContent-Type: application/zip\r\n\r\n").encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    conn = http.client.HTTPSConnection("api.telegram.org", timeout=timeout)
    try:
        conn.putrequest("POST", f"/bot{token}/sendDocument")
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(len(head) + os.path.getsize(path) + len(tail)))
        conn.endheaders()
        conn.send(head)
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                conn.send(chunk)
        conn.send(tail)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        return bool(data.get("ok")), str(data.get("description", ""))[:200]
    finally:
        conn.close()


def _mb(n):
    return f"{n / 1048576:.1f} MB"


# --------------------------------------------------------------------------- entry point
def run(token=None, ids=None):
    """Build the bundle, send it to Telegram, return the text for the chat reply. Never raises."""
    if token is None:
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if ids is None:
        ids = [x.strip() for x in (os.environ.get("TELEGRAM_ALLOW_FROM") or "").split(",")]
        ids = [x for x in ids if x.lstrip("-").isdigit()]
    if not token or not ids:
        return "⚠️ /log: TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOW_FROM सेट नाहीत."
    if not _lock.acquire(blocking=False):
        return "⏳ /log आधीच चालू आहे, थोड्या वेळाने पुन्हा पाठवा."
    dest = os.path.join(tempfile.gettempdir(), "hermes-logs-" + time.strftime("%Y%m%d-%H%M%S") + ".zip")
    try:
        files = collect_files()
        if not files:
            return "ℹ️ /log: एकही log file सापडली नाही."
        raw = sum(size for _, size in files)
        zip_size, used_tail = 0, None
        for tail_limit in (None, 8 << 20, 2 << 20, 512 << 10):
            zip_size = build_zip(files, dest, tail_limit)
            used_tail = tail_limit
            if zip_size <= MAX_ZIP_BYTES:
                break
        if zip_size > MAX_ZIP_BYTES:
            return f"❌ /log: zip खूप मोठी ({_mb(zip_size)}), Telegram limit 50 MB."
        names = ", ".join(os.path.basename(p) for p, _ in files)
        caption = (f"📎 hermes logs {time.strftime('%Y-%m-%d %H:%M')}\n{len(files)} files, {_mb(raw)} → zip "
                   f"{_mb(zip_size)}" + (" (मोठ्या files चा शेवटचा भाग)" if used_tail else "") + f"\n{names}")
        sent, errors = 0, []
        for chat_id in ids:
            try:
                ok, desc = send_document(token, chat_id, dest, caption)
            except Exception as exc:  # noqa: BLE001
                ok, desc = False, repr(exc)[:150]
            if ok:
                sent += 1
            else:
                errors.append(f"{chat_id}: {desc}")
        if sent:
            return f"✅ Logs पाठवले ({len(files)} files, zip {_mb(zip_size)})."
        return "❌ Logs पाठवता आले नाहीत: " + "; ".join(errors)[:300]
    except Exception as exc:  # noqa: BLE001
        return f"❌ /log फेल: {exc!r}"[:300]
    finally:
        try:
            os.remove(dest)
        except OSError:
            pass
        _lock.release()
