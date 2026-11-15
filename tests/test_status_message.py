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

    def test_telegram_errors_never_raise(self):
        def boom(method, **p):
            raise RuntimeError("network down")
        s = m.StatusMessage(boom, ["1"], min_interval=0.0, linger=0.1)
        s.begin()
        s.tools(["exec"])
        s.finish()
        _sleep(0.5)


if __name__ == "__main__":
    unittest.main()
