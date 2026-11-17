"""Tests for backup_status.py, the dashboard backup card/button, and the sync function in start.sh (fake rclone)."""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import http.server
import socket

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import backup_status as bs  # noqa: E402
import keep_alive  # noqa: E402


class StatusModuleTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.f = os.path.join(self.dir, "status.json")
        self.trig = os.path.join(self.dir, "now")

    def test_never_before_first_backup(self):
        s = bs.summary(path=self.f, trigger=self.trig)
        self.assertEqual(s["state"], "never")
        self.assertIsNone(s["last_ok_ago_s"])
        self.assertEqual(s["alert"], "")

    def test_ok_run_is_recorded_with_age_and_counts(self):
        bs.mark_start("full", path=self.f, now=1000)
        self.assertEqual(bs.summary(now=1005, path=self.f, trigger=self.trig)["state"], "running")
        bs.mark_done("full", True, 41, path=self.f, now=1041)
        s = bs.summary(now=1101, path=self.f, trigger=self.trig)
        self.assertEqual((s["state"], s["last_ok_ago_s"], s["last_duration_s"], s["ok_count"]), ("ok", 60, 41, 1))
        self.assertEqual(s["alert"], "")
        self.assertEqual(s["history"][-1]["ok"], True)

    def test_failures_are_counted_and_alert_after_two(self):
        bs.mark_done("full", True, 10, path=self.f, now=1000)
        bs.mark_done("full", False, 5, "rclone exit 1/0: ERROR : boom", path=self.f, now=1300)
        s = bs.summary(now=1310, path=self.f, trigger=self.trig)
        self.assertEqual((s["state"], s["consecutive_failures"], s["alert"]), ("failed", 1, ""))
        self.assertIn("boom", s["last_error"])
        bs.mark_done("full", False, 5, "ERROR : boom", path=self.f, now=1600)
        s = bs.summary(now=1610, path=self.f, trigger=self.trig)
        self.assertIn("failed 2 times", s["alert"])
        bs.mark_done("full", True, 5, path=self.f, now=1900)          # recovered
        s = bs.summary(now=1901, path=self.f, trigger=self.trig)
        self.assertEqual((s["consecutive_failures"], s["alert"], s["fail_count"]), (0, "", 2))

    def test_overdue_when_no_good_backup_for_three_intervals(self):
        bs.mark_done("full", True, 5, path=self.f, now=1000)
        s = bs.summary(now=1000 + 3 * 300 + 10, path=self.f, trigger=self.trig)
        self.assertTrue(s["overdue"])
        self.assertIn("No successful Drive backup", s["alert"])

    def test_hung_backup_is_flagged(self):
        bs.mark_start("full", path=self.f, now=1000)
        s = bs.summary(now=1000 + bs.RUNNING_STALE_SECS + 5, path=self.f, trigger=self.trig)
        self.assertTrue(s["stalled"])
        self.assertIn("may be stuck", s["alert"])

    def test_disabled_state_alerts(self):
        bs.mark_disabled("restore failed", path=self.f)
        s = bs.summary(path=self.f, trigger=self.trig)
        self.assertEqual(s["state"], "disabled")
        self.assertIn("OFF", s["alert"])

    def test_request_now_creates_trigger_once_and_not_while_running(self):
        self.assertEqual(bs.request_now(path=self.f, trigger=self.trig, now=1000), "queued")
        self.assertTrue(os.path.exists(self.trig))
        self.assertTrue(bs.summary(path=self.f, trigger=self.trig)["pending"])
        self.assertEqual(bs.request_now(path=self.f, trigger=self.trig, now=1001), "already_queued")
        os.remove(self.trig)
        bs.mark_start("full", path=self.f, now=1000)
        self.assertEqual(bs.request_now(path=self.f, trigger=self.trig, now=1002), "running")

    def test_cli_never_raises_and_writes(self):
        env = dict(os.environ, BACKUP_STATUS_FILE=self.f)
        subprocess.run([sys.executable, os.path.join(ROOT, "backup_status.py"), "start", "full"], env=env, check=True)
        subprocess.run([sys.executable, os.path.join(ROOT, "backup_status.py"), "done", "full", "fail", "7", "ERROR x"], env=env, check=True)
        subprocess.run([sys.executable, os.path.join(ROOT, "backup_status.py"), "bogus"], env=env, check=True)
        st = bs.read_state(self.f)
        self.assertEqual((st["state"], st["last_duration_s"]), ("failed", 7))

    def test_history_is_capped(self):
        for i in range(40):
            bs.mark_done("full", True, 1, path=self.f, now=1000 + i)
        self.assertEqual(len(bs.read_state(self.f)["history"]), bs.HISTORY_MAX)


