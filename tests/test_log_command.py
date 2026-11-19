"""Tests for the Telegram /log command (log_command.py + its hook in llm_proxy.py). No network. Run:
python3 -m unittest -v tests.test_log_command"""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import log_command  # noqa: E402


def chat(last_role="user", text="/log"):
    return {"model": "m", "messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        {"role": last_role, "content": text},
    ], "tools": [{"type": "function", "function": {"name": "exec"}}]}


class DetectTests(unittest.TestCase):
    def test_matches_only_a_bare_log_command_as_last_user_message(self):
        self.assertTrue(log_command.is_log_command(chat()))
        self.assertTrue(log_command.is_log_command(chat(text="  /LOG ")))
        self.assertTrue(log_command.is_log_command(chat(text="/log@my_bot")))
        self.assertTrue(log_command.is_log_command(chat(text="/logs")))
        for bad in ("/log please", "show /log", "/login", "log", ""):
            self.assertFalse(log_command.is_log_command(chat(text=bad)), bad)

    def test_tool_result_or_assistant_message_never_matches(self):
        self.assertFalse(log_command.is_log_command(chat(last_role="tool")))
        self.assertFalse(log_command.is_log_command(chat(last_role="assistant")))
        self.assertFalse(log_command.is_log_command({"messages": []}))
        self.assertFalse(log_command.is_log_command({}))

    def test_content_parts_and_disable_switch(self):
        parts = {"messages": [{"role": "user", "content": [{"type": "text", "text": "/log"}]}]}
        self.assertTrue(log_command.is_log_command(parts))
        os.environ["LOG_COMMAND"] = "0"
        try:
            self.assertFalse(log_command.is_log_command(chat()))
        finally:
            del os.environ["LOG_COMMAND"]


class RedactTests(unittest.TestCase):
    def test_secrets_are_removed(self):
        os.environ["MY_TEST_API_KEY"] = "supersecretvalue123"
        try:
            rx = log_command._secret_regex()
            cases = [
                b"POST https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage",
                b"Authorization: Bearer abcdefghijklmnop12345",
                b"key=sk-abcdefghijklmnopqrstuv",
                b'{"api_key": "xyz123456789"}',
                b"value supersecretvalue123 here",
            ]
            for line in cases:
                out = log_command.redact(line, rx)
                self.assertIn(b"<redacted>", out, line)
            joined = b" ".join(log_command.redact(c, rx) for c in cases)
            for leak in (b"AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw", b"abcdefghijklmnop12345",
                         b"sk-abcdefghijklmnopqrstuv", b"xyz123456789", b"supersecretvalue123"):
                self.assertNotIn(leak, joined)
            self.assertEqual(log_command.redact(b"normal log line 42\n", rx), b"normal log line 42\n")
        finally:
            del os.environ["MY_TEST_API_KEY"]


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.a = os.path.join(self.dir, "picoclaw.log")
        self.b = os.path.join(self.dir, "server.log")
        with open(self.a, "w") as fh:
            fh.write("".join(f"line {i}\n" for i in range(1000)))
        with open(self.b, "w") as fh:
            fh.write("boot ok\ncall https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/x\n")
        self._patterns = log_command.LOG_PATTERNS
        log_command.LOG_PATTERNS = (os.path.join(self.dir, "*.log"),)

    def tearDown(self):
        log_command.LOG_PATTERNS = self._patterns

    def test_zip_has_info_and_redacted_files(self):
        files = log_command.collect_files()
        self.assertEqual({os.path.basename(p) for p, _ in files}, {"picoclaw.log", "server.log"})
        dest = os.path.join(self.dir, "o.zip")
        log_command.build_zip(files, dest)
        with zipfile.ZipFile(dest) as zf:
            names = zf.namelist()
            self.assertIn("00_info.txt", names)
            server = [n for n in names if n.endswith("server.log")][0].encode()
            text = zf.read(server.decode()).decode()
            self.assertIn("boot ok", text)
            self.assertNotIn("AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw", text)
            pico = zf.read([n for n in names if n.endswith("picoclaw.log")][0]).decode()
            self.assertIn("line 999", pico)
            self.assertIn("line 0\n", pico)                      # complete file, nothing cut

    def test_tail_limit_keeps_only_the_end(self):
        files = log_command.collect_files()
        dest = os.path.join(self.dir, "t.zip")
        log_command.build_zip(files, dest, tail_limit=200)
        with zipfile.ZipFile(dest) as zf:
            pico = zf.read([n for n in zf.namelist() if n.endswith("picoclaw.log")][0]).decode()
        self.assertIn("line 999", pico)
        self.assertNotIn("line 1\n", pico)
        self.assertIn("older part cut", pico)

    def test_run_sends_zip_to_every_id_and_cleans_up(self):
        sent = []
        orig = log_command.send_document

        def fake(token, chat_id, path, caption, timeout=180):
            with zipfile.ZipFile(path) as zf:                    # zip must be valid at send time
                sent.append((token, chat_id, len(zf.namelist()), caption))
            return True, ""
        log_command.send_document = fake
        try:
            reply = log_command.run(token="T", ids=["11", "22"])
        finally:
            log_command.send_document = orig
        self.assertTrue(reply.startswith("✅"), reply)
        self.assertEqual([(s[0], s[1]) for s in sent], [("T", "11"), ("T", "22")])
        self.assertEqual(sent[0][2], 3)                          # info + 2 logs
        self.assertIn("picoclaw.log", sent[0][3])
        leftovers = [f for f in os.listdir(tempfile.gettempdir()) if f.startswith("hermes-logs-")]
        self.assertEqual(leftovers, [])

    def test_run_reports_failures_and_missing_config(self):
        orig = log_command.send_document
        log_command.send_document = lambda *a, **k: (False, "chat not found")
        try:
            self.assertIn("chat not found", log_command.run(token="T", ids=["11"]))
        finally:
            log_command.send_document = orig
        self.assertIn("सेट नाहीत", log_command.run(token="", ids=[]))


class ProxyHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()                                              # nothing listens here = "upstream is down"
        os.environ["PICOCLAW_UPSTREAM_API_BASE"] = f"http://127.0.0.1:{dead_port}/v1"
        os.environ["LLM_HTTP_RETRIES"] = "0"
        import llm_proxy
        cls.llm_proxy = llm_proxy
        cls.px = ThreadingHTTPServer(("127.0.0.1", 0), llm_proxy.GuardProxyHandler)
        cls.port = cls.px.server_address[1]
        threading.Thread(target=cls.px.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.px.shutdown()

    def post(self, payload):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=15).read().decode()

    def with_fake_run(self, text="✅ done"):
        calls = []
        orig = self.llm_proxy.log_command.run
        self.llm_proxy.log_command.run = lambda *a, **k: (calls.append(1), text)[1]
        self.addCleanup(setattr, self.llm_proxy.log_command, "run", orig)
        return calls

    def test_non_stream_reply_without_touching_upstream(self):
        calls = self.with_fake_run("✅ Logs पाठवले")
        body = json.loads(self.post(chat()))
        self.assertEqual(body["choices"][0]["message"]["content"], "✅ Logs पाठवले")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(len(calls), 1)

    def test_stream_reply(self):
        self.with_fake_run("✅ ok")
        payload = chat()
        payload["stream"] = True
        raw = self.post(payload)
        self.assertIn("data: [DONE]", raw)
        chunks = [json.loads(x[6:]) for x in raw.splitlines() if x.startswith("data: {")]
        text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices"))
        self.assertEqual(text, "✅ ok")

    def test_normal_chat_is_not_intercepted(self):
        calls = self.with_fake_run()
        try:
            self.post(chat(text="hello there"))        # goes to the dead upstream -> error, but never /log
        except Exception:
            pass
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
