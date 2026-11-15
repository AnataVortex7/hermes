"""Accurate RAM / CPU / disk numbers for THIS container (not the host machine).

Inside a container /proc/meminfo, /proc/stat, /proc/loadavg and /proc/uptime describe the whole HOST, so a page built
on them shows somebody else's usage.  The container's real numbers live in its cgroup:

  memory : v2 memory.current / memory.max / memory.stat (inactive_file) / memory.events (oom_kill) / memory.peak
           v1 memory.usage_in_bytes / limit_in_bytes / stat / max_usage_in_bytes / oom_control
  cpu    : v2 cpu.stat (usage_usec, throttled_usec) / cpu.max        v1 cpuacct.usage / cfs_quota_us
  procs  : /proc/<pid>/stat + statm  (a container only sees its own PIDs)
  uptime : host uptime minus the start time of PID 1

"Used" memory is the *working set* (usage minus reclaimable page cache) - the number an OOM kill is decided on.
If the platform does not expose a cgroup, memory falls back to the sum of process RSS and CPU to the sum of process
CPU time (clearly labelled in `source`).

Limits (what your plan allots) cannot always be read from inside, so they can be set:
  RAM_LIMIT_MB (default: cgroup limit, else 512)   CPU_LIMIT in cores (default: cgroup quota, else 0.1)
  DISK_LIMIT_MB (default 2000)
Everything slow (disk `du`, per-process scan) runs in a background thread; reading a snapshot never blocks.
"""
import os
import subprocess
import threading
import time
from collections import deque

