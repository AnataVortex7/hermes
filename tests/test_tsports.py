import importlib.machinery, importlib.util, os, socket, subprocess, sys, unittest

PATH = os.path.join(os.path.dirname(__file__), "..", "bin", "tsports")


def load():
    loader = importlib.machinery.SourceFileLoader("tsports", PATH)
    spec = importlib.util.spec_from_loader("tsports", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Run:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append(a)
        return subprocess.CompletedProcess(a, 0, "", "")


class TsPortsTests(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def listen(self, port=0, host="127.0.0.1"):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(1)
        self.addCleanup(s.close)
        return s.getsockname()[1]

    def test_listeners_sees_real_sockets_and_forwards_to_loopback(self):
        p = self.listen(0, "0.0.0.0")
        ls = self.m.listeners()
        self.assertEqual(ls.get(p), "127.0.0.1")

    def test_wanted_excludes_system_and_ephemeral_ports(self):
        w = self.m.wanted({18790: "127.0.0.1", 18799: "127.0.0.1", 1055: "127.0.0.1", 40000: "127.0.0.1", 22: "127.0.0.1"})
        self.assertEqual(set(w), {18790})

    def test_sync_publishes_new_ports_and_unpublishes_gone_ones_after_3_scans(self):
        run = Run()
        self.m.listeners = lambda *a, **k: {18800: "127.0.0.1", 8123: "127.0.0.1"}
        cur, missing = {}, {}
        cur = self.m.sync(run=run, current=cur, missing=missing)
        self.assertEqual(set(cur), {18800, 8123})
        self.assertIn(("serve", "--bg", "--tcp=18800", "tcp://127.0.0.1:18800"), run.calls)
        n = len(run.calls)
        self.m.sync(run=run, current=cur, missing=missing)
        self.assertEqual(len(run.calls), n)                      # nothing new -> no extra tailscale calls
        self.m.listeners = lambda *a, **k: {18800: "127.0.0.1"}  # 8123 stopped
        for _ in range(2):
            cur = self.m.sync(run=run, current=cur, missing=missing)
        self.assertIn(8123, cur)                                 # not yet (needs 3 misses)
        cur = self.m.sync(run=run, current=cur, missing=missing)
        self.assertNotIn(8123, cur)
        self.assertIn(("serve", "--tcp=8123", "off"), run.calls)

    def test_foreign_entries_are_never_unpublished(self):
        run = Run()
        self.m.listeners = lambda *a, **k: {}
        cur = {9999: "100.64.0.9:9999"}
        for _ in range(5):
            cur = self.m.sync(run=run, current=cur, missing={})
        self.assertIn(9999, cur)
        self.assertEqual(run.calls, [])

    def test_index_page_lists_ports_with_urls(self):
        html = self.m.render_index({18800: "127.0.0.1:18800", 8123: "127.0.0.1:8123"})
        self.assertIn("http://hermes:18800/", html)
        self.assertIn("PicoClaw web UI", html)
        self.assertIn("http://hermes:8123/", html)


if __name__ == "__main__":
    unittest.main()
