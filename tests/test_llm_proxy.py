"""Self-contained tests for llm_proxy.py (mock upstream, no network). Run:  python3 -m unittest -v tests.test_llm_proxy"""
import json
import os
import sys
import threading
import unittest
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

    def call(self, messages, tools=True, stream=False):
        body = {"model": "x", "messages": messages, "stream": stream}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": "exec", "parameters": {}}}]
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

    def test_stalled_only_retried_once(self):
        SCRIPT.extend([sse(*text_chunks("Let me check:")), sse(*text_chunks("Let me check again:"))])
        _, raw = self.call(self.msg())
        self.assertEqual(len(SEEN), 2)   # one retry, then the reply is passed on as is

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