class StartShSyncTests(unittest.TestCase):
    """Runs the real sync_to_cloud() text from start.sh with a fake rclone."""

    def run_sync(self, fake_rc):
        tmp = tempfile.mkdtemp()
        bin_dir = os.path.join(tmp, "bin")
        os.makedirs(bin_dir)
        rc = os.path.join(bin_dir, "rclone")
        with open(rc, "w") as fh:
            fh.write('#!/bin/bash\nif [ "$1" = "sync" ]; then echo "ERROR : file.txt: boom" >&2; exit %d; fi\nexit 0\n' % fake_rc)
        os.chmod(rc, os.stat(rc).st_mode | stat.S_IEXEC)
        os.makedirs(os.path.join(tmp, ".config", "rclone"))
        open(os.path.join(tmp, ".config", "rclone", "rclone.conf"), "w").close()
        with open(os.path.join(ROOT, "start.sh"), encoding="utf-8") as fh:
            sh = fh.read()
        start = sh.index('BACKUP_INTERVAL="${BACKUP_INTERVAL:-300}"')
        end = sh.index("\necho \">> Initial sync to Drive...\"")
        func = sh[start:end].replace("/app/backup_status.py", os.path.join(ROOT, "backup_status.py"))
        status = os.path.join(tmp, "status.json")
        script = (
            'export PATH="%s:$PATH" HOME="%s" BACKUP_STATUS_FILE="%s"\n'
            'REMOTE_BACKUP=gdrive:x; PICOCLAW_REMOTE=gdrive:x/picoclaw; SYNC_SAFE=%s\n'
            'rclone_sync() { rclone "$@"; }\npicoclaw_sync() { rclone "$@"; }\npack_pyuser() { return 1; }\n'
            % (bin_dir, tmp, status, "1" if fake_rc is not None else "0")
        ) + func + "\nsync_to_cloud full\n"
        subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
        return bs.read_state(status)

    def test_successful_sync_is_recorded_ok(self):
        st = self.run_sync(0)
        self.assertEqual((st["state"], st["ok_count"]), ("ok", 1))

    def test_failed_rclone_is_no_longer_swallowed(self):
        st = self.run_sync(1)
        self.assertEqual(st["state"], "failed")
        self.assertIn("boom", st["last_error"])
        self.assertIn("rclone exit 1/1", st["last_error"])


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.f = os.path.join(cls.dir, "status.json")
        cls.trig = os.path.join(cls.dir, "now")
        cls.saved = (bs.STATE_FILE, bs.TRIGGER_FILE)
        bs.STATE_FILE, bs.TRIGGER_FILE = cls.f, cls.trig
        http.server.ThreadingHTTPServer.daemon_threads = True
        cls.port = free_port()
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), keep_alive.PingHandler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        bs.STATE_FILE, bs.TRIGGER_FILE = cls.saved

    def setUp(self):
        for p in (self.f, self.trig):
            if os.path.exists(p):
                os.remove(p)

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.read().decode()

    def post(self, path):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, {}

    def test_page_has_backup_card_and_button(self):
        page = self.get("/memory")
        self.assertIn('id="bk-btn"', page)
        self.assertIn("Backup now", page)
        self.assertIn("/api/backup-now", page)

    def test_api_stats_exposes_backup_and_alert(self):
        bs.mark_done("full", True, 12)
        d = json.loads(self.get("/api/stats"))
        self.assertEqual(d["backup"]["state"], "ok")
        self.assertLess(d["backup"]["last_ok_ago_s"], 10)
        bs.mark_done("full", False, 3, "ERROR boom")
        bs.mark_done("full", False, 3, "ERROR boom")
        d = json.loads(self.get("/api/stats"))
        self.assertTrue(any("Drive backup failed" in a for a in d["alerts"]))

    def test_button_creates_trigger_file(self):
        self.assertEqual(self.post("/api/backup-now"), (200, {"result": "queued"}))
        self.assertTrue(os.path.exists(self.trig))
        self.assertEqual(self.post("/api/backup-now")[1]["result"], "already_queued")

    def test_button_is_protected_by_basic_auth_when_configured(self):
        old = keep_alive.UI_BASIC_AUTH
        keep_alive.UI_BASIC_AUTH = "u:p"
        self.addCleanup(setattr, keep_alive, "UI_BASIC_AUTH", old)
        self.assertEqual(self.post("/api/backup-now")[0], 401)
        self.assertFalse(os.path.exists(self.trig))

    def test_dashboard_javascript_is_valid(self):
        """A JS syntax error would silently kill the WHOLE dashboard in the browser, so check it with node."""
        import shutil
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        page = self.get("/memory")
        js = re.search(r"<script>(.*)</script>", page, re.S).group(1)
        path = os.path.join(self.dir, "page.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(js)
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
