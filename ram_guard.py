#!/usr/bin/env python3
"""hermes RAM guard: kills the agent job that makes RAM spike, BEFORE the Linux OOM killer picks a victim.

Why: when the container hits its memory limit the kernel OOM killer kills whatever it likes, often PicoClaw itself.
This watchdog is tiny (reads a few cgroup files per second, scans /proc only when RAM is getting high) and does one thing:

  RAM working set >= RAM_GUARD_PERCENT (default 91)  ->  find the agent job that grew the most / started last,
  SIGKILL that job's whole process tree, tell the agent why (via llm_proxy.py) and tell the owner (Telegram).

Main functions are NEVER killed: PicoClaw gateway, keep_alive.py, llm_proxy.py, ram_guard.py, start.sh, tailscaled,
rclone, notify.py, tsports (+ children of the services). If the biggest process is one of those, nothing is killed
(only logged + notified).

Commands:   ram_guard.py            run the watchdog (started by start.sh)
            ram_guard.py status     current RAM % and the top agent jobs
            ram_guard.py log        last kill events

Env (all optional):
  RAM_GUARD=0                 disable            RAM_GUARD_PERCENT=91     kill threshold (% of RAM limit)
  RAM_GUARD_WATCH=75          above this: scan processes fast (0.5 s) and keep growth history
  RAM_GUARD_PREDICT=1         also kill early if RAM would pass ~98% within ~1 s at the current growth rate
  RAM_GUARD_MIN_MB=25         a job smaller than this (and not growing) is never a culprit
  RAM_GUARD_MIN_GROW_MB=15    growth inside RAM_GUARD_WINDOW (20 s) that makes a job "the culprit"
  RAM_GUARD_PROTECT=regex     extra command-line pattern that must never be killed
  RAM_LIMIT_MB=512            your plan's RAM, if auto-detection shows the wrong number (see `ramguard status`)\n  RAM_GUARD_MIN_FREE_MB=32    also cut when free RAM < this   RAM_GUARD_TICK=0.1   calm poll seconds (0.02 / 0.01 when RAM fills)\n  RAM_GUARD_NOTIFY=0          no Telegram message   RAM_GUARD_DIR=/tmp/ram_guard   events / state directory
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sysstats import Stats, _read, _int, _kv, _HUGE  # noqa: E402


def _f(name, default):
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return float(default)


THRESHOLD = _f("RAM_GUARD_PERCENT", 91)
WATCH = _f("RAM_GUARD_WATCH", 75)
PREDICT = os.environ.get("RAM_GUARD_PREDICT", "1").strip() != "0"
MIN_MB = _f("RAM_GUARD_MIN_MB", 25)
MIN_GROW_MB = _f("RAM_GUARD_MIN_GROW_MB", 15)
WINDOW = _f("RAM_GUARD_WINDOW", 20)
COOLDOWN = _f("RAM_GUARD_COOLDOWN", 2)
MAX_KILLS = int(_f("RAM_GUARD_MAX_KILLS", 3))          # per incident (RAM still high after a kill -> next culprit)
NOTIFY = os.environ.get("RAM_GUARD_NOTIFY", "1").strip() != "0"
LOOKAHEAD = _f("RAM_GUARD_LOOKAHEAD", 0.4)             # seconds: kill early if RAM would reach ~99% within this time
MIN_FREE_MB = _f("RAM_GUARD_MIN_FREE_MB", 32)          # never let free RAM drop below this (matters on small plans)
SANE_MB = _f("RAM_GUARD_SANE_MB", 8192)                # /proc/meminfo bigger than this = it shows the HOST, not your plan
TICK_CALM, TICK_HOT, TICK_NEAR = _f("RAM_GUARD_TICK", 0.1), 0.02, 0.01   # polling interval (s), faster as RAM fills
STATE_DIR = os.environ.get("RAM_GUARD_DIR", "/tmp/ram_guard")
EVENTS = os.path.join(STATE_DIR, "events.jsonl")

# main functions: never killed. SERVICES also protect their children; the others protect only themselves
# (start.sh is the parent of every orphaned agent process, picoclaw is the parent of every agent command).
SERVICES = {"keep_alive.py", "llm_proxy.py", "ram_guard.py", "tailscaled", "tailscale", "tsports", "rclone", "notify.py"}
SELF_ONLY = {"start.sh", "picoclaw"}
EXTRA = re.compile(os.environ["RAM_GUARD_PROTECT"]) if os.environ.get("RAM_GUARD_PROTECT") else None
SHELLS = {"sh", "bash", "dash", "zsh", "timeout", "nohup", "setsid", "env", "stdbuf", "time"}
INTERP = re.compile(r"^(python[\d.]*|bash|sh|dash|node|perl)$")


# ----------------------------------------------------------------------------------------------- accurate RAM meter
class Meter:
    """RAM used vs. the RAM you were ALLOTTED (not the host's).

    Limit = the smallest of: RAM_LIMIT_MB (you set it), cgroup limit (any level), /proc/meminfo MemTotal (a microVM /
    sandbox shows only its plan there). Used = the HIGHER of: cgroup working set, system used (MemTotal - MemAvailable).
    /proc/meminfo is trusted only when it is not the host's (MemTotal <= SANE_MB and not above the cgroup/env limit).
    The higher of the two numbers decides, so a spike is seen whichever way the platform accounts memory."""

    def __init__(self, stats, meminfo="/proc/meminfo", vmstat="/proc/vmstat", env=None):
        self.st, self.meminfo, self.vmstat = stats, meminfo, vmstat
        self.env = os.environ if env is None else env
        self._lim, self._lim_at = None, 0.0

    def _cg_limit(self):
        best = None
        for d in self.st._dirs:
            for n in ("memory.max", "memory.limit_in_bytes"):
                v = _int(_read(os.path.join(d, n)))
                if v and 0 < v < _HUGE:
                    best = v if best is None else min(best, v)
        return best

    def _meminfo(self):
        total = avail = None
        for line in (_read(self.meminfo) or "").splitlines():
            try:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) * 1024
            except (IndexError, ValueError):
                pass
            if total and avail:
                break
        return total, avail

    def _limits(self, now):
        if self._lim is not None and now - self._lim_at < 30:
            return self._lim
        env_mb = _int(self.env.get("RAM_LIMIT_MB"))
        env_l = env_mb * 1048576 if env_mb else None
        cg_l = self._cg_limit()
        total, _ = self._meminfo()
        trust = bool(total and total <= SANE_MB * 1048576 and (cg_l is None or total <= cg_l)
                     and (env_l is None or total <= env_l))
        cands = [(env_l, "env RAM_LIMIT_MB"), (cg_l, "cgroup limit"), (total if trust else None, "/proc/meminfo (VM total)")]
        cands = [(v, n) for v, n in cands if v]
        if cands:
            limit, src = min(cands)
        else:
            limit, src = 512 * 1048576, "UNKNOWN - assumed 512 MB; set RAM_LIMIT_MB to your plan's RAM"
        self._lim, self._lim_at = (limit, src, trust, cg_l, total), now
        return self._lim

    def read(self):
        now = time.time()
        limit, lsrc, trust, cg_l, total = self._limits(now)
        used = {}
        st = self.st
        if st.version:
            f = st._file("memory.current", "memory.usage_in_bytes")
            usage = _int(_read(f)) if f else None
            if usage is not None:
                stat = _kv(st._file("memory.stat") or "")
                used["cgroup working set"] = max(0, usage - stat.get("inactive_file", stat.get("total_inactive_file", 0)))
        sys_used = None
        if trust:
            tot, avail = self._meminfo()
            if tot and avail is not None:
                sys_used = max(0, tot - avail)
                used["system (MemTotal-MemAvailable)"] = sys_used
        if not used:                                         # no cgroup, no usable meminfo: our own processes
            used["sum of process RSS"] = int(st.memory()["used_mb"] * 1048576)
        src, u = max(used.items(), key=lambda kv: kv[1])
        ev = _kv(st._file("memory.events") or "").get("oom_kill") if st.version == "cgroup-v2" else None
        if ev is None and st.version == "cgroup-v1":
            ev = _kv(st._file("memory.oom_control") or "").get("oom_kill")
        if ev is None and trust:
            ev = _kv(self.vmstat).get("oom_kill")
        mb = lambda b: round(b / 1048576, 1)  # noqa: E731
        return {"total_mb": mb(limit), "used_mb": mb(u), "free_mb": max(0.0, mb(limit - u)),
                "percent": min(100.0, round(100.0 * u / limit, 1)), "source": src, "limit_source": lsrc,
                "oom_kills": ev, "parts": {k: mb(v) for k, v in used.items()},
                "cgroup_limit_mb": mb(cg_l) if cg_l else None, "meminfo_total_mb": mb(total) if total else None,
                "meminfo_trusted": trust}


def effective_threshold(m, thr):
    """91% of the allotted RAM, but never less than MIN_FREE_MB free (tiny plans need an earlier cut)."""
    if MIN_FREE_MB > 0 and m["total_mb"] > 0:
        thr = min(thr, 100.0 * (1 - MIN_FREE_MB / m["total_mb"]))
    return max(50.0, thr)


def log(msg):
    sys.stderr.write(f"[ram-guard] {msg}\n")
    sys.stderr.flush()


# ----------------------------------------------------------------------------------------------- /proc scan
class Proc:
    __slots__ = ("pid", "ppid", "start", "rss", "comm", "cmd", "ident", "state")


def _ident(argv):
    """Who is this really? `python3 /app/keep_alive.py` -> keep_alive.py, `/usr/bin/tailscaled` -> tailscaled."""
    if not argv:
        return ""
    base = os.path.basename(argv[0])
    if INTERP.match(base):
        for a in argv[1:]:
            if a in ("-c", "-m"):                           # inline code / module: no script name to go by
                return base
            if not a.startswith("-"):
                return os.path.basename(a)
    return base


def scan(proc="/proc", page=None):
    page = page or os.sysconf("SC_PAGE_SIZE")
    out = {}
    try:
        names = os.listdir(proc)
    except OSError:
        return out
    for n in names:
        if not n.isdigit():
            continue
        try:
            with open(f"{proc}/{n}/stat", encoding="utf-8", errors="replace") as fh:
                s = fh.read()
            r = s.rfind(")")
            rest = s[r + 2:].split()
            with open(f"{proc}/{n}/statm") as fh:
                rss = int(fh.read().split()[1]) * page
            with open(f"{proc}/{n}/cmdline", "rb") as fh:
                argv = [x.decode("utf-8", "replace") for x in fh.read().split(b"\0") if x]
        except (OSError, ValueError, IndexError):
            continue
        if not argv or rest[0] == "Z":                      # kernel thread / zombie
            continue
        p = Proc()
        p.pid, p.ppid, p.state = int(n), int(rest[1]), rest[0]
        p.start, p.rss = int(rest[19]), rss
        p.comm = s[s.find("(") + 1:r]
        p.cmd, p.ident = " ".join(argv)[:300], _ident(argv)
        out[p.pid] = p
    return out


def protected_pids(procs):
    me = os.getpid()
    prot = {1, me}
    p = procs.get(me)
    while p and p.ppid in procs:                            # my own ancestors (start.sh ...)
        prot.add(p.ppid)
        p = procs[p.ppid]
    for p in procs.values():
        if p.ident in SERVICES or p.ident in SELF_ONLY or (EXTRA and EXTRA.search(p.cmd)):
            prot.add(p.pid)
    changed = True
    while changed:                                          # children of SERVICES are protected too
        changed = False
        for p in procs.values():
            par = procs.get(p.ppid)
            if p.pid not in prot and par and p.ppid in prot and par.ident in SERVICES:
                prot.add(p.pid)
                changed = True
    return prot


def children_map(procs):
    ch = {}
    for p in procs.values():
        ch.setdefault(p.ppid, []).append(p.pid)
    return ch


def tree(root, ch):
    seen, todo = [], [root]
    while todo:
        x = todo.pop()
        if x not in seen:
            seen.append(x)
            todo.extend(ch.get(x, []))
    return seen


def find_jobs(procs):
    """Agent 'jobs' = top-level non-protected processes (shell wrappers are looked through) with their whole tree."""
    prot = protected_pids(procs)
    ch = children_map(procs)
    roots = [p.pid for p in procs.values() if p.pid not in prot and (p.ppid in prot or p.ppid not in procs)]
    jobs, todo = [], list(roots)
    while todo:
        pid = todo.pop()
        p = procs[pid]
        kids = [k for k in ch.get(pid, []) if k not in prot]
        if p.comm in SHELLS and kids:                       # `sh -c "python3 x.py"` -> the job is python3 x.py
            todo.extend(kids)
            continue
        members = [x for x in tree(pid, ch) if x not in prot]
        jobs.append({"pid": pid, "cmd": p.cmd, "start": p.start, "members": members,
                     "rss": sum(procs[x].rss for x in members if x in procs)})
    return jobs


# ----------------------------------------------------------------------------------------------- decision
def pick_culprit(jobs, history, now):
    """history: deque of (t, {job_pid: rss}). Biggest growth inside WINDOW wins; ties / no history -> started last."""
    old = {}
    for t, snap in history:
        if now - t <= WINDOW:
            old = snap
            break
    best, best_key = None, None
    for j in jobs:
        grow = (j["rss"] - old.get(j["pid"], 0)) / 1048576 if old else 0.0
        j["grow_mb"] = round(grow, 1)
        if grow < MIN_GROW_MB and j["rss"] / 1048576 < MIN_MB:
            continue                                        # tiny and not growing: not the problem
        key = (grow >= MIN_GROW_MB, grow if grow >= MIN_GROW_MB else 0.0, j["start"])
        if best_key is None or key > best_key:
            best, best_key = j, key
    return best


def kill_job(job, procs):
    ch = children_map(procs)
    killed = []
    for _ in range(3):                                      # children may fork while we kill
        pids = [x for x in tree(job["pid"], ch) if x in procs and x not in (1, os.getpid())]
        if not pids:
            break
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.05)
        procs = scan()
        ch = children_map(procs)
    return killed


def hints(cmd):
    """Short, concrete 'do it another way' advice for the agent, chosen from what was killed."""
    c = cmd.lower()
    tips = []
    if re.search(r"playwright|chromium|chrome|puppeteer|selenium", c):
        tips.append("browser: use `requests`/`curl_cffi` + BeautifulSoup; if a browser is a must: one page at a time, "
                    "block images/fonts/media, close it every time (see examples/browser_example.py)")
    if re.search(r"pandas|read_csv|numpy|\.py", c):
        tips.append("python: stream line by line / generators, `pd.read_csv(chunksize=50000)`, `del` big objects, "
                    "write results to disk as you go instead of holding everything in a list")
    if re.search(r"node|npm|npx|yarn", c):
        tips.append("node: `NODE_OPTIONS=--max-old-space-size=200`, avoid parallel installs/builds")
    if re.search(r"ffmpeg|convert|magick|pillow|opencv", c):
        tips.append("media: lower resolution/bitrate, `ffmpeg -threads 1`, process one file at a time")
    if re.search(r"pip|apt|npm install|make|gcc|cargo", c):
        tips.append("install/build: one package at a time, `pip install --no-cache-dir`, `make -j1`")
    tips.append("generic: split the work into small batches that finish and free memory; no parallel heavy jobs; "
                "check `ramguard status` BEFORE starting something heavy; cap it: `(ulimit -v 350000; cmd)`")
    return tips


def record(event):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(EVENTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as e:
        log(f"cannot write event: {e!r}")


def notify(text):
    if not NOTIFY:
        return
    try:
        subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify.py"), text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:  # noqa: BLE001
        log(f"notify failed: {e!r}")


_nojob_at = [0.0]


def handle_spike(meter, history, why, thr):
    now = time.time()
    before = meter.read()
    done = 0
    while done < MAX_KILLS:
        procs = scan()
        jobs = find_jobs(procs)
        job = pick_culprit(jobs, history, now)
        mem = meter.read()
        if job is None:
            if done == 0:
                big = sorted(procs.values(), key=lambda p: -p.rss)[:1]
                who = f"{big[0].ident} ({big[0].rss // 1048576} MB)" if big else "?"
                if now - _nojob_at[0] > 300:                # log / record / message at most once per 5 min
                    _nojob_at[0] = now
                    log(f"RAM {mem['percent']}% but no killable agent job (biggest process: {who}); main processes are protected")
                    record({"t": now, "killed": False, "percent": mem["percent"], "biggest": who})
                    notify(f"⚠️ RAM {mem['percent']}% पण kill करण्यासारखा agent process नाही (सर्वात मोठा: {who}). Main processes protected आहेत.")
            return
        killed = kill_job(job, procs)
        after = meter.read()
        for _ in range(10):                                 # memory is returned a few ms after the kill: wait for it
            if after["percent"] < thr - 3:
                break
            time.sleep(0.05)
            after = meter.read()
        ev = {
            "t": now, "killed": True, "why": why, "cmd": job["cmd"], "pid": job["pid"], "pids": killed,
            "job_mb": round(job["rss"] / 1048576, 1), "grew_mb": job.get("grow_mb", 0.0),
            "before_percent": before["percent"], "after_percent": after["percent"], "limit_mb": before["total_mb"],
            "tips": hints(job["cmd"]), "shown": 0,
        }
        record(ev)
        log(f"KILLED '{job['cmd'][:80]}' ({ev['job_mb']} MB, +{ev['grew_mb']} MB/{int(WINDOW)}s) "
            f"RAM {before['percent']}% -> {after['percent']}% of {before['total_mb']} MB")
        notify(f"🛑 RAM guard: RAM {before['percent']}% ({why}) म्हणून हा process kill केला:\n{job['cmd'][:150]}\n"
               f"({ev['job_mb']} MB) → आता RAM {after['percent']}%. Agent ला कारण सांगितलंय, तो दुसरा (हलका) मार्ग वापरेल.")
        done += 1
        before = after
        if after["percent"] < thr - 3:
            return
        history.clear()                                     # RAM still high: re-measure, next culprit


def main():
    global THRESHOLD
    if os.environ.get("RAM_GUARD", "1").strip() == "0":
        log("disabled (RAM_GUARD=0)")
        time.sleep(10 ** 9)
    try:                                                    # if the kernel must kill something, never the guard
        with open("/proc/self/oom_score_adj", "w") as fh:
            fh.write("-1000")
    except OSError:
        pass
    try:                                                    # react first when CPU is contended
        os.setpriority(os.PRIO_PROCESS, 0, -10)
    except (OSError, AttributeError):
        pass
    if os.environ.get("RAM_GUARD_MLOCK", "1") != "0":       # keep guard's pages in RAM: no page-fault stalls under pressure
        try:
            import ctypes
            ctypes.CDLL(None, use_errno=True).mlockall(3)   # MCL_CURRENT | MCL_FUTURE (may be refused: fine)
        except Exception:  # noqa: BLE001
            pass
    stats = Stats(disk_paths=())
    meter = Meter(stats)
    m = meter.read()
    thr = effective_threshold(m, THRESHOLD)
    log(f"allotted RAM {m['total_mb']} MB [{m['limit_source']}]; counting {m['source']}; kill at {thr:.1f}% "
        f"(~{m['total_mb'] * thr / 100:.0f} MB); watch from {WATCH}%")
    if m["limit_source"].startswith("UNKNOWN"):
        notify("⚠️ RAM guard: तुझी allotted RAM detect झाली नाही, 512 MB धरलीय. Koyeb env मध्ये RAM_LIMIT_MB=<तुझा plan RAM> set कर.")
    else:
        notify(f"✅ RAM guard चालू: allotted RAM {m['total_mb']:.0f} MB ({m['limit_source']}), "
               f"{thr:.0f}% ({m['total_mb'] * thr / 100:.0f} MB) ला agent चा spike करणारा job kill होईल.")
    history = deque(maxlen=120)
    last_scan = 0.0
    samples = deque()
    oom_base = m["oom_kills"] or 0
    while True:
        m = meter.read()
        pct, now = m["percent"], time.time()
        ev = m["oom_kills"]
        if ev is not None and ev > oom_base:                # Linux OOM killer still got something: tighten the guard
            oom_base = ev
            THRESHOLD = max(70.0, THRESHOLD - 3)
            thr = effective_threshold(m, THRESHOLD)
            log(f"Linux OOM kill detected (guard was too late) -> threshold lowered to {thr:.1f}%")
            notify(f"⚠️ Linux ने एक process kill केला (guard वेळेत पोहोचला नाही). Guard आता {thr:.0f}% ला कट करेल.")
        hot = pct >= WATCH
        if hot or now - last_scan >= 10:                    # calm: scan only every 10 s (baseline); hot: every tick
            jobs = find_jobs(scan())
            history.append((now, {j["pid"]: j["rss"] for j in jobs}))
            last_scan = now
        samples.append((now, pct))                          # growth rate (% per second) over the last 0.12-0.3 s:
        while samples and now - samples[0][0] > 0.3:        # long enough to ignore one 10 MB step, short enough to
            samples.popleft()                               # catch a real runaway (Chromium start, huge array)
        rate = 0.0
        if samples and now - samples[0][0] >= 0.12:
            rate = (pct - samples[0][1]) / (now - samples[0][0])
        spike = pct >= thr
        why = f"RAM {pct}% >= {thr:.0f}%"
        if PREDICT and pct >= 60 and not spike and rate > 0 and pct + rate * LOOKAHEAD >= 99:
            spike, why = True, f"RAM {pct}% आणि +{rate:.0f}%/s वाढतोय (OOM जवळ)"
        if spike:
            handle_spike(meter, history, why, thr)
            time.sleep(COOLDOWN)
            samples.clear()
            continue
        time.sleep(TICK_NEAR if pct >= thr - 8 else TICK_HOT if hot else TICK_CALM)


def cli(arg):
    stats = Stats(disk_paths=())
    meter = Meter(stats)
    if arg == "status":
        m = meter.read()
        print(f"RAM {m['percent']}%  used {m['used_mb']} / {m['total_mb']} MB  (free {m['free_mb']} MB)  "
              f"guard kills at {effective_threshold(m, THRESHOLD):.1f}%")
        print(f"  allotted RAM from: {m['limit_source']}   counted from: {m['source']}")
        print(f"  cgroup limit: {m['cgroup_limit_mb']} MB | meminfo total: {m['meminfo_total_mb']} MB "
              f"(trusted: {m['meminfo_trusted']}) | readings: {m['parts']}")
        for j in sorted(find_jobs(scan()), key=lambda j: -j["rss"])[:8]:
            print(f"  {j['rss'] // 1048576:>5} MB  pid {j['pid']:<6} {j['cmd'][:90]}")
    elif arg == "log":
        try:
            for line in open(EVENTS, encoding="utf-8").read().splitlines()[-10:]:
                e = json.loads(line)
                print(time.strftime("%H:%M:%S", time.localtime(e["t"])), e.get("cmd") or e.get("biggest"),
                      f"{e.get('before_percent', '')}% -> {e.get('after_percent', '')}%")
        except OSError:
            print("no events yet")
    else:
        print(__doc__)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli(sys.argv[1])
    else:
        main()
