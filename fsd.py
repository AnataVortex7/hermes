"""fsd.py - tiny file-manager API for the hermes box. stdlib only, no extra process.

health.py starts it as a daemon thread (one idle thread, ~0 CPU, ~1 MB RAM). It listens on 127.0.0.1 ONLY;
start.sh publishes that port with `tailscale serve --tcp`, so only devices on your tailnet can reach it
(same model as sshd). No password / token, by design.

Everything is streamed in 256 KB chunks, so RAM stays flat for any file size.

  GET  /fs/ping                         host, root, disk free/total
  GET  /fs/list?path=/etc               directory listing
  GET  /fs/counts?path=/etc[&start=0&n=60]   per sub-folder "N folders / M files" (batched, time-boxed)
  GET  /fs/search?path=/etc&q=name[&kind=all|files|folders&limit=1000]   recursive search (q may use * ?; empty q = list everything)
  GET  /fs/read?path=/x.bin[&dl=1]      download / inline view (HTTP Range supported)
  PUT  /fs/write?path=/x.bin[&overwrite=1]   upload / save (body = raw bytes, Content-Length required)
  POST /fs/op  {"op": "mkdir"|"delete"|"rename"|"move"|"copy", ...}   (needs header X-Requested-With: fsd)

Env: FS_ENABLED (1), FS_PORT (8765), FS_ROOT (/ = whole machine; set e.g. /root to confine it).
"""
import os, re, json, stat, shutil, socket, ipaddress, mimetypes, threading, time, errno, fnmatch
from collections import deque
from urllib.parse import urlsplit, parse_qs, quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("FS_PORT", "8765") or 8765)
ROOT = os.path.realpath(os.environ.get("FS_ROOT", "/") or "/")
CHUNK = 256 * 1024
MAX_LIST = 10000
COUNT_CAP = 5000                          # a folder with more entries than this is shown as "5000+"
PSEUDO = ("/proc", "/sys", "/dev")        # virtual filesystems: never walked by search (still listable by hand)
HOST_RE = re.compile(r"^(?:\[([0-9a-f:.]+)\]|([^:\[\]]+))(?::\d+)?$")


