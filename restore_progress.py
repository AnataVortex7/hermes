#!/usr/bin/env python3
"""restore_progress.py - run the Google Drive restore and publish LIVE progress for the dashboard.

Runs one or more `rclone sync` phases one after the other, reads rclone's JSON stats
(--use-json-log --stats 1s) and writes a small state file (default /tmp/restore_progress.json)
that keep_alive.py serves to the /memory dashboard: percent, MB done / total, files, speed, ETA.

Usage (restore):
    restore_progress.py [--attempt N] [--attempts M] -- \
        "Label 1" rclone sync SRC DST [flags...] +++ "Label 2" rclone sync SRC2 DST2 [flags...]
    Exit code: 0 = every phase OK, otherwise the exit code of the phase that failed.

Usage (just update the stage text, e.g. from start.sh):
    restore_progress.py --stage retrying --message "Retrying in 20s" [--clean-start]

Only the Python standard library is used. Total size of every phase is measured up-front with
`rclone size --json` (same filters as the sync), so the percentage and the ETA are real, not guesses.
"""
import collections
import json
import os
import signal
import subprocess
import sys
import threading
import time

STATE_FILE = os.environ.get("RESTORE_PROGRESS_FILE", "/tmp/restore_progress.json")
SIZE_TIMEOUT = int(os.environ.get("RESTORE_SIZE_TIMEOUT", "180"))
LOG_EVERY = 30            # seconds between progress lines in the deploy log
RATE_WINDOW = 20.0        # seconds of history used to smooth speed / ETA
MIN_RATE_SPAN = 3.0       # need at least this much history before an ETA is shown
FILTER_FLAGS = ("--exclude", "--include", "--filter", "--exclude-from", "--include-from", "--filter-from")

_lock = threading.Lock()


# ── state file (atomic write, tolerant read) ──
def read_state(path=None):
    try:
        with open(path or STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state, path=None):
    path = path or STATE_FILE
    state = dict(state)
    state["updated"] = time.time()
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with _lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, path)
    except OSError:
        pass


def update_state(path=None, **fields):
    """Merge fields into the state file (keeps everything else, e.g. clean_start / ready)."""
    st = read_state(path)
    st.update(fields)
    write_state(st, path)
    return st


# ── helpers ──
def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def fmt_eta(sec):
    if sec is None:
        return "calculating..."
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"


def size_command(cmd):
    """`rclone sync SRC DST flags...` -> `rclone size --json SRC <only the filter flags>` (same file set)."""
    args = cmd[2:]
    positional = []
    flags = ["--fast-list"]
    i = 0
    while i < len(args):
        a = args[i]
        name = a.split("=", 1)[0]
        if name in FILTER_FLAGS:
            if "=" in a:
                flags.append(a)
            elif i + 1 < len(args):
                flags += [a, args[i + 1]]
                i += 1
        elif a.startswith("-"):
            # other flags: skip; value-taking ones we know about consume their value
            if "=" not in a and name in ("--drive-chunk-size", "--transfers", "--checkers", "--bwlimit", "--stats",
                                       "--drive-pacer-min-sleep", "--drive-pacer-burst"):
                i += 1
        else:
            positional.append(a)
        i += 1
    if not positional:
        return None
    return [cmd[0], "size", "--json", positional[0]] + flags


def measure(cmd):
    """(files, bytes) of the remote side of a sync command, or None when it cannot be measured."""
    sc = size_command(cmd)
    if not sc:
        return None
    try:
        out = subprocess.run(sc, capture_output=True, text=True, timeout=SIZE_TIMEOUT)
        if out.returncode != 0:
            return None
        d = json.loads(out.stdout.strip().splitlines()[-1])
        return int(d.get("count", 0)), int(d.get("bytes", 0))
    except Exception:  # noqa: BLE001
        return None


