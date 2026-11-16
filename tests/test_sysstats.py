import os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sysstats import Stats  # noqa: E402


def w(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class SysStatsTests(unittest.TestCase):
    def setUp(self):
        self.cg = tempfile.mkdtemp()
        self.proc = tempfile.mkdtemp()
        self.clock = Clock()

    def stats(self, **env):
        return Stats(cg_root=self.cg, proc=self.proc, clock=self.clock, env=env, disk_paths=())

    def test_cgroup_v2_memory_is_working_set_against_the_cgroup_limit(self):
        w(self.cg, "memory.current", str(300 * 1048576))
        w(self.cg, "memory.max", str(512 * 1048576))
        w(self.cg, "memory.stat", f"anon 1\ninactive_file {50 * 1048576}\n")
        w(self.cg, "memory.events", "low 0\noom 1\noom_kill 2\n")
        w(self.cg, "memory.peak", str(420 * 1048576))
        m = self.stats().memory()
        self.assertEqual((m["total_mb"], m["used_mb"], m["free_mb"]), (512.0, 250.0, 262.0))   # 300 - 50 cache
        self.assertEqual(m["percent"], 48.8)
        self.assertEqual((m["cache_mb"], m["peak_mb"], m["oom_kills"], m["source"]), (50.0, 420.0, 2, "cgroup-v2"))

    def test_cgroup_v1_memory(self):
        w(self.cg, "memory/memory.usage_in_bytes", str(200 * 1048576))
        w(self.cg, "memory/memory.limit_in_bytes", str(256 * 1048576))
        w(self.cg, "memory/memory.stat", f"total_inactive_file {100 * 1048576}\n")
        m = self.stats().memory()
        self.assertEqual((m["total_mb"], m["used_mb"], m["source"]), (256.0, 100.0, "cgroup-v1"))

    def test_unlimited_cgroup_uses_env_or_default_not_the_host_size(self):
        w(self.cg, "memory.current", str(100 * 1048576))
        w(self.cg, "memory.max", "max")
        self.assertEqual(self.stats().memory()["total_mb"], 512.0)
        self.assertEqual(self.stats(RAM_LIMIT_MB="1024").memory()["total_mb"], 1024.0)

    def test_env_limit_overrides_cgroup(self):
        w(self.cg, "memory.current", str(100 * 1048576))
        w(self.cg, "memory.max", str(2048 * 1048576))
        m = self.stats(RAM_LIMIT_MB="512").memory()
        self.assertEqual((m["total_mb"], m["limit_source"]), (512.0, "env RAM_LIMIT_MB"))

    def test_memory_falls_back_to_process_rss_without_cgroup(self):
        w(self.proc, "10/statm", f"1000 {1048576 * 40 // 4096} 0 0 0 0 0")        # 40 MB resident
        m = self.stats().memory()
        self.assertEqual((m["source"], m["used_mb"]), ("processes", 40.0))

    def test_cpu_percent_is_relative_to_the_cgroup_quota_and_throttling_is_reported(self):
        w(self.cg, "cpu.max", "10000 100000")                                      # 0.1 vCPU
        w(self.cg, "memory.current", "1")
        s = self.stats()
        w(self.cg, "cpu.stat", "usage_usec 0\nthrottled_usec 0\n")
        s.sample()
        self.clock.t += 20
        w(self.cg, "cpu.stat", f"usage_usec {int(1.0 * 1e6)}\nthrottled_usec {int(10 * 1e6)}\n")   # 1 cpu-second in 20 s
        s.sample()
        c = s.cpu()
        self.assertEqual(c["limit_cores"], 0.1)
        self.assertAlmostEqual(c["cores_used"], 0.05, places=3)
        self.assertEqual(c["percent"], 50.0)                                        # 0.05 of 0.1 vCPU
        self.assertEqual(c["throttled_percent"], 50.0)
        self.assertEqual(c["avg"]["1m"], 50.0)

    def test_cpu_limit_env_and_default(self):
        self.assertEqual(self.stats().cpu_limit()[0], 0.1)
        self.assertEqual(self.stats(CPU_LIMIT="0.5").cpu_limit()[0], 0.5)

    def test_process_scan_reports_rss_and_cpu_share(self):
        w(self.cg, "memory.current", "1")
        w(self.cg, "cpu.max", "10000 100000")
        for pid, rss_mb, name in ((5, 100, "big"), (6, 10, "small")):
            w(self.proc, f"{pid}/statm", f"1 {rss_mb * 1048576 // 4096} 0 0 0 0 0")
            w(self.proc, f"{pid}/comm", name)
            w(self.proc, f"{pid}/cmdline", f"/usr/bin/{name}\x00")
            w(self.proc, f"{pid}/stat", f"{pid} ({name}) S " + " ".join(["0"] * 10 + ["0", "0"] + ["0"] * 30))
        s = self.stats()
        s.scan_processes()
        self.clock.t += 10
        tick = s.clk_tck
        w(self.proc, "5/stat", "5 (big) S " + " ".join(["0"] * 10 + [str(tick // 2), str(tick // 2)] + ["0"] * 30))   # 1 cpu-s / 10 s
        s.scan_processes()
        rows = s.snapshot()["processes"]
        self.assertEqual([r["command"] for r in rows], ["big", "small"])           # sorted by RSS
        self.assertEqual(rows[0]["rss_mb"], 100.0)
        self.assertEqual(rows[0]["cpu"], 100.0)                                    # 0.1 cpu / 0.1 vCPU limit
        self.assertEqual(rows[1]["cpu"], 0.0)

    def test_comm_with_parentheses_and_spaces_does_not_break_stat_parsing(self):
        w(self.proc, "7/stat", "7 (my (weird) name) S " + " ".join(["0"] * 10 + ["30", "20"] + ["0"] * 30))
        self.assertEqual(self.stats()._proc_ticks(7), 50)

    def test_container_uptime_is_host_uptime_minus_pid1_start(self):
        w(self.proc, "uptime", "10000.00 5000.00")
        s = self.stats()
        w(self.proc, "1/stat", "1 (start.sh) S " + " ".join(["0"] * 18 + [str(9000 * s.clk_tck)] + ["0"] * 20))
        self.assertAlmostEqual(s.uptime_seconds(), 1000.0, places=1)

    def test_disk_limit_is_configurable_and_percent_is_correct(self):
        s = self.stats(DISK_LIMIT_MB="1000")
        s._disk = {"used_mb": 250.0, "parts": {"/root": 250.0}, "at": 0}
        d = s.disk()
        self.assertEqual((d["total_mb"], d["used_mb"], d["free_mb"], d["percent"]), (1000.0, 250.0, 750.0, 25.0))

    # ---- low-overhead sampling + alerts ----
    def test_idle_tick_only_reads_cpu_counter_and_does_no_scans(self):
        w(self.cg, "cpu.stat", "usage_usec 0\nthrottled_usec 0\n")
        s = self.stats()
        calls = []
        s.scan_processes = lambda *a, **k: calls.append("procs")
        s.scan_disk = lambda *a, **k: calls.append("disk")
        self.assertFalse(s.active())
        self.assertEqual(s.tick(), 10.0)                      # IDLE_INTERVAL
        self.assertEqual(calls, [])                           # nobody watching: no process scan, no du
        self.assertEqual(len(s._samples), 1)                  # but the cheap cpu sample is taken

    def test_touch_switches_to_fast_mode_then_back_to_idle(self):
        w(self.cg, "cpu.stat", "usage_usec 0\nthrottled_usec 0\n")
        s = self.stats()
        calls = []
        s.scan_processes = lambda *a, **k: calls.append("procs")
        s.scan_disk = lambda *a, **k: (calls.append("disk"), s._disk.update(at=s.clock()))
        s.touch()
        self.assertTrue(s._wake.is_set())                     # first view wakes the sampler immediately
        self.assertEqual(s.tick(), 2.0)                       # FAST_INTERVAL
        self.assertEqual(calls, ["procs", "disk"])            # disk never measured yet -> measured once
        self.clock.t += 5
        s.touch(); s._wake.clear()
        s.touch()
        self.assertFalse(s._wake.is_set())                    # already active: more requests do not re-wake it
        s.tick()
        self.assertEqual(calls, ["procs", "disk", "procs"])   # du is NOT repeated before DISK_EVERY
        self.clock.t += 16                                    # nobody asked for 16 s
        self.assertFalse(s.active())
        self.assertEqual(s.tick(), 10.0)

    def test_alerts_for_high_ram_cpu_disk_and_new_oom_kills(self):
        w(self.cg, "memory.current", str(480 * 1048576))
        w(self.cg, "memory.max", str(512 * 1048576))
        w(self.cg, "memory.stat", "inactive_file 0\n")
        w(self.cg, "memory.events", "oom_kill 0\n")
        w(self.cg, "cpu.max", "10000 100000")
        s = self.stats(DISK_LIMIT_MB="1000")
        s._disk = {"used_mb": 950.0, "parts": {}, "at": 0}
        w(self.cg, "cpu.stat", "usage_usec 0\nthrottled_usec 0\n")
        s.sample()
        self.clock.t += 10
        w(self.cg, "cpu.stat", f"usage_usec {int(0.95e6)}\nthrottled_usec 0\n")    # 0.095 vCPU of 0.1
        s.sample()
        a = s.snapshot()["alerts"]
        self.assertTrue(any(x.startswith("RAM high") for x in a), a)
        self.assertTrue(any(x.startswith("CPU high") for x in a), a)
        self.assertTrue(any(x.startswith("Disk almost full") for x in a), a)
        self.assertFalse(any("OOM" in x for x in a), a)       # baseline value, nothing new yet
        w(self.cg, "memory.events", "oom_kill 2\n")
        self.assertTrue(any("OOM kill detected (+2" in x for x in s.snapshot()["alerts"]))

    def test_no_alerts_when_everything_is_calm(self):
        w(self.cg, "memory.current", str(100 * 1048576))
        w(self.cg, "memory.max", str(512 * 1048576))
        w(self.cg, "memory.stat", "inactive_file 0\n")
        s = self.stats()
        self.assertEqual(s.snapshot()["alerts"], [])

    def test_top_cpu_list_is_sorted_by_cpu_and_secrets_are_redacted(self):
        w(self.cg, "memory.current", "1")
        w(self.cg, "cpu.max", "10000 100000")
        for pid, rss_mb, name in ((5, 100, "idle-big"), (6, 10, "busy-small")):
            w(self.proc, f"{pid}/statm", f"1 {rss_mb * 1048576 // 4096} 0 0 0 0 0")
            w(self.proc, f"{pid}/comm", name)
            w(self.proc, f"{pid}/cmdline", f"/usr/bin/{name}\x00")
            w(self.proc, f"{pid}/stat", f"{pid} ({name}) S " + " ".join(["0"] * 10 + ["0", "0"] + ["0"] * 30))
        s = self.stats()
        s.scan_processes()
        self.clock.t += 10
        w(self.proc, "6/stat", "6 (busy-small) S " + " ".join(["0"] * 10 + [str(s.clk_tck // 2), str(s.clk_tck // 2)] + ["0"] * 30))
        s.scan_processes()
        snap = s.snapshot()
        self.assertEqual([r["command"] for r in snap["processes"]], ["idle-big", "busy-small"])        # by RAM
        self.assertEqual([r["command"] for r in snap["processes_cpu"]], ["busy-small", "idle-big"])    # by CPU
        from sysstats import _redact
        self.assertEqual(_redact("tailscale up --authkey=tskey-abc123 x"), "tailscale up --authkey=*** x")
        self.assertNotIn("hunter2", _redact("run --password hunter2"))


if __name__ == "__main__":
    unittest.main()
