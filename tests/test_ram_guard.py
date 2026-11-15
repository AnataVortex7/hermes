import json, os, sys, tempfile, time, unittest
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ram_guard as rg  # noqa: E402
from sysstats import Stats  # noqa: E402

MB = 1048576


def P(pid, ppid, cmd, rss_mb=1, start=0, comm=None):
    p = rg.Proc()
    p.pid, p.ppid, p.cmd, p.rss, p.start, p.state = pid, ppid, cmd, rss_mb * MB, start, "S"
    argv = cmd.split()
    p.ident, p.comm = rg._ident(argv), comm or os.path.basename(argv[0])
    return p


class RamGuardTests(unittest.TestCase):
    def procs(self, *ps):
        return {p.pid: p for p in ps}

    def test_ident_looks_through_interpreters(self):
        self.assertEqual(rg._ident(["python3", "/app/keep_alive.py"]), "keep_alive.py")
        self.assertEqual(rg._ident(["/usr/local/bin/picoclaw", "gateway"]), "picoclaw")
        self.assertEqual(rg._ident(["python3", "-c", "print(1)"]), "python3")

    def test_main_processes_are_never_jobs(self):
        procs = self.procs(
            P(1, 0, "bash /app/start.sh", 5), P(2, 1, "python3 /app/keep_alive.py", 60),
            P(3, 1, "python3 /app/llm_proxy.py", 40), P(4, 1, "picoclaw gateway", 90),
            P(5, 1, "tailscaled --tun=userspace-networking", 50), P(6, 2, "du -sk /root", 3),
            P(10, 4, "sh -c python3 scrape.py", 1, 100), P(11, 10, "python3 scrape.py", 200, 101))
        jobs = rg.find_jobs(procs)
        self.assertEqual([j["cmd"] for j in jobs], ["python3 scrape.py"])        # shell wrapper looked through
        self.assertEqual(jobs[0]["members"], [11])

    def test_whole_tree_is_one_job(self):
        procs = self.procs(P(1, 0, "bash /app/start.sh"), P(4, 1, "picoclaw gateway"),
                           P(11, 4, "python3 crawl.py", 30, 10), P(12, 11, "chromium --headless", 150, 11),
                           P(13, 12, "chromium --type=renderer", 120, 12))
        jobs = rg.find_jobs(procs)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(sorted(jobs[0]["members"]), [11, 12, 13])
        self.assertEqual(jobs[0]["rss"], 300 * MB)

    def test_culprit_is_the_job_that_grew_not_the_big_old_one(self):
        old = {"pid": 20, "cmd": "node big_server.js", "start": 1, "rss": 200 * MB, "members": [20]}
        new = {"pid": 30, "cmd": "python3 spike.py", "start": 9, "rss": 120 * MB, "members": [30]}
        now = time.time()
        hist = deque([(now - 5, {20: 200 * MB, 30: 2 * MB})])
        self.assertEqual(rg.pick_culprit([old, new], hist, now)["pid"], 30)

    def test_without_history_last_started_big_job_wins_and_tiny_jobs_are_ignored(self):
        a = {"pid": 20, "cmd": "a", "start": 1, "rss": 80 * MB, "members": [20]}
        b = {"pid": 30, "cmd": "b", "start": 9, "rss": 60 * MB, "members": [30]}
        tiny = {"pid": 40, "cmd": "sleep 300", "start": 99, "rss": 1 * MB, "members": [40]}
        self.assertEqual(rg.pick_culprit([a, b, tiny], deque(), time.time())["pid"], 30)
        self.assertIsNone(rg.pick_culprit([tiny], deque(), time.time()))


class MeterTests(unittest.TestCase):
    def setUp(self):
        self.cg, self.proc = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.mi = os.path.join(tempfile.mkdtemp(), "meminfo")

    def w(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write(text)

    def meter(self, total_mb, avail_mb, cg_used_mb=None, cg_limit_mb=None, **env):
        self.w(self.mi, f"MemTotal: {total_mb * 1024} kB\nMemFree: 1 kB\nMemAvailable: {avail_mb * 1024} kB\n")
        if cg_used_mb is not None:
            self.w(os.path.join(self.cg, "memory.current"), str(cg_used_mb * MB))
            self.w(os.path.join(self.cg, "memory.max"), "max" if cg_limit_mb is None else str(cg_limit_mb * MB))
            self.w(os.path.join(self.cg, "memory.stat"), "inactive_file 0\n")
        st = Stats(cg_root=self.cg, proc=self.proc, env=env, disk_paths=())
        return rg.Meter(st, meminfo=self.mi, vmstat=os.path.join(self.proc, "none"), env=env)

    def test_vm_plan_ram_comes_from_meminfo_when_cgroup_has_no_limit(self):
        m = self.meter(512, 40, cg_used_mb=100).read()          # cgroup says 100 MB, but the VM has only 40 MB left
        self.assertEqual((m["total_mb"], m["used_mb"], m["percent"]), (512.0, 472.0, 92.2))
        self.assertTrue(m["source"].startswith("system"))

    def test_cgroup_limit_wins_and_host_meminfo_is_ignored(self):
        m = self.meter(64000, 1000, cg_used_mb=300, cg_limit_mb=512).read()   # container on a big host
        self.assertEqual((m["total_mb"], m["used_mb"], m["percent"]), (512.0, 300.0, 58.6))
        self.assertFalse(m["meminfo_trusted"])

    def test_env_limit_overrides_and_unknown_limit_is_flagged(self):
        m = self.meter(64000, 1000, cg_used_mb=200, RAM_LIMIT_MB="256").read()
        self.assertEqual((m["total_mb"], m["percent"], m["limit_source"]), (256.0, 78.1, "env RAM_LIMIT_MB"))
        m = self.meter(64000, 1000, cg_used_mb=200).read()
        self.assertTrue(m["limit_source"].startswith("UNKNOWN"))

    def test_small_plans_get_an_earlier_cut_for_the_min_free_margin(self):
        self.assertAlmostEqual(rg.effective_threshold({"total_mb": 512.0}, 91), 91)
        self.assertAlmostEqual(rg.effective_threshold({"total_mb": 256.0}, 91), 100 * (1 - 32 / 256))


class ProxyNoticeTests(unittest.TestCase):
    def test_agent_is_told_why_for_a_limited_number_of_requests(self):
        d = tempfile.mkdtemp()
        os.environ["RAM_GUARD_DIR"] = d
        import llm_proxy
        llm_proxy.RAM_EVENTS = os.path.join(d, "events.jsonl")
        ev = {"t": time.time(), "killed": True, "cmd": "python3 scrape.py", "pid": 7, "job_mb": 210.0, "grew_mb": 200.0,
              "before_percent": 95.0, "limit_mb": 512.0, "tips": ["use requests"], "shown": 0}
        open(llm_proxy.RAM_EVENTS, "w").write(json.dumps(ev) + "\n")
        for _ in range(llm_proxy.RAM_NOTICE_MAX):
            payload = {"messages": [{"role": "tool", "content": "exit 137"}]}
            llm_proxy.inject_ram_notice(payload)
            text = payload["messages"][-1]["content"]
            self.assertIn("KILLED your process `python3 scrape.py`", text)
            self.assertIn("use requests", text)
        payload = {"messages": [{"role": "tool", "content": "exit 137"}]}
        llm_proxy.inject_ram_notice(payload)
        self.assertEqual(payload["messages"][-1]["content"], "exit 137")        # shown 3 times, then quiet


if __name__ == "__main__":
    unittest.main()