_HUGE = 1 << 60            # cgroup "no limit" values are huge (v1: 9223372036854771712) or the word "max"


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _kv(path):
    out = {}
    txt = _read(path)
    for line in (txt or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def _int(txt):
    try:
        return int(str(txt).strip())
    except (TypeError, ValueError):
        return None


class Stats:
    def __init__(self, cg_root="/sys/fs/cgroup", proc="/proc", clock=time.time, env=None, disk_paths=("/root", "/app", "/tmp")):
        self.cg = cg_root
        self.proc = proc
        self.clock = clock
        self.env = os.environ if env is None else env
        self.disk_paths = disk_paths
        self.clk_tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self.page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        self._lock = threading.Lock()
        self._samples = deque()                 # (t, cpu_seconds, throttled_seconds)
        self._procs = []                        # cached top processes
        self._pprev = {}                        # pid -> (cpu_ticks, t)
        self._disk = {"used_mb": 0.0, "parts": {}, "at": 0.0}
        self._dirs = self._cg_dirs()
        self.version = self._detect_version()

    # ------------------------------------------------------------------ cgroup discovery
    def _cg_dirs(self):
        dirs = [self.cg]
        txt = _read(os.path.join(self.proc, "self", "cgroup")) or ""
        for line in txt.splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[2].strip("/"):
                dirs.append(os.path.join(self.cg, parts[2].strip("/")))
                for ctrl in parts[1].split(","):
                    if ctrl:
                        dirs.append(os.path.join(self.cg, ctrl, parts[2].strip("/")))
        for ctrl in ("memory", "cpu", "cpuacct", "cpu,cpuacct"):
            dirs.append(os.path.join(self.cg, ctrl))
        return dirs

    def _file(self, *names):
        for d in self._dirs:
            for n in names:
                p = os.path.join(d, n)
                if os.path.isfile(p):
                    return p
        return None

    def _detect_version(self):
        if self._file("memory.current"):
            return "cgroup-v2"
        if self._file("memory.usage_in_bytes"):
            return "cgroup-v1"
        return None

    # ------------------------------------------------------------------ memory
    def _proc_pids(self):
        try:
            return [int(x) for x in os.listdir(self.proc) if x.isdigit()]
        except OSError:
            return []

    def _rss_bytes(self, pid):
        txt = _read(os.path.join(self.proc, str(pid), "statm"))
        try:
            return int(txt.split()[1]) * self.page
        except (AttributeError, IndexError, ValueError):
            return 0

    def memory(self):
        usage = limit = inactive = peak = oom = None
        source = "processes"
        if self.version == "cgroup-v2":
            usage = _int(_read(self._file("memory.current")))
            lim = _read(self._file("memory.max") or "")
            limit = None if lim in (None, "max") else _int(lim)
            inactive = _kv(self._file("memory.stat") or "").get("inactive_file", 0)
            peak = _int(_read(self._file("memory.peak") or ""))
            oom = _kv(self._file("memory.events") or "").get("oom_kill")
            source = "cgroup-v2"
        elif self.version == "cgroup-v1":
            usage = _int(_read(self._file("memory.usage_in_bytes")))
            limit = _int(_read(self._file("memory.limit_in_bytes") or ""))
            st = _kv(self._file("memory.stat") or "")
            inactive = st.get("total_inactive_file", st.get("inactive_file", 0))
            peak = _int(_read(self._file("memory.max_usage_in_bytes") or ""))
            oom = _kv(self._file("memory.oom_control") or "").get("oom_kill")
            source = "cgroup-v1"
        if usage is None:                                           # no cgroup: sum of our own processes
            usage = sum(self._rss_bytes(p) for p in self._proc_pids())
            inactive = 0
            source = "processes"
        working = max(0, usage - (inactive or 0))

        env_lim = _int(self.env.get("RAM_LIMIT_MB"))
        if env_lim:
            total, lsrc = env_lim * 1048576, "env RAM_LIMIT_MB"
        elif limit and limit < _HUGE:
            total, lsrc = limit, "cgroup"
        else:
            total, lsrc = 512 * 1048576, "default 512 MB (no cgroup limit visible)"
        mb = lambda b: round(b / 1048576, 1)  # noqa: E731
        return {
            "total_mb": mb(total), "used_mb": mb(working), "free_mb": max(0.0, mb(total - working)),
            "percent": min(100.0, round(100.0 * working / total, 1)),
            "cache_mb": mb(inactive or 0), "peak_mb": mb(peak) if peak else None,
            "oom_kills": oom, "source": source, "limit_source": lsrc,
        }

    # ------------------------------------------------------------------ cpu
    def cpu_limit(self):
        env_lim = self.env.get("CPU_LIMIT")
        try:
            if env_lim:
                return float(env_lim), "env CPU_LIMIT"
        except ValueError:
            pass
        txt = _read(self._file("cpu.max") or "")
        if txt:
            parts = txt.split()
            if len(parts) == 2 and parts[0] != "max":
                try:
                    return int(parts[0]) / int(parts[1]), "cgroup"
                except (ValueError, ZeroDivisionError):
                    pass
        quota = _int(_read(self._file("cpu.cfs_quota_us") or ""))
        period = _int(_read(self._file("cpu.cfs_period_us") or ""))
        if quota and quota > 0 and period:
            return quota / period, "cgroup"
        return 0.1, "default 0.1 vCPU (no cgroup limit visible)"

    def _cpu_counters(self):
        """(cpu_seconds, throttled_seconds, source)"""
        f = self._file("cpu.stat")
        if self.version == "cgroup-v2" and f:
            st = _kv(f)
            if "usage_usec" in st:
                return st["usage_usec"] / 1e6, st.get("throttled_usec", 0) / 1e6, "cgroup-v2"
        f = self._file("cpuacct.usage")
        if f:
            ns = _int(_read(f))
            if ns is not None:
                thr = _kv(self._file("cpu.stat") or "").get("throttled_time", 0) / 1e9
                return ns / 1e9, thr, "cgroup-v1"
        total = 0
        for pid in self._proc_pids():
            ticks = self._proc_ticks(pid)
            total += ticks or 0
        return total / self.clk_tck, 0.0, "processes"

    def _proc_ticks(self, pid):
        txt = _read(os.path.join(self.proc, str(pid), "stat"))
        if not txt:
            return None
        try:
            rest = txt[txt.rindex(")") + 2:].split()
            return int(rest[11]) + int(rest[12])            # utime + stime
        except (ValueError, IndexError):
            return None

    def sample(self):
        """Take one CPU sample (called every few seconds by the background thread)."""
        cpu, thr, _src = self._cpu_counters()
        now = self.clock()
        with self._lock:
            self._samples.append((now, cpu, thr))
            while self._samples and now - self._samples[0][0] > 16 * 60:
                self._samples.popleft()

    def _window(self, seconds):
        """(cores_used, throttled_fraction) over about the last `seconds`, or None if not enough data."""
        with self._lock:
            if len(self._samples) < 2:
                return None
            t1, c1, th1 = self._samples[-1]
            base = self._samples[0]
            for s in self._samples:
                if t1 - s[0] <= seconds:
                    base = s
                    break
            if base is self._samples[-1]:          # window shorter than the sampling interval: use the last interval
                base = self._samples[-2]
        t0, c0, th0 = base
        dt = t1 - t0
        if dt <= 0:
            return None
        return max(0.0, (c1 - c0) / dt), max(0.0, (th1 - th0) / dt)

    def cpu(self):
        limit, lsrc = self.cpu_limit()
        _c, _t, src = self._cpu_counters()
        out = {"limit_cores": round(limit, 3), "limit_source": lsrc, "source": src,
               "percent": 0.0, "cores_used": 0.0, "throttled_percent": 0.0, "avg": {"1m": 0.0, "5m": 0.0, "15m": 0.0}}
        cur = self._window(15)
        if cur:
            out["cores_used"] = round(cur[0], 4)
            out["percent"] = min(100.0, round(100.0 * cur[0] / limit, 1))
            out["throttled_percent"] = min(100.0, round(100.0 * cur[1], 1))
        for name, secs in (("1m", 60), ("5m", 300), ("15m", 900)):
            w = self._window(secs)
            if w:
                out["avg"][name] = min(100.0, round(100.0 * w[0] / limit, 1))
        return out

    # ------------------------------------------------------------------ processes
    def _cmd(self, pid):
        raw = _read(os.path.join(self.proc, str(pid), "cmdline")) or ""
        argv = [a for a in raw.split("\x00") if a]
        name = _read(os.path.join(self.proc, str(pid), "comm")) or (os.path.basename(argv[0]) if argv else str(pid))
        if argv:
            first = os.path.basename(argv[0])
            if first.startswith(("python", "node", "bash", "sh", "ruby", "perl")):
                for a in argv[1:]:
                    if not a.startswith("-"):
                        name = f"{first} {os.path.basename(a)}"
                        break
        return name[:40]

    def scan_processes(self, top=8):
        """Per-process RSS and CPU (share of the plan's CPU allotment) - run from the background thread."""
        limit, _ = self.cpu_limit()
        mem_total = self.memory()["total_mb"] * 1048576
        now = self.clock()
        rows, seen = [], {}
        for pid in self._proc_pids():
            ticks = self._proc_ticks(pid)
            if ticks is None:
                continue
            seen[pid] = (ticks, now)
            cpu_pct = 0.0
            prev = self._pprev.get(pid)
            if prev and now > prev[1]:
                cpu_pct = max(0.0, (ticks - prev[0]) / self.clk_tck / (now - prev[1]) / limit * 100.0)
            rss = self._rss_bytes(pid)
            rows.append({"pid": str(pid), "command": self._cmd(pid), "rss_mb": round(rss / 1048576, 1),
                         "mem": round(100.0 * rss / mem_total, 1), "cpu": round(min(cpu_pct, 999.0), 1)})
        self._pprev = seen
        rows.sort(key=lambda r: -r["rss_mb"])
        with self._lock:
            self._procs = rows[:top]

    # ------------------------------------------------------------------ disk
    def scan_disk(self):
        parts, total = {}, 0.0
        for p in self.disk_paths:
            try:
                out = subprocess.check_output(["du", "-xsm", p], stderr=subprocess.DEVNULL, timeout=30).decode()
                mb = float(out.split()[0])
            except Exception:  # noqa: BLE001
                continue
            parts[p] = mb
            total += mb
        with self._lock:
            self._disk = {"used_mb": total, "parts": parts, "at": self.clock()}

    def disk(self):
        try:
            limit = float(self.env.get("DISK_LIMIT_MB") or 2000)
        except ValueError:
            limit = 2000.0
        with self._lock:
            d = dict(self._disk)
        used = round(d["used_mb"], 1)
        return {"total_mb": limit, "used_mb": used, "free_mb": max(0.0, round(limit - used, 1)),
                "percent": min(100.0, round(100.0 * used / limit, 1)), "parts": d["parts"],
                "measured": "du -x of " + ", ".join(self.disk_paths),
                "limit_source": "env DISK_LIMIT_MB" if self.env.get("DISK_LIMIT_MB") else "default 2000 MB"}

    # ------------------------------------------------------------------ uptime
    def uptime_seconds(self):
        """Container uptime = host uptime minus the start time of PID 1 (/proc/uptime alone is the HOST's)."""
        try:
            host = float((_read(os.path.join(self.proc, "uptime")) or "0").split()[0])
            txt = _read(os.path.join(self.proc, "1", "stat"))
            start_ticks = int(txt[txt.rindex(")") + 2:].split()[19])
            return max(0.0, host - start_ticks / self.clk_tck)
        except (ValueError, IndexError, AttributeError):
            return 0.0

    # ------------------------------------------------------------------ snapshot + thread
    def snapshot(self):
        secs = int(self.uptime_seconds())
        with self._lock:
            procs = list(self._procs)
        return {
            "memory": self.memory(), "cpu": self.cpu(), "disk": self.disk(),
            "uptime": f"{secs // 3600}h {(secs % 3600) // 60}m", "uptime_seconds": secs,
            "processes": procs,
        }

    def start(self, interval=5.0):
        def loop():
            n = 0
            while True:
                try:
                    self.sample()
                    if n % 2 == 0:
                        self.scan_processes()
                    if n % 12 == 0:
                        self.scan_disk()
                except Exception:  # noqa: BLE001  - the stats thread must never die
                    pass
                n += 1
                threading.Event().wait(interval)
        t = threading.Thread(target=loop, daemon=True, name="sysstats")
        t.start()
        return t
