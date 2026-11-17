import base64, http.server, json, os, socket, sys, threading, time, unittest, urllib.error, urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import keep_alive  # noqa: E402
import restore_progress  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


SEEN = []


class FakeUI(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        SEEN.append((self.command, self.path, body))
        if self.path.startswith("/slow"):
            time.sleep(1.0)
        if self.path in ("/", "/index.html"):
            data, ctype = b'<html><script src="/assets/app.js"></script><a href="/x">x</a><a href="//cdn/y">y</a></html>', "text/html"
        else:
            data, ctype = json.dumps({"path": self.path, "body": body.decode()}).encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _reply


class KeepAliveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        http.server.ThreadingHTTPServer.daemon_threads = True
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), keep_alive.PingHandler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.ui_port = free_port()
        cls.ui = http.server.ThreadingHTTPServer(("127.0.0.1", cls.ui_port), FakeUI)
        threading.Thread(target=cls.ui.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.ui.shutdown()

    def setUp(self):
        SEEN.clear()
        self.addCleanup(setattr, keep_alive, "PICOCLAW_UI_PORT", keep_alive.PICOCLAW_UI_PORT)
        self.addCleanup(setattr, keep_alive, "UI_BASIC_AUTH", keep_alive.UI_BASIC_AUTH)
        keep_alive.PICOCLAW_UI_PORT = self.ui_port

    def get(self, path, data=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers or {})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode(), r.headers

    def test_health_root_and_stats_json(self):
        st, body, _ = self.get("/")
        self.assertEqual((st, body), (200, "Hermes Agent is Alive and Running!"))
        st, body, _ = self.get("/api/stats")
        d = json.loads(body)
        for key in ("memory", "cpu", "disk", "uptime", "processes", "platform", "startup"):
            self.assertIn(key, d)
        self.assertIn("source", d["memory"])
        self.assertIn("limit_cores", d["cpu"])

    def test_memory_page_is_served_and_links_to_picoclaw_not_hermes(self):
        _, html, _ = self.get("/memory")
        self.assertIn('href="/picoclaw"', html)
        self.assertNotIn("/dashboard", html)
        self.assertNotIn("Koyeb Free Tier", html)

    def test_hermes_dashboard_route_is_gone(self):
        self.assertFalse(hasattr(keep_alive, "DASHBOARD_PORT"))
        _, body, _ = self.get("/dashboard")
        self.assertEqual(body, "Hermes Agent is Alive and Running!")       # plain fallback, nothing is proxied to 9119

    def test_picoclaw_route_shows_status_page_when_no_ui_runs(self):
        keep_alive.PICOCLAW_UI_PORT = free_port()                            # nothing listens there
        _, html, _ = self.get("/picoclaw")
        self.assertIn("PicoClaw", html)
        self.assertIn("/picoclaw/status.json", html)
        _, js, _ = self.get("/picoclaw/status.json")
        s = json.loads(js)
        for key in ("gateway_listening", "ready_ok", "llm_proxy_listening", "web_ui_listening", "health", "ready"):
            self.assertIn(key, s)
        self.assertFalse(s["web_ui_listening"])
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/picoclaw/api/x")
        self.assertEqual(cm.exception.code, 503)

    def test_picoclaw_route_proxies_the_web_ui_and_rewrites_absolute_links(self):
        _, html, _ = self.get("/picoclaw")
        self.assertIn('src="/picoclaw/assets/app.js"', html)
        self.assertIn('href="/picoclaw/x"', html)
        self.assertIn('href="//cdn/y"', html)                                 # protocol-relative untouched
        _, body, _ = self.get("/picoclaw/api/ping?a=1")
        self.assertEqual(json.loads(body)["path"], "/api/ping?a=1")           # prefix stripped, query kept
        _, body, _ = self.get("/picoclaw/api/save", data=b"hello", headers={"Content-Type": "text/plain"})
        self.assertEqual(json.loads(body), {"path": "/api/save", "body": "hello"})

    def test_websocket_upgrade_is_refused_with_a_hint(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/picoclaw/ws", headers={"Upgrade": "websocket"})
        self.assertEqual(cm.exception.code, 501)

    def test_optional_basic_auth_protects_picoclaw_only(self):
        keep_alive.UI_BASIC_AUTH = "me:secret"
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/picoclaw")
        self.assertEqual(cm.exception.code, 401)
        ok = {"Authorization": "Basic " + base64.b64encode(b"me:secret").decode()}
        st, _, _ = self.get("/picoclaw", headers=ok)
        self.assertEqual(st, 200)
        self.assertEqual(self.get("/memory")[0], 200)                         # stats page stays open

    def test_slow_ui_request_does_not_block_the_health_check(self):
        t = threading.Thread(target=lambda: self.get("/picoclaw/slow"))
        t.start()
        time.sleep(0.2)
        t0 = time.time()
        self.assertEqual(self.get("/")[0], 200)
        self.assertLess(time.time() - t0, 0.6)                                # threaded server: answered while /slow sleeps
        t.join()

    # ── live restore progress on the dashboard ──
    def _use_temp_state(self):
        import tempfile
        d = tempfile.mkdtemp()
        old = restore_progress.STATE_FILE
        restore_progress.STATE_FILE = os.path.join(d, "state.json")
        self.addCleanup(setattr, restore_progress, "STATE_FILE", old)
        with keep_alive._startup_lock:
            saved = dict(keep_alive._startup_state)
        self.addCleanup(keep_alive._startup_state.update, saved)
        keep_alive._startup_state.update({"phase": "waiting", "message": "Restoring state from Google Drive..."})

    def test_api_stats_exposes_live_restore_progress(self):
        self._use_temp_state()
        restore_progress.write_state({"stage": "restoring", "message": "m", "percent": 42.5, "bytes_done": 5, "bytes_total": 10,
                                      "eta_seconds": 30, "speed_bps": 1000, "phase": 1, "phases": 2, "label": "x"})
        _, body, _ = self.get("/api/stats")
        st = json.loads(body)["startup"]
        self.assertEqual(st["phase"], "waiting")
        self.assertEqual(st["restore"]["percent"], 42.5)
        self.assertEqual(st["restore"]["eta_seconds"], 30)
        self.assertFalse(st["restore"]["stalled"])
        self.assertFalse(st["clean_start"])

    def test_stalled_restore_is_flagged(self):
        self._use_temp_state()
        restore_progress.write_state({"stage": "restoring", "percent": 10})
        st = json.load(open(restore_progress.STATE_FILE))
        st["updated"] = time.time() - 120
        json.dump(st, open(restore_progress.STATE_FILE, "w"))
        r = keep_alive.get_startup_phase()["restore"]
        self.assertTrue(r["stalled"])
        self.assertGreaterEqual(r["age_seconds"], 119)

    def test_startup_ready_hides_banner_and_survives_a_restart(self):
        self._use_temp_state()
        restore_progress.write_state({"stage": "restored", "percent": 100})
        self.assertEqual(self.get("/startup-ready")[1], "OK")
        # a fresh process (state in memory lost) must still say ready, from the file
        keep_alive._startup_state.update({"phase": "waiting"})
        self.assertEqual(keep_alive.get_startup_phase()["phase"], "ready")

    def test_startup_ready_refuses_non_local_clients(self):
        self._use_temp_state()
        h = keep_alive.PingHandler.__new__(keep_alive.PingHandler)
        h.client_address = ("203.0.113.9", 5555)
        h.path = "/startup-ready"
        sent = []
        h._send = lambda code, ctype, body, extra=None: sent.append(code)
        h.do_GET()
        self.assertEqual(sent, [403])
        self.assertEqual(keep_alive.get_startup_phase()["phase"], "waiting")

    def test_clean_start_flag_is_exposed(self):
        self._use_temp_state()
        restore_progress.update_state(stage="clean_start", clean_start=True)
        self.assertTrue(keep_alive.get_startup_phase()["clean_start"])

    def test_query_strings_do_not_break_the_routes(self):
        self.assertIn("Hermes Live Stats", self.get("/memory?x=1")[1])
        self.assertIn("memory", json.loads(self.get("/api/stats?t=123")[1]))

    def test_memory_page_has_progress_banner_and_valid_utf8(self):
        _, html, _ = self.get("/memory")
        for ident in ("banner-bar", "banner-pct", "sb-eta", "sb-speed", "sb-left", "renderStartup"):
            self.assertIn(ident, html)

    def test_page_js_has_no_lone_surrogates(self):
        keep_alive.HTML_PAGE.encode("utf-8")        # raises if a \ud83d-style half emoji sneaks in

    def test_rewrite_html_links_unit(self):
        r = keep_alive.rewrite_html_links
        self.assertEqual(r('<a href="/a">'), '<a href="/picoclaw/a">')
        self.assertEqual(r("<img src='/i.png'>"), "<img src='/picoclaw/i.png'>")
        self.assertEqual(r('<a href="/picoclaw/a">'), '<a href="/picoclaw/a">')
        self.assertEqual(r('<a href="https://x/y">'), '<a href="https://x/y">')


if __name__ == "__main__":
    unittest.main()
