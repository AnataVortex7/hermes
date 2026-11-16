import os
import sys
import threading
import time
import unittest

import importlib.util

# Load a private copy: importing "llm_proxy" here would put it in sys.modules before test_llm_proxy.py sets its env.
_spec = importlib.util.spec_from_file_location(
    "llm_proxy_status_under_test",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "llm_proxy.py"))
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, method, **p):
        with self.lock:
            self.calls.append((method, p))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 7}}
        return {"ok": True}

    def texts(self):
        return [p.get("text") for _, p in self.calls if "text" in p]

    def methods(self):
        return [c[0] for c in self.calls]


_sleep = time.sleep   # test_llm_proxy.py replaces time.sleep globally with a no-op


def wait_for(cond, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        _sleep(0.05)
    return False


class StatusMessageTests(unittest.TestCase):
    def make(self, **kw):
        tg = FakeTelegram()
        s = m.StatusMessage(tg, ["42"], min_interval=0.0, linger=0.2, **kw)
        return tg, s

    def test_short_names(self):
        self.assertEqual(m.short_tool_name("mcp__srv__web_search"), "web_search")
        self.assertEqual(m.short_tool_name("exec"), "exec")
        self.assertLessEqual(len(m.short_tool_name("x" * 80)), 24)

    def test_shows_tool_then_deletes_on_finish(self):
        tg, s = self.make()
        s.begin()
        self.assertTrue(wait_for(lambda: "sendMessage" in tg.methods()))
        s.tools(["exec"])
        self.assertTrue(wait_for(lambda: any("🔧 exec" in (x or "") for x in tg.texts())))
        s.begin()                       # next LLM request: back to thinking, remembers last tool
        self.assertTrue(wait_for(lambda: any("Thinking" in (x or "") and "last: exec" in x for x in tg.texts())))
        s.finish()
        self.assertTrue(wait_for(lambda: "deleteMessage" in tg.methods()))

    def test_new_request_during_linger_keeps_same_message(self):
        tg, s = self.make()
        s.begin()
        wait_for(lambda: "sendMessage" in tg.methods())
        s.finish()
        s.begin()                       # e.g. a sub-agent finished, main job continues
        _sleep(0.6)
        self.assertNotIn("deleteMessage", tg.methods())
        self.assertEqual(tg.methods().count("sendMessage"), 1)

    def test_stalled_turn_becomes_warning_and_stays(self):
        flag = {"dead": False}
        tg, s = self.make(stall_after=lambda: flag["dead"])
        s.begin()
        s.tools(["exec"])
        wait_for(lambda: "sendMessage" in tg.methods())
        flag["dead"] = True
        self.assertTrue(wait_for(lambda: any("activity नाही" in (x or "") for x in tg.texts())))
        self.assertNotIn("deleteMessage", tg.methods())

    # ---- time since the last tool call
    def test_ago_has_minute_resolution(self):
        a = m.StatusMessage.ago
        self.assertEqual((a(0), a(59), a(60), a(125), a(3599), a(3600), a(3900), a(-5)), ("<1m", "<1m", "1m", "2m", "59m", "1h 0m", "1h 5m", "<1m"))

    def test_no_timing_line_before_the_first_tool_call(self):
        t = m._Turn()
        self.assertNotIn("⏱", m.StatusMessage.render(t))

    def test_timing_while_thinking_and_while_a_tool_runs(self):
        now = time.time()
        t = m._Turn()
        t.started, t.steps = now - 16 * 60, 21
        t.last_tools, t.last_tool_at, t.phase_at = ["edit_file — editing MEMORY.md"], now - 130, now - 20
        text = m.StatusMessage.render(t, now)
        self.assertTrue(text.endswith("Step 21 · 16m\n⏱ last tool call: 2m ago"), text)       # LLM wait < 1 min: not shown
        t.phase_at = now - 75
        self.assertTrue(m.StatusMessage.render(t, now).endswith("⏱ last tool call: 2m ago · waiting for the LLM: 1m"))
        t.phase, t.names, t.last_tool_at = "tool", ["exec"], now - 3900
        self.assertTrue(m.StatusMessage.render(t, now).endswith("⏱ tool running: 1h 5m"))
        t.last_tool_at = now - 5
        self.assertTrue(m.StatusMessage.render(t, now).endswith("⏱ tool running: <1m"))

    def test_text_changes_only_once_a_minute(self):
        now = time.time()
        t = m._Turn()
        t.started, t.last_tool_at, t.phase, t.names = now - 600, now - 70, "tool", ["exec"]
        self.assertEqual(m.StatusMessage.render(t, now), m.StatusMessage.render(t, now + 20))      # 70s -> 90s: still "1m"

    def test_live_message_shows_tool_running_then_last_tool_call(self):
        tg, s = self.make()
        s.begin()
        s.tools(["exec"])
        self.assertTrue(wait_for(lambda: any("⏱ tool running: <1m" in (x or "") for x in tg.texts())))
        s.begin()
        self.assertTrue(wait_for(lambda: any("⏱ last tool call: <1m ago" in (x or "") for x in tg.texts())))
        s.finish()

    def test_stalled_warning_says_how_long_ago_the_last_tool_call_was(self):
        flag = {"dead": False}
        tg, s = self.make(stall_after=lambda: flag["dead"])
        s.begin()
        s.tools(["exec"])
        wait_for(lambda: "sendMessage" in tg.methods())
        flag["dead"] = True
        self.assertTrue(wait_for(lambda: any("last tool call <1m ago" in (x or "") for x in tg.texts())))


    def test_telegram_errors_never_raise(self):
        def boom(method, **p):
            raise RuntimeError("network down")
        s = m.StatusMessage(boom, ["1"], min_interval=0.0, linger=0.1)
        s.begin()
        s.tools(["exec"])
        s.finish()
        _sleep(0.5)


class DescribeTests(unittest.TestCase):
    def test_commands_become_short_phrases(self):
        d = m.describe_command
        self.assertEqual(d("cd /root/work && pip install --no-cache-dir pandas openpyxl"), "installing pandas, openpyxl")
        self.assertEqual(d("cd x && python3 -u scrape.py --all 2>&1 | tail -20"), "running scrape.py")
        self.assertEqual(d("curl -s -H 'Authorization: Bearer abc123' https://api.example.com/v1/x?key=1"), "downloading api.example.com")
        self.assertEqual(d("python3 -m pytest tests -q"), "running tests")
        self.assertEqual(d("sleep 30"), "waiting 30s")
        self.assertEqual(d("grep -rn 'TODO' ."), "searching TODO")

    def test_tools_and_secrets(self):
        self.assertEqual(m.describe_tool("write_file", {"path": "/root/.picoclaw/workspace/scripts/app.py", "content": "x"}), "writing app.py")
        self.assertEqual(m.describe_tool("mcp__s__web_fetch", {"url": "https://docs.example.com/a/b?token=zzz"}), "opening docs.example.com")
        self.assertEqual(m.describe_tool("web_search", {"query": "koyeb ram limit"}), "searching “koyeb ram limit”")
        out = m.describe_command("export K=1 && mytool --password hunter2 AKIAABCDEFGHIJKLMNOPQRSTUVWXYZ123456")
        self.assertNotIn("hunter2", out)
        self.assertNotIn("AKIAABCDEFGHIJKLMNOPQRSTUVWXYZ123456", out)
        self.assertEqual(m.describe_tool("weird", None), "")

    def test_why_is_first_sentence_only(self):
        self.assertEqual(m.describe_why("Now I install pandas to parse the CSV. Then I will plot it."), "Now I install pandas to parse the CSV.")
        self.assertEqual(m.describe_why(""), "")
        self.assertEqual(m.describe_why("[hermes-guard] nudge text here"), "")


class StatusDetailTests(unittest.TestCase):
    def test_render_shows_what_and_why(self):
        t = m._Turn()
        t.phase, t.names, t.details, t.why, t.steps = "tool", ["exec"], ["installing pandas"], "CSV parse karayla", 27
        t.started = time.time() - 8 * 60
        text = m.StatusMessage.render(t)
        self.assertEqual(text, "⚙️ Agent is working…\n🔧 exec — installing pandas\n💡 CSV parse karayla\nStep 27 · 8m")
        t.phase, t.last_tools = "thinking", m.StatusMessage._labels(t)
        self.assertIn("🧠 Thinking… (last: exec — installing pandas)", m.StatusMessage.render(t))


if __name__ == "__main__":
    unittest.main()
