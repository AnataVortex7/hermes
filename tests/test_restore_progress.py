import json, os, stat, subprocess, sys, tempfile, textwrap, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import restore_progress as rp  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "restore_progress.py")

FAKE_RCLONE = textwrap.dedent('''\
    #!/usr/bin/env python3
    """fake rclone: `size --json X` and `sync SRC DST` that prints JSON stats like the real one."""
    import json, sys, time
    a = sys.argv[1:]
    if a[0] == "size":
        if "bad" in a[2]:
            sys.exit(3)
        print(json.dumps({"count": 4, "bytes": 4000000, "sizeless": 0}))
    elif a[0] == "sync":
        if "bad" in a[1]:
            print(json.dumps({"level": "error", "msg": "Failed to sync: directory not found"}), file=sys.stderr)
            sys.exit(3)
        for i in range(1, 5):
            st = {"bytes": i * 1000000, "totalBytes": 4000000, "transfers": i, "totalTransfers": 4, "speed": 1000000.0,
                  "transferring": [{"name": "dir/f%d.bin" % i, "percentage": 50, "size": 1000000}]}
            print(json.dumps({"level": "warning", "msg": "stats", "stats": st}), file=sys.stderr, flush=True)
            time.sleep(0.15)
''')


class UnitTests(unittest.TestCase):
    def test_size_command_keeps_only_the_filters(self):
        cmd = ["rclone", "sync", "gdrive:b", "/root/.x/", "--exclude", "cache/**", "--exclude=.env",
               "--drive-chunk-size", "8M", "--bwlimit", "1M", "--fast-list"]
        self.assertEqual(rp.size_command(cmd), ["rclone", "size", "--json", "gdrive:b", "--fast-list",
                                                "--exclude", "cache/**", "--exclude=.env"])

    def test_size_command_ignores_speed_flag_values(self):
        cmd = ["rclone", "sync", "gdrive:b", "/d/", "--transfers", "8", "--checkers", "16",
               "--drive-pacer-min-sleep", "10ms", "--drive-pacer-burst", "200", "--exclude", "x"]
        self.assertEqual(rp.size_command(cmd), ["rclone", "size", "--json", "gdrive:b", "--fast-list", "--exclude", "x"])

    def test_parse_phases(self):
        argv = ["A", "rclone", "sync", "s1", "d1", "--x", "+++", "B", "rclone", "sync", "s2", "d2"]
        self.assertEqual(rp.parse_phases(argv), [("A", ["rclone", "sync", "s1", "d1", "--x"]),
                                                 ("B", ["rclone", "sync", "s2", "d2"])])
        with self.assertRaises(SystemExit):
            rp.parse_phases(["A", "rclone", "copy", "s", "d"])

    def test_formatting(self):
        self.assertEqual(rp.fmt_bytes(512), "512 B")
        self.assertEqual(rp.fmt_bytes(1536 * 1024), "1.5 MB")
        self.assertEqual(rp.fmt_eta(None), "calculating...")
        self.assertEqual(rp.fmt_eta(75), "1m 15s")
        self.assertEqual(rp.fmt_eta(3700), "1h 01m")

    def _tracker(self, known=True):
        ph = [{"label": "a", "total_bytes": 10_000_000, "total_files": 10, "known": known, "done_bytes": 0, "done_files": 0, "finished": False},
              {"label": "b", "total_bytes": 10_000_000, "total_files": 10, "known": known, "done_bytes": 0, "done_files": 0, "finished": False}]
        tr = rp.Tracker(ph)
        tr.stage = "restoring"
        return tr

    def test_percent_combines_phases_by_bytes(self):
        tr = self._tracker()
        tr.phases[0].update(finished=True)
        tr.phases[1].update(done_bytes=5_000_000)
        self.assertEqual(tr.percent(), 75.0)
        tr.phases[1].update(done_bytes=99_000_000)             # never above 100%
        self.assertLessEqual(tr.percent(), 100.0)

    def test_percent_unknown_totals_falls_back_to_phase_share(self):
        tr = self._tracker(known=False)
        tr.idx = 1                                              # second phase is running
        tr.phases[0].update(finished=True)
        tr.phases[1].update(done_bytes=5_000_000)
        self.assertEqual(tr.percent(), 75.0)
        self.assertIsNone(tr.eta())

    def test_eta_uses_the_slower_of_bytes_and_files(self):
        tr = self._tracker()
        now = time.time()
        tr.phases[0].update(done_bytes=1_000_000, done_files=1)
        tr.samples.append((now - 10, 0, 0))
        tr.samples.append((now, 1_000_000, 1))                 # 100 KB/s, 0.1 files/s
        # bytes left 19 MB / 100 KB/s = 190 s ; files left 19 / 0.1 = 190 s
        self.assertAlmostEqual(tr.eta(), 190, delta=2)
        tr.samples.clear()
        tr.samples.append((now - 10, 0, 0))
        tr.samples.append((now, 5_000_000, 1))                 # fast bytes, slow files -> files decide (19/0.1)
        self.assertAlmostEqual(tr.eta(), 190, delta=2)

    def test_eta_needs_some_history(self):
        tr = self._tracker()
        tr.samples.append((time.time(), 0, 0))
        self.assertIsNone(tr.eta())


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fake = os.path.join(self.tmp, "rclone")
        with open(fake, "w") as f:
            f.write(FAKE_RCLONE)
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        self.state = os.path.join(self.tmp, "state.json")
        self.env = dict(os.environ, RESTORE_PROGRESS_FILE=self.state)

    def run_script(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=self.env, timeout=60)

    def test_successful_restore_reaches_100_percent(self):
        fake = os.path.join(self.tmp, "rclone")
        r = self.run_script("--attempt", "1", "--", "One", fake, "sync", "src1", "dst1", "+++", "Two", fake, "sync", "src2", "dst2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        st = json.load(open(self.state))
        self.assertEqual(st["stage"], "restored")
        self.assertEqual(st["percent"], 100.0)
        self.assertEqual(st["bytes_total"], 8_000_000)
        self.assertEqual(st["bytes_done"], 8_000_000)
        self.assertEqual(st["files_done"], 8)
        self.assertFalse(st["estimated"])

    def test_failed_restore_reports_error_and_exit_code(self):
        fake = os.path.join(self.tmp, "rclone")
        r = self.run_script("--", "One", fake, "sync", "bad-src", "dst")
        self.assertEqual(r.returncode, 3)
        st = json.load(open(self.state))
        self.assertEqual(st["stage"], "failed")
        self.assertIn("directory not found", st["error"])
        self.assertTrue(st["estimated"])                       # size scan failed -> approximate progress
        self.assertIn("ERROR: Failed to sync", r.stdout)       # still visible in /tmp/restore.log for start.sh

    def test_stage_update_keeps_other_fields(self):
        self.run_script("--stage", "packages", "--message", "Re-installing saved packages...")
        self.run_script("--stage", "clean_start", "--message", "clean", "--clean-start")
        st = json.load(open(self.state))
        self.assertEqual((st["stage"], st["message"], st["clean_start"]), ("clean_start", "clean", True))


if __name__ == "__main__":
    unittest.main()