class Tracker:
    """Combines all phases into one overall progress figure."""

    def __init__(self, phases, attempt=1, attempts=0):
        self.t0 = time.time()
        self.attempt = attempt
        self.attempts = attempts
        self.phases = phases            # [{label, total_bytes, total_files, known, done_bytes, done_files, finished}]
        self.idx = 0
        self.stage = "scanning"
        self.message = "Scanning Google Drive backup..."
        self.current = []
        self.error = ""
        self.samples = collections.deque()   # (t, bytes, files)
        self.speed_hint = 0.0

    # -- derived numbers --
    def totals(self):
        tb = sum(p["total_bytes"] for p in self.phases)
        tf = sum(p["total_files"] for p in self.phases)
        db = sum(p["total_bytes"] if p["finished"] else min(p["done_bytes"], p["total_bytes"]) for p in self.phases)
        df = sum(p["total_files"] if p["finished"] else min(p["done_files"], p["total_files"]) for p in self.phases)
        return tb, db, tf, df

    def percent(self):
        tb, db, tf, df = self.totals()
        if all(p["known"] for p in self.phases):
            if tb >= 1024 * 1024:                    # real data: weigh by bytes
                return round(100.0 * db / tb, 1)
            if tf > 0:                               # only tiny files: weigh by file count
                return round(100.0 * df / tf, 1)
            return 100.0 if self.stage == "restored" else 0.0
        # some phase could not be measured: fall back to "phases finished + share of the current one"
        n = max(1, len(self.phases))
        fin = sum(1 for p in self.phases if p["finished"])
        cur = self.phases[self.idx] if self.idx < len(self.phases) else None
        share = 0.0
        if cur and not cur["finished"] and cur["total_bytes"] > 0:
            share = min(1.0, cur["done_bytes"] / cur["total_bytes"])
        return round(100.0 * (fin + share) / n, 1)

    def sample(self):
        _, db, _, df = self.totals()
        now = time.time()
        self.samples.append((now, db, df))
        while self.samples and now - self.samples[0][0] > RATE_WINDOW:
            self.samples.popleft()

    def rates(self):
        if len(self.samples) < 2:
            return None, None, 0.0
        (t1, b1, f1), (t2, b2, f2) = self.samples[0], self.samples[-1]
        span = t2 - t1
        if span < MIN_RATE_SPAN:
            return None, None, span
        return max(0.0, (b2 - b1) / span), max(0.0, (f2 - f1) / span), span

    def eta(self):
        tb, db, tf, df = self.totals()
        if self.stage != "restoring":
            return None
        if not all(p["known"] for p in self.phases):
            return None
        rb, rf, _ = self.rates()
        if rb is None:
            return None
        candidates = []
        if tb - db > 0 and rb > 0:
            candidates.append((tb - db) / rb)
        if tf - df > 0 and rf and rf > 0:
            candidates.append((tf - df) / rf)
        if tb - db <= 0 and tf - df <= 0:
            return 0
        # both bytes and files must finish -> the slower of the two decides
        return max(candidates) if candidates else None

    def snapshot(self):
        tb, db, tf, df = self.totals()
        rb, _, _ = self.rates()
        cur = self.phases[self.idx] if self.idx < len(self.phases) else self.phases[-1]
        return {
            "stage": self.stage,
            "message": self.message,
            "attempt": self.attempt,
            "attempts": self.attempts,
            "phase": min(self.idx + 1, len(self.phases)),
            "phases": len(self.phases),
            "label": cur["label"],
            "percent": self.percent(),
            "bytes_done": int(db),
            "bytes_total": int(tb),
            "files_done": int(df),
            "files_total": int(tf),
            "speed_bps": int(rb if rb is not None else self.speed_hint),
            "eta_seconds": (None if self.eta() is None else int(round(self.eta()))),
            "elapsed_seconds": int(time.time() - self.t0),
            "estimated": not all(p["known"] for p in self.phases),
            "current": self.current[:3],
            "error": self.error,
        }

    def publish(self):
        st = read_state()
        st.update(self.snapshot())
        st["ready"] = False
        write_state(st)

    def log_line(self):
        s = self.snapshot()
        return (f">> Restore {s['percent']:.0f}% - {fmt_bytes(s['bytes_done'])} / {fmt_bytes(s['bytes_total'])}, "
                f"{s['files_done']}/{s['files_total']} files, {fmt_bytes(s['speed_bps'])}/s, ETA {fmt_eta(s['eta_seconds'])}"
                f" ({s['label']}, phase {s['phase']}/{s['phases']})")


child_procs = []


