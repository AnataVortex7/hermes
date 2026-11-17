#!/usr/bin/env python3
"""Google Drive backup status: written by start.sh (CLI below), read by the /memory dashboard (keep_alive.py).

Before this, a failed `rclone sync` was swallowed (`|| true`), so nobody could tell whether the 5-minute backup
really worked. Now every run records start / result / duration / error in a small JSON file (BACKUP_STATUS_FILE,
default /tmp/backup_status.json), and the dashboard shows "last backup ... ago" plus a "Backup now" button.

CLI (used by start.sh; never raises, so a status problem can never break a backup):
    backup_status.py start <full|quick>
    backup_status.py done  <full|quick> <ok|fail> <seconds> [error text]
    backup_status.py disabled [reason]
Manual trigger: the dashboard creates BACKUP_TRIGGER_FILE; the sync loop in start.sh sees it within a second.
"""
import json
import os
import sys
import time

STATE_FILE = os.environ.get("BACKUP_STATUS_FILE", "/tmp/backup_status.json")
TRIGGER_FILE = os.environ.get("BACKUP_TRIGGER_FILE", "/tmp/backup_now")
HISTORY_MAX = 20
RUNNING_STALE_SECS = 3600          # a "running" backup older than this is considered hung


def interval_secs():
    try:
        return max(30, int(float(os.environ.get("BACKUP_INTERVAL", "300") or 300)))
    except ValueError:
        return 300


def read_state(path=None):
    try:
        with open(path or STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state, path=None):
    path = path or STATE_FILE
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def mark_start(kind="full", path=None, now=None):
    st = read_state(path)
    st.update(state="running", started_at=now or time.time(), kind=kind)
    write_state(st, path)
    return st


def mark_done(kind, ok, seconds, error="", path=None, now=None):
    now = now or time.time()
    st = read_state(path)
    st.update(state="ok" if ok else "failed", kind=kind, finished_at=now, last_duration_s=int(seconds or 0))
    st.pop("started_at", None)
    if ok:
        st.update(last_ok_at=now, last_error="", consecutive_failures=0, ok_count=int(st.get("ok_count", 0)) + 1)
    else:
        st.update(last_fail_at=now, last_error=(error or "unknown error")[:400],
                  consecutive_failures=int(st.get("consecutive_failures", 0)) + 1,
                  fail_count=int(st.get("fail_count", 0)) + 1)
    hist = list(st.get("history") or [])
    hist.append({"t": now, "ok": bool(ok), "secs": int(seconds or 0), "kind": kind})
    st["history"] = hist[-HISTORY_MAX:]
    write_state(st, path)
    return st


def mark_disabled(reason="Drive sync is off for this run", path=None, now=None):
    st = read_state(path)
    st.update(state="disabled", disabled_reason=reason, finished_at=now or time.time())
    write_state(st, path)
    return st


def request_now(path=None, trigger=None, now=None):
    """The dashboard button. Returns 'queued' | 'already_queued' | 'running' | 'error'."""
    trigger = trigger or TRIGGER_FILE
    now = now or time.time()
    if os.path.exists(trigger):
        return "already_queued"
    st = read_state(path)
    if st.get("state") == "running" and now - float(st.get("started_at") or 0) < RUNNING_STALE_SECS:
        return "running"
    try:
        with open(trigger, "w", encoding="utf-8") as fh:
            fh.write(str(int(now)))
        return "queued"
    except OSError:
        return "error"


def summary(now=None, path=None, trigger=None):
    """Everything the dashboard needs, already computed (ages in seconds, flags)."""
    now = now or time.time()
    trigger = trigger or TRIGGER_FILE
    st = read_state(path)
    interval = interval_secs()
    state = st.get("state") or "never"
    started = st.get("started_at")
    last_ok = st.get("last_ok_at")
    running_secs = int(now - started) if state == "running" and started else None
    stalled = bool(running_secs is not None and running_secs > RUNNING_STALE_SECS)
    # no good backup for more than 3 intervals (and not busy right now) = something is wrong
    overdue = bool(state != "disabled" and last_ok and now - last_ok > 3 * interval and state != "running")
    out = {
        "state": state,
        "interval_s": interval,
        "pending": os.path.exists(trigger),
        "last_ok_at": last_ok,
        "last_ok_ago_s": int(now - last_ok) if last_ok else None,
        "last_fail_ago_s": int(now - st["last_fail_at"]) if st.get("last_fail_at") else None,
        "last_duration_s": st.get("last_duration_s"),
        "last_kind": st.get("kind"),
        "last_error": st.get("last_error") or "",
        "consecutive_failures": int(st.get("consecutive_failures", 0)),
        "ok_count": int(st.get("ok_count", 0)),
        "fail_count": int(st.get("fail_count", 0)),
        "running_s": running_secs,
        "stalled": stalled,
        "overdue": overdue,
        "disabled_reason": st.get("disabled_reason") or "",
        "history": [{"ago_s": int(now - h.get("t", now)), "ok": bool(h.get("ok")), "secs": h.get("secs", 0)}
                    for h in (st.get("history") or [])[-10:]],
    }
    out["alert"] = alert_text(out)
    return out


def alert_text(s):
    """One-line warning for the dashboard's red alert box, or ''."""
    if s["state"] == "disabled":
        return "Drive backup is OFF for this run: " + (s["disabled_reason"] or "restore did not finish") + "."
    if s["stalled"]:
        return f"A Drive backup has been running for {s['running_s'] // 60} min - rclone may be stuck."
    if s["consecutive_failures"] >= 2:
        return (f"Drive backup failed {s['consecutive_failures']} times in a row"
                + (": " + s["last_error"][:160] if s["last_error"] else "") + ".")
    if s["overdue"]:
        return f"No successful Drive backup for {s['last_ok_ago_s'] // 60} min (it should run every {s['interval_s'] // 60} min)."
    return ""


def main(argv):
    if len(argv) < 2:
        return 0
    cmd = argv[1]
    try:
        if cmd == "start":
            mark_start(argv[2] if len(argv) > 2 else "full")
        elif cmd == "done" and len(argv) >= 5:
            mark_done(argv[2], argv[3] == "ok", float(argv[4]), " ".join(argv[5:]))
        elif cmd == "disabled":
            mark_disabled(" ".join(argv[2:]) or "Drive sync is off for this run")
    except Exception as exc:  # noqa: BLE001 - status is best effort, never break the backup
        print(f"backup_status: {exc!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