class FsErr(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def host_ok(h):
    """Anti DNS-rebinding: accept only IPs, single-label names (MagicDNS short names) and *.ts.net."""
    m = HOST_RE.match((h or "").strip().lower())
    name = (m.group(1) or m.group(2)) if m else ""
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return "." not in name or name.endswith(".ts.net")


def resolve(p):
    """virtual path -> real path, confined to ROOT."""
    p = p or "/"
    full = os.path.normpath(os.path.join(ROOT, p.lstrip("/")))
    if ROOT != "/":
        if full != ROOT and not full.startswith(ROOT + os.sep):
            raise FsErr(403, "outside root")
        real = os.path.realpath(full)
        if real != ROOT and not real.startswith(ROOT + os.sep):
            raise FsErr(403, "outside root (symlink)")
    return full


def virt(full):
    rel = os.path.relpath(full, ROOT)
    return "/" if rel == "." else "/" + rel.replace(os.sep, "/")


def oserr(e):
    code = {errno.ENOENT: 404, errno.EEXIST: 409, errno.ENOTEMPTY: 409, errno.EACCES: 403, errno.EPERM: 403,
            errno.ENOSPC: 507, errno.EISDIR: 400, errno.ENOTDIR: 400}.get(e.errno, 500)
    return FsErr(code, e.strerror or str(e))


def unique(path):
    if not os.path.lexists(path):
        return path
    base, ext = os.path.splitext(path) if not os.path.isdir(path) else (path, "")
    n = 1
    while True:
        cand = f"{base} (copy{'' if n == 1 else ' ' + str(n)}){ext}"
        if not os.path.lexists(cand):
            return cand
        n += 1


def disk():
    try:
        s = os.statvfs(ROOT)
        return s.f_bavail * s.f_frsize, s.f_blocks * s.f_frsize
    except Exception:
        return 0, 0


def _place(p):
    try:
        real = os.path.realpath(p)
        if not os.path.isdir(real):
            return None
        if ROOT != "/" and real != ROOT and not real.startswith(ROOT + os.sep):
            return None
        return virt(real)
    except OSError:
        return None


def places():
    """Quick links for the Machines sidebar: where the AI agent keeps its files, home, /tmp."""
    home = os.path.expanduser("~")
    ph = os.environ.get("PICOCLAW_HOME") or os.path.join(home, ".picoclaw")
    ws = os.environ.get("PICOCLAW_WORKSPACE") or os.path.join(ph, "workspace")
    out, seen = [], set()
    for name, path in (("Agent workspace", ws), ("Home", home), ("/tmp", "/tmp")):
        v = _place(path)
        if v and v not in seen:
            seen.add(v)
            out.append({"name": name, "path": v})
    return out


def do_list(path):
    full = resolve(path)
    items, trunc = [], False
    try:
        with os.scandir(full) as it:
            for e in it:
                if len(items) >= MAX_LIST:
                    trunc = True
                    break
                try:
                    st = e.stat(follow_symlinks=False)
                    link = stat.S_ISLNK(st.st_mode)
                    isdir = e.is_dir(follow_symlinks=True) if link else stat.S_ISDIR(st.st_mode)
                    if link:
                        try:
                            st = e.stat()
                        except OSError:
                            pass
                    items.append({"n": e.name, "d": isdir, "s": 0 if isdir else st.st_size,
                                  "m": int(st.st_mtime), "l": link})
                except OSError:
                    items.append({"n": e.name, "d": False, "s": 0, "m": 0, "l": False, "x": True})
    except OSError as e:
        raise oserr(e)
    parent = None if virt(full) == "/" else virt(os.path.dirname(full))
    return {"ok": True, "path": virt(full), "parent": parent, "items": items, "truncated": trunc}

def _isdir(e):
    try:
        return e.is_dir()                 # follows symlinks, same as do_list
    except OSError:
        return False


def _pseudo(v):
    return any(v == p or v.startswith(p + "/") for p in PSEUDO)


def do_counts(path, start=0, n=60, budget=2.0):
    """{name: [sub-folders, files, capped] | None} for sub-folders [start, start+n) of `path`; stops early after `budget` s."""
    full = resolve(path)
    try:
        with os.scandir(full) as it:
            names = sorted(e.name for e in it if _isdir(e))
    except OSError as e:
        raise oserr(e)
    t0, out, i = time.time(), {}, max(0, start)
    end = min(len(names), i + max(1, n))
    while i < end:
        if out and time.time() - t0 > budget:
            break
        name = names[i]
        i += 1
        d = f = 0
        capped = False
        try:
            with os.scandir(os.path.join(full, name)) as it:
                for k, e in enumerate(it):
                    if k >= COUNT_CAP:
                        capped = True
                        break
                    if _isdir(e):
                        d += 1
                    else:
                        f += 1
            out[name] = [d, f, capped]
        except OSError:
            out[name] = None              # no permission / vanished
    return {"ok": True, "counts": out, "next": i if i < len(names) else None, "total": len(names)}


def make_matcher(q):
    q = (q or "").strip().lower()
    if not q:
        return lambda name: True
    if any(c in q for c in "*?["):
        return lambda name: fnmatch.fnmatchcase(name.lower(), q)
    return lambda name: q in name.lower()


def do_search(path, q, kind="all", limit=1000, budget=10.0):
    """Breadth-first recursive search below `path` (symlinked folders are not followed; /proc /sys /dev are skipped)."""
    full = resolve(path)
    match = make_matcher(q)
    kind = kind if kind in ("all", "files", "folders") else "all"
    limit = max(1, min(int(limit or 1000), 5000))
    skip_pseudo = not _pseudo(virt(full))
    out, scanned, trunc, timeup = [], 0, False, False
    t0 = time.time()
    todo = deque([full])
    while todo and not trunc:
        if time.time() - t0 > budget:
            timeup = True
            break
        d = todo.popleft()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        scanned += 1
        entries.sort(key=lambda e: e.name.lower())
        for e in entries:
            try:
                link = e.is_symlink()
                isdir = e.is_dir()
            except OSError:
                continue
            fp = os.path.join(d, e.name)
            if isdir and not link and not (skip_pseudo and _pseudo(virt(fp))):
                todo.append(fp)
            if (kind == "files" and isdir) or (kind == "folders" and not isdir) or not match(e.name):
                continue
            try:
                st = e.stat(follow_symlinks=False)
                size, mt = (0 if isdir else st.st_size), int(st.st_mtime)
            except OSError:
                size, mt = 0, 0
            out.append({"n": virt(fp), "d": isdir, "s": size, "m": mt, "l": link})
            if len(out) >= limit:
                trunc = True
                break
    return {"ok": True, "path": virt(full), "items": out, "truncated": trunc, "timeup": timeup, "scanned": scanned}


def _one(fn, path, errs):
    try:
        fn()
        return 1
    except FsErr as e:
        errs.append({"path": path, "error": e.msg})
    except OSError as e:
        errs.append({"path": path, "error": e.strerror or str(e)})
    except shutil.Error as e:
        errs.append({"path": path, "error": str(e)[:200]})
    return 0


def do_op(b):
    op = b.get("op")
    errs, done = [], 0
    if op == "mkdir":
        full = resolve(b.get("path"))
        try:
            os.makedirs(full, exist_ok=False)
        except OSError as e:
            raise oserr(e)
        return {"ok": True, "done": 1, "errors": []}
    if op == "rename":
        full = resolve(b.get("path"))
        name = str(b.get("name") or "")
        if not name or "/" in name or name in (".", "..") or "\0" in name:
            raise FsErr(400, "bad name")
        if full == ROOT:
            raise FsErr(400, "cannot rename root")
        dst = os.path.join(os.path.dirname(full), name)
        if os.path.lexists(dst):
            raise FsErr(409, "name already exists")
        try:
            os.rename(full, dst)
        except OSError as e:
            raise oserr(e)
        return {"ok": True, "done": 1, "errors": []}
    paths = b.get("paths")
    if not isinstance(paths, list) or not paths:
        raise FsErr(400, "paths required")
    if op == "delete":
        for p in paths:
            full = resolve(p)

            def rm(full=full):
                if full == ROOT or full == "/":
                    raise FsErr(400, "refusing to delete the root")
                if os.path.islink(full) or not os.path.isdir(full):
                    os.remove(full)
                else:
                    shutil.rmtree(full)
            done += _one(rm, p, errs)
        return {"ok": not errs, "done": done, "errors": errs}
    if op in ("move", "copy"):
        dest = resolve(b.get("dest"))
        if not os.path.isdir(dest):
            raise FsErr(400, "destination is not a folder")
        for p in paths:
            src = resolve(p)

            def go(src=src):
                if src == ROOT or src == "/":
                    raise FsErr(400, "cannot " + op + " the root")
                dst = os.path.join(dest, os.path.basename(src))
                if os.path.isdir(src) and not os.path.islink(src) and (dst + os.sep).startswith(src + os.sep):
                    raise FsErr(400, "cannot put a folder inside itself")
                if op == "copy":
                    dst = unique(dst)
                    if os.path.isdir(src) and not os.path.islink(src):
                        shutil.copytree(src, dst, symlinks=True)
                    else:
                        shutil.copy2(src, dst, follow_symlinks=False)
                else:
                    if os.path.lexists(dst):
                        if os.path.samefile(src, dst):
                            return
                        raise FsErr(409, "already exists in destination")
                    shutil.move(src, dst)
            done += _one(go, p, errs)
        return {"ok": not errs, "done": done, "errors": errs}
    raise FsErr(400, "unknown op")


class H(BaseHTTPRequestHandler):
    server_version = "fsd"
    timeout = 120           # a stalled client never holds a thread forever

    def log_message(self, *a):
        pass

    # ---- helpers ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, e):
        try:
            self._json({"ok": False, "error": e.msg}, e.code)
        except Exception:
            pass

    def _gate(self, mutating):
        if not host_ok(self.headers.get("Host")):
            raise FsErr(421, "bad host")
        if mutating and self.headers.get("X-Requested-With") != "fsd":
            raise FsErr(400, "missing X-Requested-With: fsd")

    def _dispatch(self, method):
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        try:
            self._gate(method != "GET")
            if not u.path.startswith("/fs/"):
                raise FsErr(404, "not found")
            route = u.path[4:]
            if method == "GET" and route == "ping":
                free, total = disk()
                return self._json({"ok": True, "host": socket.gethostname(), "root": ROOT, "free": free, "total": total, "places": places()})
            if method == "GET" and route == "list":
                return self._json(do_list(q.get("path")))
            if method == "GET" and route == "counts":
                return self._json(do_counts(q.get("path"), int(q.get("start") or 0), min(int(q.get("n") or 60), 200)))
            if method == "GET" and route == "search":
                return self._json(do_search(q.get("path"), q.get("q"), q.get("kind", "all"), int(q.get("limit") or 1000)))
            if method == "GET" and route == "read":
                return self._read(q)
            if method == "PUT" and route == "write":
                return self._write(q)
            if method == "POST" and route == "op":
                n = int(self.headers.get("Content-Length") or 0)
                if n > 4 << 20:
                    raise FsErr(413, "too large")
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    raise FsErr(400, "bad json")
                return self._json(do_op(body))
            raise FsErr(404, "not found")
        except FsErr as e:
            self._err(e)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except ValueError:
            self._err(FsErr(400, "bad number in query"))
        except Exception as e:  # never let one request kill the thread silently
            self._err(FsErr(500, f"{type(e).__name__}: {e}"[:200]))

    do_GET = lambda self: self._dispatch("GET")
    do_PUT = lambda self: self._dispatch("PUT")
    do_POST = lambda self: self._dispatch("POST")

    # ---- download ----
    def _read(self, q):
        full = resolve(q.get("path"))
        try:
            st = os.stat(full)
        except OSError as e:
            raise oserr(e)
        if not stat.S_ISREG(st.st_mode):
            raise FsErr(400, "not a regular file")
        name = os.path.basename(full) or "file"
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            f = open(full, "rb")
        except OSError as e:
            raise oserr(e)
        with f:
            size = st.st_size
            if size == 0:       # /proc, /sys style files report 0: read what is really there (capped)
                data = f.read(1 << 20)
                size = len(data)
                f_iter = None
            else:
                data, f_iter = None, f
            start, end, code = 0, size - 1, 200
            rng = self.headers.get("Range")
            if rng and f_iter:
                m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                    else:
                        start = max(size - int(m.group(2)), 0)
                    if start >= size or start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    code = 206
            length = max(end - start + 1, 0)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", self.date_time_string(st.st_mtime))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Disposition",
                             ("attachment" if q.get("dl") == "1" else "inline") + "; filename*=UTF-8''" + quote(name))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if data is not None:
                self.wfile.write(data)
                return
            f.seek(start)
            left = length
            while left > 0:
                buf = f.read(min(CHUNK, left))
                if not buf:
                    break
                self.wfile.write(buf)
                left -= len(buf)

    # ---- upload / save ----
    def _write(self, q):
        full = resolve(q.get("path"))
        if full == ROOT:
            raise FsErr(400, "bad path")
        cl = self.headers.get("Content-Length")
        if cl is None or not cl.isdigit():
            raise FsErr(411, "Content-Length required")
        left = int(cl)
        exists = os.path.lexists(full)
        if exists and q.get("overwrite") != "1":
            raise FsErr(409, "already exists")
        if exists and os.path.isdir(full):
            raise FsErr(400, "is a folder")
        d = os.path.dirname(full)
        if not os.path.isdir(d):
            raise FsErr(404, "parent folder missing")
        tmp = os.path.join(d, f".{os.path.basename(full)}.fsd{os.getpid()}-{threading.get_ident()}")
        try:
            with open(tmp, "wb") as out:
                while left > 0:
                    buf = self.rfile.read(min(CHUNK, left))
                    if not buf:
                        raise FsErr(400, "upload interrupted")
                    out.write(buf)
                    left -= len(buf)
            if exists:
                try:
                    shutil.copymode(full, tmp)
                except OSError:
                    pass
            os.replace(tmp, full)
        except OSError as e:
            self._cleanup(tmp)
            raise oserr(e)
        except BaseException:
            self._cleanup(tmp)
            raise
        self._json({"ok": True, "path": virt(full), "size": int(cl)})

    @staticmethod
    def _cleanup(p):
        try:
            os.remove(p)
        except OSError:
            pass


class Srv(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16


def serve():
    while True:            # survive e.g. "address in use" during a quick restart
        try:
            Srv(("127.0.0.1", PORT), H).serve_forever()
        except Exception as e:
            print(f"fsd: {e}; retrying in 5s", flush=True)
            time.sleep(5)


def start():
    if os.environ.get("FS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        print("fsd: disabled (FS_ENABLED=0)", flush=True)
        return None
    t = threading.Thread(target=serve, daemon=True, name="fsd")
    t.start()
    print(f"fsd: file API on 127.0.0.1:{PORT} root={ROOT} (publish with tailscale serve --tcp={PORT})", flush=True)
    return t


if __name__ == "__main__":
    start()
    while True:
        time.sleep(3600)