def run_phase(tr, i, cmd, last_errors):
    """Run one rclone sync; feed its JSON stats into the tracker. Returns rclone's exit code."""
    ph = tr.phases[i]
    full = list(cmd) + ["--use-json-log", "--stats", "1s", "--stats-log-level", "NOTICE"]
    tr.idx = i
    tr.stage = "restoring"
    tr.message = f"Restoring from Google Drive ({ph['label']})"
    tr.publish()
    try:
        proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except OSError as e:
        msg = f"cannot start rclone: {e}"
        print(f"ERROR: {msg}", flush=True)
        last_errors.append(msg)
        return 127
    child_procs.append(proc)

    def pump_stdout():
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                print(line, flush=True)

    threading.Thread(target=pump_stdout, daemon=True).start()
    last_log = time.time()
    for raw in proc.stderr:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except ValueError:
            print(raw, flush=True)
            last_errors.append(raw)
            continue
        st = ev.get("stats")
        if isinstance(ev, dict) and isinstance(st, dict):
            ph["done_bytes"] = int(st.get("bytes") or 0)
            ph["done_files"] = int(st.get("transfers") or 0)
            # learn / correct totals from rclone (needed when the size scan failed; keeps the bar <= 100% otherwise)
            ph["total_bytes"] = max(ph["total_bytes"], int(st.get("totalBytes") or 0))
            ph["total_files"] = max(ph["total_files"], int(st.get("totalTransfers") or 0))
            tr.speed_hint = float(st.get("speed") or 0)
            tr.current = [
                {"name": os.path.basename(str(t.get("name", ""))), "percent": int(t.get("percentage") or 0),
                 "size": int(t.get("size") or 0)}
                for t in (st.get("transferring") or [])
            ]
            tr.sample()
            tr.publish()
            if time.time() - last_log >= LOG_EVERY:
                print(tr.log_line(), flush=True)
                last_log = time.time()
        else:
            level = str(ev.get("level", "info")).upper()
            msg = str(ev.get("msg", raw))
            print(f"{level}: {msg}", flush=True)
            if level in ("ERROR", "CRITICAL", "FATAL"):
                last_errors.append(msg)
    rc = proc.wait()
    child_procs.remove(proc)
    return rc


def parse_phases(argv):
    """['Label', 'rclone', 'sync', ..., '+++', 'Label2', ...] -> [(label, [cmd...]), ...]"""
    groups, cur = [], []
    for a in argv:
        if a == "+++":
            groups.append(cur)
            cur = []
        else:
            cur.append(a)
    groups.append(cur)
    out = []
    for g in groups:
        if len(g) < 5 or g[2] != "sync":
            raise SystemExit(f"bad phase (need: LABEL rclone sync SRC DST ...): {g}")
        out.append((g[0], g[1:]))
    return out


def cmd_restore(attempt, attempts, argv):
    phases_spec = parse_phases(argv)
    phases = [{"label": lab, "total_bytes": 0, "total_files": 0, "known": False, "done_bytes": 0,
               "done_files": 0, "finished": False} for lab, _ in phases_spec]
    tr = Tracker(phases, attempt, attempts)
    tr.publish()

    def _term(*_):
        for p in list(child_procs):
            try:
                p.terminate()
            except OSError:
                pass
        sys.exit(143)

    signal.signal(signal.SIGTERM, _term)

    # 1) measure every phase (in parallel) so percent / ETA are based on the real total
    print(">> Measuring Drive backup size...", flush=True)
    results = [None] * len(phases_spec)

    def work(i):
        results[i] = measure(phases_spec[i][1])

    threads = [threading.Thread(target=work, args=(i,)) for i in range(len(phases_spec))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for i, r in enumerate(results):
        if r is not None:
            phases[i]["total_files"], phases[i]["total_bytes"] = r
            phases[i]["known"] = True
    if all(p["known"] for p in phases):
        tb, tf = sum(p["total_bytes"] for p in phases), sum(p["total_files"] for p in phases)
        print(f">> Backup size: {tf} files, {fmt_bytes(tb)}", flush=True)
    else:
        print(">> Could not measure backup size - progress will be approximate", flush=True)

    # 2) run the phases
    last_errors = []
    for i, (_label, cmd) in enumerate(phases_spec):
        rc = run_phase(tr, i, cmd, last_errors)
        if rc != 0:
            tr.stage = "failed"
            tr.error = "\n".join(last_errors[-5:])[-900:] or f"rclone exited with code {rc}"
            tr.message = f"Restore failed (attempt {attempt})"
            tr.current = []
            tr.publish()
            return rc
        phases[i]["finished"] = True
        phases[i]["done_bytes"], phases[i]["done_files"] = phases[i]["total_bytes"], phases[i]["total_files"]
        tr.sample()

    tr.stage = "restored"
    tr.message = "Restore complete"
    tr.current = []
    tr.publish()
    return 0


def main(argv):
    attempt, attempts, stage, message, clean = 1, 0, None, "", False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            return cmd_restore(attempt, attempts, argv[i + 1:])
        if a == "--attempt":
            attempt = int(argv[i + 1]); i += 1
        elif a == "--attempts":
            attempts = int(argv[i + 1]); i += 1
        elif a == "--stage":
            stage = argv[i + 1]; i += 1
        elif a == "--message":
            message = argv[i + 1]; i += 1
        elif a == "--clean-start":
            clean = True
        else:
            raise SystemExit(f"unknown option {a}")
        i += 1
    if stage:
        fields = {"stage": stage, "message": message}
        if clean:
            fields["clean_start"] = True
        update_state(**fields)
        return 0
    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
