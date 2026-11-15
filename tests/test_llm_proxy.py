"""Self-contained tests for llm_proxy.py (mock upstream, no network). Run:  python3 -m unittest -v tests.test_llm_proxy"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---- mock upstream -------------------------------------------------------
SCRIPT = []      # list of responses to serve, in order. item: ("status", code) | ("sse", [chunks]) | ("json", obj)
SEEN = []        # request payloads the upstream received


def sse(*chunks):
    return ("sse", list(chunks))


def text_chunks(text, model="gemini-3.5-flash"):
    return [
        {"model": model, "choices": [{"delta": {"content": text}}]},
        {"model": model, "choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]


def tool_chunks(calls, model="gemini-3.5-flash", finish="tool_calls"):
    """calls: list of (index, id, name, args_str)"""
    out = []
    for idx, cid, name, args in calls:
        tc = {"index": idx, "function": {"arguments": args}}
        if cid:
            tc["id"] = cid
        if name:
            tc["function"]["name"] = name
        out.append({"model": model, "choices": [{"delta": {"tool_calls": [tc]}}]})
    out.append({"model": model, "choices": [{"delta": {}, "finish_reason": finish}]})
    return out


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        SEEN.append(json.loads(self.rfile.read(n).decode()))
        kind, val = SCRIPT.pop(0)
        try:
            self._serve(kind, val)
        except (BrokenPipeError, ConnectionResetError):
            pass   # proxy gave up on us (hang test)

    def _serve(self, kind, val):
        if kind == "hang":               # backend accepts the call but never answers
            threading.Event().wait(val)
            return
        if kind == "stall":              # sends the first chunk, then goes silent
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"par"}}]}\n\n')
            self.wfile.flush()
            threading.Event().wait(val)
            return
        if kind == "slow_alive":         # slow reply, but keep-alive comments + chunks keep flowing (total 1.5s > idle 0.6s)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for _ in range(5):
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                threading.Event().wait(0.3)
            for ch in val:
                self.wfile.write(f"data: {json.dumps(ch)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        if kind == "status":
            raw = b'{"error":"busy"}'
            self.send_response(val)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
        elif kind == "json":
            raw = json.dumps(val).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for ch in val:
                self.wfile.write(f"data: {json.dumps(ch)}\n\n".encode())
            if kind != "sse_nodone":          # connection dropped: no finish_reason, no [DONE]
                self.wfile.write(b"data: [DONE]\n\n")


def wait(sec):
    threading.Event().wait(sec)      # real wait (setUp stubs time.sleep)


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        up_port, px_port = free_port(), free_port()
        cls.up = ThreadingHTTPServer(("127.0.0.1", up_port), Upstream)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        os.environ.update(
            PICOCLAW_UPSTREAM_API_BASE=f"http://127.0.0.1:{up_port}/v1",
            LOCAL_LLM_PROXY_PORT=str(px_port),
            LLM_RETRY_BACKOFF="0.05",
            LLM_FIRST_BYTE_TIMEOUT="0.6",
            LLM_STREAM_IDLE_TIMEOUT="0.6",
            LLM_TIMEOUT_RETRIES="1",
            LLM_HTTP_RETRIES="3",
            LLM_NOTIFY_ERRORS="0",
            HERMES_WORKSPACE="/ws",
        )
        import llm_proxy
        cls.mod = llm_proxy
        cls.px = ThreadingHTTPServer(("127.0.0.1", px_port), llm_proxy.GuardProxyHandler)
        threading.Thread(target=cls.px.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{px_port}/v1/chat/completions"

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()
        cls.px.shutdown()

    def setUp(self):
        SCRIPT.clear()
        SEEN.clear()
        self.mod._state.update(last_model="", last_was_bad=False)
        # speed: the guard sleeps 1s between semantic retries
        self.mod.time.sleep = lambda s: None

    def call(self, messages, tools=True, stream=False, tool_names=("exec",)):
        body = {"model": "x", "messages": messages, "stream": stream}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": n, "parameters": {}}} for n in tool_names]
        req = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, raw

    def msg(self, text="do it", role="user"):
        return [{"role": role, "content": text}]

    # ---- tests
    def test_plain_reply_is_folded_to_json(self):
        SCRIPT.append(sse(*text_chunks("All done, file saved ✅")))
        st, raw = self.call(self.msg())
        out = json.loads(raw)
        self.assertEqual(out["choices"][0]["message"]["content"], "All done, file saved ✅")
        self.assertEqual(len(SEEN), 1)
        self.assertTrue(SEEN[0]["stream"])

    def test_http_429_is_retried(self):
        SCRIPT.extend([("status", 429), ("status", 503), sse(*text_chunks("ok ✅"))])
        st, raw = self.call(self.msg())
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "ok ✅")
        self.assertEqual(len(SEEN), 3)

    def test_empty_reply_is_retried_with_nudge(self):
        SCRIPT.extend([sse(*text_chunks("")), sse(*tool_chunks([(0, "c1", "exec", '{"cmd":"ls"}')]))])
        _, raw = self.call(self.msg())
        calls = json.loads(raw)["choices"][0]["message"]["tool_calls"]
        self.assertEqual(calls[0]["function"]["name"], "exec")
        self.assertEqual(len(SEEN), 2)
        self.assertIn("hermes-guard", SEEN[1]["messages"][-1]["content"])

    def test_broken_tool_json_retried(self):
        SCRIPT.extend([
            sse(*tool_chunks([(0, "c1", "exec", '{"cmd": "ls"')])),   # truncated -> unrepairable
            sse(*tool_chunks([(0, "c2", "exec", '{"cmd": "ls"}')])),
        ])
        _, raw = self.call(self.msg())
        args = json.loads(raw)["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(json.loads(args), {"cmd": "ls"})
        self.assertEqual(len(SEEN), 2)
        self.assertIn("invalid JSON", SEEN[1]["messages"][-1]["content"])

    def test_fenced_tool_json_is_repaired_without_retry(self):
        SCRIPT.append(sse(*tool_chunks([(0, "c1", "exec", '```json\n{"cmd": "ls"}\n```')])))
        _, raw = self.call(self.msg())
        args = json.loads(raw)["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(json.loads(args), {"cmd": "ls"})
        self.assertEqual(len(SEEN), 1)

    def test_empty_args_become_empty_object(self):
        SCRIPT.append(sse(*tool_chunks([(0, "c1", "exec", "")])))
        _, raw = self.call(self.msg())
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], "{}")

    def test_truncated_tool_call_retried(self):
        SCRIPT.extend([
            sse(*tool_chunks([(0, "c1", "exec", '{"cmd": "x"}')], finish="length")),
            sse(*tool_chunks([(0, "c2", "exec", '{"cmd": "y"}')])),
        ])
        _, raw = self.call(self.msg())
        args = json.loads(raw)["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(json.loads(args), {"cmd": "y"})
        self.assertEqual(len(SEEN), 2)

    def test_stalled_announcement_retried_once(self):
        SCRIPT.extend([
            sse(*text_chunks("Script ready. Now I will run it:")),
            sse(*tool_chunks([(0, "c1", "exec", '{"cmd":"python3 a.py"}')])),
        ])
        _, raw = self.call(self.msg("ok", role="tool"))
        self.assertTrue(json.loads(raw)["choices"][0]["message"].get("tool_calls"))
        self.assertEqual(len(SEEN), 2)

    def test_stalled_is_retried_up_to_the_retry_limit_then_passed_on(self):
        SCRIPT.extend([sse(*text_chunks("Let me check:")), sse(*text_chunks("Let me check again:")), sse(*text_chunks("Still checking:"))])
        _, raw = self.call(self.msg())
        self.assertEqual(len(SEEN), 3)   # original + 2 re-asks (LLM_GUARD_MAX_RETRIES), then passed on as is

    def test_real_final_answer_not_retried(self):
        SCRIPT.append(sse(*text_chunks("Cron job created and tested ✅. Let me know if you want changes.")))
        self.call(self.msg())
        self.assertEqual(len(SEEN), 1)

    def test_gemini_parallel_calls_all_index_zero(self):
        SCRIPT.append(sse(*tool_chunks([
            (0, "call_a", "exec", '{"cmd":"ls"}'),
            (0, "call_b", "read_file", '{"path":"/x"}'),
        ])))
        _, raw = self.call(self.msg())
        calls = json.loads(raw)["choices"][0]["message"]["tool_calls"]
        self.assertEqual([c["function"]["name"] for c in calls], ["exec", "read_file"])
        self.assertEqual(json.loads(calls[1]["function"]["arguments"]), {"path": "/x"})

    def test_gemini_parallel_calls_without_ids(self):
        SCRIPT.append(sse(*tool_chunks([(0, None, "exec", '{"cmd":"ls"}'), (0, None, "exec", '{"cmd":"pwd"}')])))
        _, raw = self.call(self.msg())
        calls = json.loads(raw)["choices"][0]["message"]["tool_calls"]
        self.assertEqual(len(calls), 2)

    def test_openai_incremental_args_still_merge(self):
        SCRIPT.append(sse(*tool_chunks([(0, "c1", "exec", '{"cmd":'), (0, None, None, ' "ls"}')])))
        _, raw = self.call(self.msg())
        calls = json.loads(raw)["choices"][0]["message"]["tool_calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"cmd": "ls"})

    def test_reminder_injected_in_last_message_only_for_agent_turns(self):
        SCRIPT.extend([sse(*text_chunks("a ✅")), sse(*text_chunks("Summary text."))])
        self.call([{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}])
        self.assertIn("[hermes-reminder]", SEEN[0]["messages"][-1]["content"])
        self.assertNotIn("[hermes-reminder]", SEEN[0]["messages"][0]["content"])
        self.call(self.msg("summarize this"), tools=False)        # no tools -> untouched
        self.assertEqual(SEEN[1]["messages"][-1]["content"], "summarize this")

    def test_reminder_goes_into_tool_message_and_list_content(self):
        SCRIPT.extend([sse(*text_chunks("a ✅")), sse(*text_chunks("b ✅"))])
        self.call([{"role": "tool", "content": "exit 0", "tool_call_id": "1"}])
        self.assertIn("[hermes-reminder]", SEEN[0]["messages"][-1]["content"])
        self.call([{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
        self.assertIn("[hermes-reminder]", SEEN[1]["messages"][-1]["content"][-1]["text"])

    def test_strong_reminder_after_lite_model(self):
        SCRIPT.extend([
            sse(*text_chunks("one ✅", model="gemini-3.5-flash-lite")),
            sse(*text_chunks("two ✅", model="gemini-3.5-flash")),
        ])
        self.call(self.msg("first"))
        self.call(self.msg("second"))
        self.assertNotIn("tasks/current.md", SEEN[0]["messages"][-1]["content"])
        self.assertIn("/ws/tasks/current.md", SEEN[1]["messages"][-1]["content"])

    def test_no_tools_no_stall_retry(self):
        SCRIPT.append(sse(*text_chunks("Let me think:")))
        self.call(self.msg(), tools=False)
        self.assertEqual(len(SEEN), 1)

    def test_client_streaming_passthrough(self):
        SCRIPT.append(sse(*text_chunks("hi")))
        _, raw = self.call(self.msg(), stream=True)
        self.assertIn(b"data:", raw)
        self.assertIn(b"[DONE]", raw)

    # ---- v2 fixes: explanation after send_file, cut-off text, guarded streaming
    SENT = ("File \"x.py\" sent to user\nThe requested output has already been delivered to the user in the current "
            "chat. Do not call send_file or any other delivery tool again. If you reply, provide only a brief confirmation.")

    def test_send_file_result_is_rewritten_so_model_must_explain(self):
        SCRIPT.append(sse(*text_chunks("Root cause: X. Changed: Y. Tested: Z. Run: python x.py " + "a" * 200)))
        self.call([{"role": "user", "content": "fix"}, {"role": "tool", "content": self.SENT, "tool_call_id": "1"}])
        sent = SEEN[0]["messages"][-1]["content"]
        self.assertNotIn("only a brief confirmation", sent)
        self.assertIn("explanation", sent.lower())

    def test_bare_confirmation_after_send_file_is_retried(self):
        SCRIPT.extend([sse(*text_chunks("Requested output delivered via tool attachment.")),
                       sse(*text_chunks("Root cause: URL had literal ${e}. Changed: ... " + "b" * 200))])
        _, raw = self.call([{"role": "tool", "content": self.SENT, "tool_call_id": "1"}])
        self.assertEqual(len(SEEN), 2)
        self.assertIn("no explanation", SEEN[1]["messages"][-1]["content"])
        self.assertIn("Root cause", json.loads(raw)["choices"][0]["message"]["content"])

    def test_text_cut_by_max_tokens_is_retried(self):
        SCRIPT.extend([sse(*[{"model": "m", "choices": [{"delta": {"content": "half an ans"}, "finish_reason": "length"}]}]),
                       sse(*text_chunks("Full short answer."))])
        _, raw = self.call(self.msg())
        self.assertEqual(len(SEEN), 2)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "Full short answer.")

    def test_stream_dropped_without_finish_is_retried(self):
        SCRIPT.extend([("sse_nodone", [{"model": "m", "choices": [{"delta": {"content": "par"}}]}]),
                       sse(*text_chunks("complete answer"))])
        _, raw = self.call(self.msg())
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "complete answer")

    def test_streaming_client_with_tools_is_validated_and_re_emitted(self):
        SCRIPT.extend([sse(*text_chunks("")),     # empty -> must be retried even though client wants stream
                       sse(*tool_chunks([(0, "c1", "exec", '{"cmd":"ls"}')]))])
        _, raw = self.call(self.msg(), stream=True)
        self.assertEqual(len(SEEN), 2)
        text = raw.decode()
        self.assertIn("[DONE]", text)
        chunks = [json.loads(l[5:]) for l in text.splitlines() if l.startswith("data:") and "[DONE]" not in l]
        tcs = [c["choices"][0]["delta"]["tool_calls"][0] for c in chunks if c["choices"] and c["choices"][0]["delta"].get("tool_calls")]
        self.assertEqual(tcs[0]["function"]["name"], "exec")
        self.assertEqual(json.loads(tcs[0]["function"]["arguments"]), {"cmd": "ls"})
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")

    def test_agent_temperature_is_lowered(self):
        SCRIPT.append(sse(*text_chunks("ok ✅")))
        body = {"model": "x", "temperature": 0.7, "stream": False, "messages": self.msg(),
                "tools": [{"type": "function", "function": {"name": "exec", "parameters": {}}}]}
        req = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
        self.assertEqual(SEEN[0]["temperature"], 0.3)

    # ---- typing keeper
    def _fresh_keeper(self):
        import time as _t
        sent = []
        k = self.mod.TypingKeeper(lambda: sent.append(1), interval=0.05, max_idle=5, linger=0.05)
        self.mod.TYPING = k
        self.addCleanup(setattr, self.mod, "TYPING", None)
        return k, sent, _t

    def test_typing_stays_on_across_tool_calls_and_stops_after_final_answer(self):
        k, sent, t = self._fresh_keeper()
        SCRIPT.append(sse(*tool_chunks([(0, "c1", "exec", '{"cmd":"sleep 600"}')])))
        self.call(self.msg())                      # model asks for a tool -> tool runs -> typing must stay on
        wait(0.4)
        self.assertTrue(k.is_on())
        self.assertGreaterEqual(len(sent), 3)
        SCRIPT.append(sse(*text_chunks("Final answer. " + "x" * 50)))
        self.call([{"role": "tool", "content": "ok", "tool_call_id": "c1"}])
        wait(0.5)
        self.assertFalse(k.is_on())
        n = len(sent)
        wait(0.3)
        self.assertEqual(len(sent), n)             # really stopped

    def test_typing_stops_after_max_idle(self):
        k = self.mod.TypingKeeper(lambda: None, interval=0.05, max_idle=0.2, linger=0.05)
        k.touch()
        self.assertTrue(k.is_on())
        wait(0.4)
        self.assertFalse(k.is_on())

    def test_typing_not_started_for_plain_non_agent_calls(self):
        k, sent, t = self._fresh_keeper()
        SCRIPT.append(sse(*text_chunks("summary")))
        self.call(self.msg("summarize"), tools=False)
        self.assertFalse(k.is_on())
        self.assertEqual(sent, [])


    # ---- system protection
    def _tc(self, name, **a):
        return [{"function": {"name": name, "arguments": json.dumps(a)}}]

    def test_protection_blocks_system_changes_and_allows_workspace_work(self):
        fv = self.mod.find_violation
        for name, a in [("write_file", {"path": "/app/start.sh"}), ("edit_file", {"path": "~/.picoclaw/config.json"}),
                        ("exec", {"command": "sed -i s/a/b/ /app/llm_proxy.py"}), ("exec", {"command": "echo x > /app/start.sh"}),
                        ("exec", {"command": "tailscale serve reset"}), ("exec", {"command": "sudo tailscale up"}),
                        ("exec", {"command": "pkill -f llm_proxy"}), ("exec", {"command": "rm -rf /root/.picoclaw"}),
                        ("exec", {"command": "cp x /usr/local/bin/y"}), ("exec", {"command": "tailscaled &"})]:
            self.assertTrue(fv(self._tc(name, **a)), (name, a))
        for name, a in [("write_file", {"path": "/root/.picoclaw/workspace/dashboards/a/index.html"}),
                        ("exec", {"command": "cat /app/start.sh | head"}), ("exec", {"command": "tailscale status"}),
                        ("exec", {"command": "hdash start live /root/.picoclaw/workspace/dashboards/live"}),
                        ("exec", {"command": "python3 /app/notify.py hi"}), ("exec", {"command": "pip install requests"}),
                        ("write_file", {"path": "/root/.picoclaw/workspace/app/x.py"})]:
            self.assertIsNone(fv(self._tc(name, **a)), (name, a))

    def test_blocked_tool_call_is_bounced_back_and_model_corrects_itself(self):
        SCRIPT.extend([sse(*tool_chunks([(0, "c1", "write_file", '{"path":"/app/start.sh","content":"x"}')])),
                       sse(*tool_chunks([(0, "c2", "exec", '{"command":"hdash start live /root/.picoclaw/workspace/dashboards/live"}')]))])
        _, raw = self.call(self.msg())
        self.assertEqual(len(SEEN), 2)
        self.assertIn("BLOCKED", SEEN[1]["messages"][-1]["content"])
        self.assertIn("hdash start", json.loads(raw)["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])

    def test_blocked_call_never_reaches_picoclaw_when_retries_run_out(self):
        bad = sse(*tool_chunks([(0, "c1", "exec", '{"command":"tailscale down"}')]))
        SCRIPT.extend([bad, bad, bad, bad])
        _, raw = self.call(self.msg())
        msg = json.loads(raw)["choices"][0]["message"]
        self.assertFalse(msg.get("tool_calls"))
        self.assertIn("⛔", msg["content"])


    # ---- PicoClaw ends the turn after send_file: explanation must be sent BEFORE it
    LONG = "Root cause: the URL had a literal ${e}. Changed: build_url(). Tested: unit test passed. Run: python nvs_check.py " + "x" * 40
    NAMES = ("exec", "message", "send_file")

    def test_send_file_without_prior_message_is_bounced_and_model_adds_explanation(self):
        SCRIPT.extend([sse(*tool_chunks([(0, "c1", "send_file", '{"path":"/w/a.py"}')])),
                       sse(*tool_chunks([(0, "c2", "message", json.dumps({"content": self.LONG})),
                                         (1, "c3", "send_file", '{"path":"/w/a.py"}')]))])
        _, raw = self.call(self.WORKED, tool_names=self.NAMES)
        self.assertEqual(len(SEEN), 2)
        self.assertIn("ENDS right after send_file", SEEN[1]["messages"][-1]["content"])
        calls = json.loads(raw)["choices"][0]["message"]["tool_calls"]
        self.assertEqual([c["function"]["name"] for c in calls], ["message", "send_file"])

    WORKED = [{"role": "user", "content": "fix it"},
              {"role": "assistant", "content": None, "tool_calls": [
                  {"id": "w1", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}]},
              {"role": "tool", "tool_call_id": "w1", "content": "File written"}]

    def test_plain_send_me_the_file_request_needs_no_report(self):
        SCRIPT.append(sse(*tool_chunks([(0, "c1", "send_file", '{"path":"/w/a.log"}')])))
        self.call(self.msg("send me a.log"), tool_names=self.NAMES)
        self.assertEqual(len(SEEN), 1)

    def test_message_then_send_file_in_one_response_is_accepted(self):
        SCRIPT.append(sse(*tool_chunks([(0, "c2", "message", json.dumps({"content": self.LONG})),
                                        (1, "c3", "send_file", '{"path":"/w/a.py"}')])))
        self.call(self.msg(), tool_names=self.NAMES)
        self.assertEqual(len(SEEN), 1)

    def test_send_file_accepted_when_explanation_was_sent_earlier_in_the_turn(self):
        hist = [{"role": "user", "content": "fix it"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "m1", "type": "function", "function": {"name": "message", "arguments": json.dumps({"content": self.LONG})}}]},
                {"role": "tool", "tool_call_id": "m1", "content": "sent"}]
        SCRIPT.append(sse(*tool_chunks([(0, "c3", "send_file", '{"path":"/w/a.py"}')])))
        self.call(hist, tool_names=self.NAMES)
        self.assertEqual(len(SEEN), 1)

    def test_typing_stops_when_turn_ends_with_send_file(self):
        k, sent, t = self._fresh_keeper()
        SCRIPT.append(sse(*tool_chunks([(0, "c2", "message", json.dumps({"content": self.LONG})),
                                        (1, "c3", "send_file", '{"path":"/w/a.py"}')])))
        self.call(self.msg(), tool_names=self.NAMES)
        wait(0.5)
        self.assertFalse(k.is_on())

    def test_quick_tools_get_short_typing_idle_and_exec_gets_long(self):
        k, sent, t = self._fresh_keeper()
        SCRIPT.append(sse(*tool_chunks([(0, "c1", "read_file", '{"path":"/w/a"}')])))
        self.call(self.msg(), tool_names=("read_file",))
        self.assertEqual(k._idle, self.mod.QUICK_TOOL_IDLE)
        SCRIPT.append(sse(*tool_chunks([(0, "c1", "exec", '{"command":"sleep 100"}')])))
        self.call(self.msg(), tool_names=("exec",))
        self.assertEqual(k._idle, k.max_idle)

    def test_streaming_client_gets_keepalive_pings_while_reply_is_validated(self):
        old = self.mod.KEEPALIVE_SECS
        self.mod.KEEPALIVE_SECS = 0.2
        self.addCleanup(setattr, self.mod, "KEEPALIVE_SECS", old)
        SCRIPT.append(("slow_alive", text_chunks("done")))
        _, raw = self.call(self.msg(), stream=True)
        text = raw.decode()
        self.assertIn(": keep-alive", text)
        self.assertLess(text.index(": keep-alive"), text.index('"content": "done"'))
        self.assertTrue(text.rstrip().endswith("[DONE]"))

    def test_streaming_client_upstream_error_after_keepalive_is_reported_in_band(self):
        old = self.mod.KEEPALIVE_SECS
        self.mod.KEEPALIVE_SECS = 0.1
        self.addCleanup(setattr, self.mod, "KEEPALIVE_SECS", old)
        SCRIPT.extend([("status", 500)] * 6)
        try:                                              # fast failure: plain HTTP 500 passed through (no keep-alive yet)
            _, raw = self.call(self.msg(), stream=True)
            self.assertIn(b"[DONE]", raw)                 # ...or, if pings already opened the stream, an in-band error
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 500)


    # ---- long jobs must not stop by accident
    def _task_ws(self, text, age=0):
        import tempfile, time as _time
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "tasks"))
        f = os.path.join(d, "tasks", "current.md")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(text)
        if age:
            os.utime(f, (_time.time() - age, _time.time() - age))
        old = self.mod.WORKSPACE
        self.mod.WORKSPACE = d
        self.addCleanup(setattr, self.mod, "WORKSPACE", old)

    MID = [{"role": "user", "content": "build it"},
           {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}]},
           {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    SUMMARY = "Here is a summary of what I found in the logs so far, all three files were inspected carefully."

    def test_text_reply_while_task_has_open_requirements_is_re_asked(self):
        self._task_ws("# Task\nStatus: IN PROGRESS\n- [x] R1 read logs\n- [ ] R2 fix parser\n")
        SCRIPT.extend([sse(*text_chunks(self.SUMMARY)), sse(*tool_chunks([(0, "c2", "exec", '{"command":"ls"}')]))])
        _, raw = self.call(self.MID)
        self.assertEqual(len(SEEN), 2)
        self.assertIn("R2 fix parser", SEEN[1]["messages"][-1]["content"])
        self.assertTrue(json.loads(raw)["choices"][0]["message"]["tool_calls"])

    def test_no_re_ask_when_done_blocked_stale_question_or_fresh_user_message(self):
        for text, age, msgs in [("# Task\nStatus: DONE\n- [ ] R1 x\n", 0, self.MID),
                                ("# Task\nStatus: BLOCKED (geo-block)\n- [ ] R1 x\n", 0, self.MID),
                                ("# Task\nStatus: IN PROGRESS\n- [ ] R1 x\n", 99999, self.MID),
                                ("# Task\nStatus: IN PROGRESS\n- [ ] R1 x\n", 0, self.msg("hi")) ]:
            SEEN.clear()
            self._task_ws(text, age)
            SCRIPT.append(sse(*text_chunks(self.SUMMARY)))
            self.call(msgs)
            self.assertEqual(len(SEEN), 1, (text, age))
        SEEN.clear()
        self._task_ws("# Task\nStatus: IN PROGRESS\n- [ ] R1 x\n")
        SCRIPT.append(sse(*text_chunks("I need the password for the server to go on. Which one should I use?")))
        self.call(self.MID)
        self.assertEqual(len(SEEN), 1)                     # asking the user a question is a legitimate stop

    def test_persistent_500_gives_up_early_but_429_keeps_retrying(self):
        old = (self.mod.HTTP_RETRIES, self.mod.SERVER_ERR_RETRIES)
        self.mod.HTTP_RETRIES, self.mod.SERVER_ERR_RETRIES = 20, 2
        self.addCleanup(lambda: setattr(self.mod, "HTTP_RETRIES", old[0]) or setattr(self.mod, "SERVER_ERR_RETRIES", old[1]))
        SCRIPT.extend([("status", 500)] * 6)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.call(self.msg())
        self.assertEqual(cm.exception.code, 500)
        self.assertEqual(len(SEEN), 3)                   # 1 try + 2 retries, not 20

    def test_long_burst_of_429_is_survived_with_default_style_retries(self):
        old = self.mod.HTTP_RETRIES
        self.mod.HTTP_RETRIES = 10
        self.addCleanup(setattr, self.mod, "HTTP_RETRIES", old)
        SCRIPT.extend([("status", 429)] * 8 + [sse(*text_chunks("finally ok ✅"))])
        _, raw = self.call(self.msg())
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "finally ok ✅")
        self.assertEqual(len(SEEN), 9)

    def test_protection_has_no_false_positives_on_reading_and_workspace_work(self):
        fv = self.mod.find_violation
        for cmd in ["cp /usr/share/zoneinfo/UTC /root/.picoclaw/workspace/tz", "cat /etc/os-release > /tmp/os.txt",
                    "python3 -c \"print(open('/etc/hostname').read())\"", "find /usr -name '*.py' | head",
                    "curl -s https://example.com/usr/lib -o /tmp/x", "cd /root/.picoclaw/workspace && sed -i s/a/b/ scripts/x.py",
                    "echo 'see /etc/hosts' > /root/.picoclaw/workspace/notes.txt", "du -sh /usr/lib | tail -1"]:
            self.assertIsNone(fv(self._tc("exec", command=cmd)), cmd)
        for cmd in ["cd /app && sed -i s/a/b/ start.sh", "cd /app; echo x > start.sh", "mv /app/start.sh /tmp/",
                    "python3 -c \"open('/app/start.sh','w').write('x')\"", "curl -o /usr/local/bin/x http://a/b", "echo x >> /etc/hosts"]:
            self.assertTrue(fv(self._tc("exec", command=cmd)), cmd)


    def test_upstream_error_is_passed_through_after_retries(self):
        SCRIPT.extend([("status", 500)] * 4)
        try:
            self.call(self.msg())
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 500)
        self.assertEqual(len(SEEN), 4)

    def test_slow_but_alive_backend_with_keepalive_is_not_cut(self):
        SCRIPT.append(("slow_alive", text_chunks("slow but fine ✅")))
        _, raw = self.call(self.msg())
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "slow but fine ✅")
        self.assertEqual(len(SEEN), 1)

    def test_504_is_retried_only_timeout_retries_times(self):
        SCRIPT.extend([("status", 504)] * 3)
        try:
            self.call(self.msg())
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 504)
        self.assertEqual(len(SEEN), 2)          # 1 try + LLM_TIMEOUT_RETRIES(1), not 4 like for 500

    def test_504_then_ok(self):
        SCRIPT.extend([("status", 504), sse(*text_chunks("back ✅"))])
        _, raw = self.call(self.msg())
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "back ✅")

    def test_hung_backend_is_cut_and_retried(self):
        import time as _t
        SCRIPT.extend([("hang", 3.0), sse(*text_chunks("second model ✅"))])
        t0 = _t.time()
        _, raw = self.call(self.msg())
        self.assertLess(_t.time() - t0, 2.5)    # cut after ~0.6s, did not wait the 3s hang
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "second model ✅")
        self.assertEqual(len(SEEN), 2)

    def test_stream_that_goes_silent_midway_is_retried(self):
        SCRIPT.extend([("stall", 3.0), sse(*text_chunks("complete ✅"))])
        _, raw = self.call(self.msg())
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "complete ✅")
        self.assertEqual(len(SEEN), 2)

    def test_hang_twice_gives_clean_502(self):
        SCRIPT.extend([("hang", 3.0), ("hang", 3.0)])
        try:
            self.call(self.msg())
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 502)
        self.assertEqual(len(SEEN), 2)

    def test_upstream_plain_json_reply_also_guarded(self):
        SCRIPT.extend([
            ("json", {"model": "m", "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]}),
            ("json", {"model": "m", "choices": [{"index": 0, "message": {"role": "assistant", "content": "fine ✅"}, "finish_reason": "stop"}]}),
        ])
        _, raw = self.call(self.msg())
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "fine ✅")


if __name__ == "__main__":
    unittest.main()
