#!/usr/bin/env python3
"""hermes LLM guard proxy (127.0.0.1 -> your custom OpenAI-compatible API).

Sits between PicoClaw and the custom API (which round-robins gemini-* models and
falls back to *-lite models at the end). What it does:

1. Always talks SSE to upstream (avoids Koyeb/Cloudflare 504 on long replies) and,
   when PicoClaw asked for a normal JSON reply, folds the SSE back into one
   chat.completion.                                         (same as the old proxy)
2. Retries transient upstream failures (408/429/5xx, connection errors) with backoff.
   Each retry is a fresh call, so your round-robin can pick another model.
3. Validates every agent reply BEFORE PicoClaw sees it and silently retries bad ones,
   which is what weak/lite models get wrong most often:
     - empty reply (no text, no tool call)
     - tool call with broken JSON arguments (repaired if possible, else retried)
     - tool call cut off by max_tokens
     - "announcement" replies ("Now I will run X:") that never call the tool
4. Fixes Gemini-style streamed tool calls that all arrive with index 0
   (old code glued them into one broken call like  exec+exec  /  {..}{..}).
5. Appends a short reminder to the LAST message of every agent request (not stored in
   the session history). Stronger wording when the previous answer came from a *lite* model.

Guard is applied only to requests that carry `tools` (agent turns); summarisation and
other plain calls pass through untouched.

Env: see README ("Agent quality guard"). LLM_GUARD=0 turns steps 3 and 5 off.
"""
import http.client
import json
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _env_int(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


UPSTREAM_BASE = os.environ.get("PICOCLAW_UPSTREAM_API_BASE", "https://api.openai.com/v1").rstrip("/")
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = _env_int("LOCAL_LLM_PROXY_PORT", 18799)
TIMEOUT = _env_int("LLM_REQUEST_TIMEOUT", 600)

GUARD = (os.environ.get("LLM_GUARD", "1").strip() != "0")
REMINDER = (os.environ.get("LLM_REMINDER", "1").strip() != "0")
HTTP_RETRIES = _env_int("LLM_HTTP_RETRIES", 3)          # transient upstream errors (429/5xx/conn)
# The custom API sits behind Cloudflare/Koyeb, which cuts a connection that is SILENT for ~100 s and
# answers "504 error code: 504" - even when the backend later finishes fine (logged as 200 Success).
# So we give up on silence BEFORE that (90 s) and retry, which normally lands on a faster model.
# Backend keep-alive bytes (SSE comments) reset these timers, so healthy slow calls are not cut.
FIRST_BYTE_TIMEOUT = _env_float("LLM_FIRST_BYTE_TIMEOUT", 90)    # no response bytes for this long -> hung
STREAM_IDLE_TIMEOUT = _env_float("LLM_STREAM_IDLE_TIMEOUT", 90)  # no SSE bytes for this long -> hung
TIMEOUT_RETRIES = _env_int("LLM_TIMEOUT_RETRIES", 2)    # retries after a hang / 504 / 524
BAD_REPLY_RETRIES = _env_int("LLM_GUARD_MAX_RETRIES", 2)  # bad/empty/stalled replies
BACKOFF = _env_float("LLM_RETRY_BACKOFF", 2.0)
LITE_RE = re.compile(os.environ.get("LLM_LITE_PATTERN", "lite") or "lite", re.I)
WORKSPACE = os.environ.get("HERMES_WORKSPACE", "~/.picoclaw/workspace")
SLOW_STATUS = {408, 504, 524}                 # backend already waited a long time
RETRY_STATUS = {425, 429, 500, 502, 503}

_state_lock = threading.Lock()
_state = {"last_model": "", "last_was_bad": False}


LOGFILE = os.environ.get("LLM_PROXY_LOGFILE", "/tmp/stream_proxy.log")
ACCESS_LOG = os.environ.get("LLM_PROXY_ACCESS_LOG", "0") == "1"


def log(msg):
    """stderr = container/Koyeb log (shows which model answered); also kept in LOGFILE."""
    line = f"[guard] {msg}\n"
    sys.stderr.write(line)
    sys.stderr.flush()
    if LOGFILE:
        try:
            with open(LOGFILE, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%H:%M:%S ") + line)
        except OSError:
            pass


# --------------------------------------------------------------------------- URL
def build_upstream_url(req_path: str) -> str:
    parsed_base = urllib.parse.urlparse(UPSTREAM_BASE)
    base_path = parsed_base.path.rstrip("/")
    if base_path.endswith("/v1") and req_path.startswith("/v1/"):
        req_path = req_path[3:]
    elif base_path.endswith("/v1") and req_path == "/v1":
        req_path = ""
    return f"{UPSTREAM_BASE}{req_path}"


# --------------------------------------------------------------------------- SSE -> JSON
def sse_to_chat_completion(raw_lines):
    content_parts = []
    reasoning_parts = []
    tool_calls = []          # list of entries, in arrival order
    by_index = {}            # upstream index -> position in tool_calls
    model = ""
    comp_id = "chatcmpl-proxy"
    finish_reason = "stop"
    usage = None

    def new_entry(tc, idx):
        entry = {
            "id": tc.get("id") or f"call_{len(tool_calls)}",
            "type": tc.get("type") or "function",
            "function": {"name": "", "arguments": ""},
            "_real_id": bool(tc.get("id")),
        }
        tool_calls.append(entry)
        by_index[idx] = len(tool_calls) - 1
        return entry

    def args_complete(entry):
        a = entry["function"]["arguments"].strip()
        if not a:
            return False
        try:
            json.loads(a)
            return True
        except ValueError:
            return False

    for raw in raw_lines:
        line = raw.decode("utf-8", errors="replace").strip() if isinstance(raw, bytes) else raw.strip()
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
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
                fn = tc.get("function") or {}
                pos = by_index.get(idx)
                entry = tool_calls[pos] if pos is not None else None
                # Gemini-compatible gateways often stream EVERY parallel tool call with
                # index 0. A different id, or a second function name once the first call's
                # arguments are already complete JSON, means "this is a new call".
                if entry is not None:
                    new_id = tc.get("id")
                    if new_id and entry["_real_id"] and new_id != entry["id"]:
                        entry = None
                    elif fn.get("name") and entry["function"]["name"] and args_complete(entry):
                        entry = None
                if entry is None:
                    entry = new_entry(tc, idx)
                if tc.get("id"):
                    entry["id"] = tc["id"]
                    entry["_real_id"] = True
                if fn.get("name"):
                    entry["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    entry["function"]["arguments"] += fn["arguments"]

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        for e in tool_calls:
            e.pop("_real_id", None)
        message["tool_calls"] = tool_calls
        if finish_reason == "stop":
            finish_reason = "tool_calls"

    result = {
        "id": comp_id,
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage:
        result["usage"] = usage
    return result


# --------------------------------------------------------------------------- validation
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def repair_tool_arguments(msg):
    """Make every tool call's `arguments` valid JSON. Returns None if ok, else a reason."""
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        if not (fn.get("name") or "").strip():
            return "tool_call_without_name"
        args = fn.get("arguments")
        if args is None or (isinstance(args, str) and not args.strip()):
            fn["arguments"] = "{}"
            continue
        if not isinstance(args, str):
            fn["arguments"] = json.dumps(args)
            continue
        try:
            obj = json.loads(args)
            if isinstance(obj, dict):
                continue
        except ValueError:
            pass
        cleaned = _FENCE_RE.sub("", args.strip())
        lo, hi = cleaned.find("{"), cleaned.rfind("}")
        if lo != -1 and hi > lo:
            try:
                obj = json.loads(cleaned[lo:hi + 1])
                if isinstance(obj, dict):
                    fn["arguments"] = json.dumps(obj, ensure_ascii=False)
                    continue
            except ValueError:
                pass
        return "bad_tool_json"
    return None


# forward-looking "I will do X" phrases (English / Hindi / Marathi, Devanagari + Roman)
_INTENT_RE = re.compile(
    r"(\blet me(?! know)\b|\bi(?:'|’)ll\b|\bi will\b|\bi am going to\b|\bi(?:'|’)m going to\b|"
    r"\bnow i\b|\bnext,? i\b|\bgoing to (?:run|check|create|write|install|fix|test)\b|"
    r"आता मी|मी आता|पुढे मी|आता मैं|अब मैं|करता हूँ|करता हूं|करतो|बघतो|पाहतो|"
    r"\b(?:ata|aata) mi\b|\bmi (?:ata|aata)\b|\bpudhe mi\b|\bbaghto\b|\bpahto\b|\bkarto\b)",
    re.I,
)
_DONE_RE = re.compile(r"(✅|\bdone\b|\bcompleted\b|\bfinished\b|पूर्ण झाले|झालं|झाले|\bzala\b|\bkela\b)", re.I)


def looks_stalled(content, last_role):
    text = (content or "").strip()
    if not text:
        return False
    limit = 600 if last_role == "tool" else 300
    if len(text) > limit or _DONE_RE.search(text):
        return False
    last_line = [ln for ln in text.splitlines() if ln.strip()][-1].strip()
    if len(last_line) > 220:
        return False
    return bool(_INTENT_RE.search(last_line)) or last_line.endswith((":", "…", "..."))


def classify(comp, payload):
    """Return a reason string if the reply is bad, else None."""
    try:
        choice = comp["choices"][0]
    except (KeyError, IndexError, TypeError):
        return "no_choices"
    msg = choice.get("message") or {}
    if msg.get("tool_calls"):
        reason = repair_tool_arguments(msg)
        if reason:
            return reason
        if choice.get("finish_reason") == "length":
            return "truncated_tool_call"
        return None
    content = msg.get("content") or ""
    if not content.strip():
        return "empty"
    msgs = payload.get("messages") or []
    last_role = msgs[-1].get("role") if msgs and isinstance(msgs[-1], dict) else ""
    if payload.get("tools") and looks_stalled(content, last_role):
        return "stalled"
    return None


NUDGES = {
    "stalled": (
        "[hermes-guard] You described an action but did not perform it. Do NOT answer with a plan "
        "or an announcement. Call the needed tool NOW. Only if every requirement is really finished "
        "and verified, reply with the final report instead."
    ),
    "empty": (
        "[hermes-guard] Your last reply was empty. Continue the task: call the next tool, or give "
        "the final report if everything is finished and verified."
    ),
    "bad_tool_json": (
        "[hermes-guard] Your last tool call had invalid JSON arguments. Call the tool again with "
        "strictly valid JSON (double quotes, no comments, no trailing commas, escape newlines)."
    ),
    "truncated_tool_call": (
        "[hermes-guard] Your last tool call was cut off because it was too long. Split the work into "
        "smaller steps (e.g. write a big file in several parts) and call the tool again."
    ),
}


# --------------------------------------------------------------------------- reminder
def reminder_text(strong):
    base = (
        "[hermes-reminder] Finish EVERY part of the user's request: each thing they asked = one "
        "requirement. Do the work with tools instead of describing it. Verify with real output "
        "before saying done. If you say you will do something, do it now with a tool call."
    )
    if not strong:
        return base
    return (
        base
        + f" Keep the checklist in {WORKSPACE}/tasks/current.md and update it after each step. "
        "Do not skip steps, do not shorten code, no placeholders. Tool arguments must be valid "
        "JSON. Work one step at a time: act, read the result, then the next step."
    )


def inject_reminder(payload, strong):
    msgs = payload.get("messages")
    if not isinstance(msgs, list) or not msgs or not isinstance(msgs[-1], dict):
        return payload
    last = msgs[-1]
    if last.get("role") not in ("user", "tool"):
        return payload
    text = "\n\n" + reminder_text(strong)
    content = last.get("content")
    if isinstance(content, str):
        if "[hermes-reminder]" in content:
            return payload
        last["content"] = content + text
    elif isinstance(content, list):
        if any(isinstance(p, dict) and "[hermes-reminder]" in str(p.get("text", "")) for p in content):
            return payload
        content.append({"type": "text", "text": text.strip()})
    else:
        return payload
    return payload


def append_nudge(payload, reason):
    text = NUDGES.get(reason)
    if text:
        payload.setdefault("messages", []).append({"role": "user", "content": text})


# --------------------------------------------------------------------------- upstream
def _expired(deadline):
    return deadline is not None and time.time() > deadline


def open_upstream(method, target_url, headers, body, deadline=None):
    """Open upstream with retries. Returns (conn, resp) of the final try.

    - connection errors / 425/429/5xx        -> up to LLM_HTTP_RETRIES retries with backoff
    - no headers for FIRST_BYTE_TIMEOUT, 408/504/524 -> up to LLM_TIMEOUT_RETRIES retries
    - when retries are used up, an error *response* is returned as is (so PicoClaw sees the status).
    """
    parsed = urllib.parse.urlparse(target_url)
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    http_fail = slow_fail = 0
    while True:
        conn = resp = err = None
        t0 = time.time()
        try:
            conn = conn_cls(parsed.hostname, port, timeout=FIRST_BYTE_TIMEOUT)
            conn.request(method, path, body=body if body else None, headers=headers)
            resp = conn.getresponse()
        except (socket.timeout, TimeoutError) as exc:
            err, kind = exc, "slow"
        except Exception as exc:  # noqa: BLE001
            err, kind = exc, "http"
        else:
            if resp.status in SLOW_STATUS:
                kind = "slow"
            elif resp.status in RETRY_STATUS:
                kind = "http"
            else:
                if conn.sock is not None:
                    conn.sock.settimeout(STREAM_IDLE_TIMEOUT)   # from now on: max silence between chunks
                return conn, resp

        if kind == "slow":
            slow_fail += 1
            allowed = slow_fail <= TIMEOUT_RETRIES
            wait = 1.0 + random.random()
            limit = f"{slow_fail}/{TIMEOUT_RETRIES}"
        else:
            http_fail += 1
            allowed = http_fail <= HTTP_RETRIES
            wait = _retry_wait(resp, http_fail - 1) if resp is not None else min(BACKOFF * (2 ** (http_fail - 1)) + random.random(), 30.0)
            limit = f"{http_fail}/{HTTP_RETRIES}"
        what = f"HTTP {resp.status}" if resp is not None else repr(err)
        took = int((time.time() - t0) * 1000)
        if not allowed or _expired(deadline):
            log(f"upstream {what} after {took}ms - giving up ({'time budget used' if allowed else 'retries used'})")
            if resp is not None:
                return conn, resp
            if conn:
                conn.close()
            raise err
        log(f"upstream {what} after {took}ms ({kind}), retry {limit} in {wait:.1f}s")
        if conn:
            try:
                if resp is not None:
                    resp.read()
            except Exception:  # noqa: BLE001
                pass
            conn.close()
        time.sleep(wait)


def _retry_wait(resp, attempt):
    ra = resp.getheader("Retry-After")
    try:
        if ra:
            return min(float(ra), 30.0)
    except ValueError:
        pass
    return min(BACKOFF * (2 ** attempt) + random.random(), 30.0)


def read_completion(resp, hard_deadline=None):
    """Return (completion_dict, None) or (None, raw_bytes) when it is not a chat completion.
    Raises socket.timeout when the stream goes silent (STREAM_IDLE_TIMEOUT) or the hard deadline passes."""
    ctype = (resp.getheader("Content-Type") or "").lower()
    if resp.status == 200 and "text/event-stream" in ctype:
        lines = []
        while True:
            line = resp.readline()
            if not line:
                break
            lines.append(line)
            if hard_deadline is not None and time.time() > hard_deadline:
                raise TimeoutError("stream exceeded LLM_REQUEST_TIMEOUT")
        return sse_to_chat_completion(lines), None
    raw = resp.read()
    if resp.status == 200:
        try:
            obj = json.loads(raw.decode("utf-8"))
            if isinstance(obj, dict) and obj.get("choices"):
                return obj, None
        except Exception:  # noqa: BLE001
            pass
    return None, raw


# --------------------------------------------------------------------------- handler
class GuardProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if ACCESS_LOG:
            log(fmt % args)

    # -- small helpers
    def _send_json(self, status, obj):
        out = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(out)

    def _send_raw(self, resp, raw):
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.getheader("Content-Type") or "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def _forward_headers(self, body):
        fwd = {}
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length", "accept-encoding", "connection"):
                continue
            fwd[k] = v
        if body:
            fwd["Content-Length"] = str(len(body))
        return fwd

    # -- main entry
    def _handle_request(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length > 0 else b""

        payload = None
        client_wants_stream = False
        is_chat = self.path.rstrip("/").endswith("/chat/completions")
        if body and "json" in (self.headers.get("Content-Type") or "").lower():
            try:
                parsed = json.loads(body.decode("utf-8"))
                if isinstance(parsed, dict):
                    payload = parsed
                    client_wants_stream = bool(payload.get("stream", False))
            except Exception:  # noqa: BLE001
                payload = None

        target_url = build_upstream_url(self.path)
        try:
            if payload is not None and is_chat and not client_wants_stream:
                self._guarded_chat(target_url, payload)
                return
            if payload is not None and is_chat:
                payload["stream"] = True
                body = json.dumps(payload).encode("utf-8")
            self._passthrough(target_url, body, client_wants_stream)
        except Exception as exc:  # noqa: BLE001
            try:
                self._send_json(502, {"error": {"message": f"stream_proxy error: {exc}"}})
            except Exception:  # noqa: BLE001
                pass

    def _passthrough(self, target_url, body, client_wants_stream):
        conn, resp = open_upstream(self.command, target_url, self._forward_headers(body), body)
        try:
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
                    comp, _ = read_completion(resp)
                    self._send_json(200, comp)
            else:
                self._send_raw(resp, resp.read())
        finally:
            conn.close()

    def _guarded_chat(self, target_url, payload):
        payload["stream"] = True
        agent_turn = bool(payload.get("tools"))
        guard_on = GUARD and agent_turn
        with _state_lock:
            prev_lite = bool(LITE_RE.search(_state["last_model"] or ""))
            prev_bad = _state["last_was_bad"]
        if guard_on and REMINDER:
            inject_reminder(payload, strong=prev_lite or prev_bad)

        semantic_retries = 0
        stall_retried = False
        read_failures = 0
        call_started = time.time()
        retry_deadline = call_started + max(TIMEOUT - 30, 60)   # stay inside PicoClaw's own request timeout
        hard_deadline = call_started + TIMEOUT
        while True:
            started = time.time()
            body = json.dumps(payload).encode("utf-8")
            conn, resp = open_upstream("POST", target_url, self._forward_headers(body), body, retry_deadline)
            try:
                comp, raw = read_completion(resp, hard_deadline)
            except (socket.timeout, TimeoutError, http.client.HTTPException, OSError) as exc:
                read_failures += 1
                log(f"stream stalled/broken after {int(time.time() - started)}s ({exc!r}) "
                    f"retry {read_failures}/{TIMEOUT_RETRIES}")
                if read_failures <= TIMEOUT_RETRIES and not _expired(retry_deadline):
                    time.sleep(1.0)
                    continue
                raise
            finally:
                conn.close()
            if comp is None:                      # upstream error / non-completion: pass it on
                self._send_raw(resp, raw)
                return

            model = comp.get("model") or ""
            reason = classify(comp, payload) if guard_on else None
            try:
                ch = comp["choices"][0]
                n_calls = len((ch.get("message") or {}).get("tool_calls") or [])
                finish = ch.get("finish_reason")
            except (KeyError, IndexError, TypeError):
                n_calls, finish = 0, None
            with _state_lock:
                _state["last_model"] = model
                _state["last_was_bad"] = reason is not None
            log(
                f"model={model or '?'} finish={finish} tool_calls={n_calls} "
                f"reason={reason or 'ok'} retry={semantic_retries} ms={int((time.time() - started) * 1000)} "
                f"req_kb={len(body) // 1024}"
            )

            can_retry = (
                reason is not None
                and semantic_retries < BAD_REPLY_RETRIES
                and not (reason == "stalled" and stall_retried)
                and reason != "no_choices"
            )
            if not can_retry:
                self._send_json(200, comp)
                return
            semantic_retries += 1
            stall_retried = stall_retried or reason == "stalled"
            append_nudge(payload, reason)
            time.sleep(1.0)

    def do_GET(self):
        self._handle_request()

    def do_POST(self):
        self._handle_request()


def main():
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), GuardProxyHandler)
    print(
        f"LLM guard proxy listening on http://{LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM_BASE} "
        f"(guard={'on' if GUARD else 'off'}, reminder={'on' if REMINDER else 'off'}, "
        f"first_byte={FIRST_BYTE_TIMEOUT:g}s, idle={STREAM_IDLE_TIMEOUT:g}s)",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
