#!/usr/bin/env python3
"""adb-transfer: a small local web UI for pushing/pulling files with adb.

Stdlib only. Binds to 127.0.0.1 and requires a per-run token, so nothing
outside this machine (or a stray webpage) can drive your device.
"""

import json
import os
import pty
import queue
import re
import secrets
import shutil
import signal
import stat
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import displays

HERE = os.path.dirname(os.path.abspath(__file__))
def _load_token():
    """Keep the token across restarts so an open tab does not silently 403."""
    tf = os.path.join(HERE, ".token")
    try:
        with open(tf) as f:
            tok = f.read().strip()
        if len(tok) >= 24:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        fd = os.open(tf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok)
    except OSError:
        pass
    return tok


TOKEN = _load_token()
ADB = shutil.which("adb") or "adb"

# ---------------------------------------------------------------- adb helpers


def sh_quote(p):
    return "'" + p.replace("'", "'\\''") + "'"


def adb_cmd(serial, *args):
    cmd = [ADB]
    if serial:
        cmd += ["-s", serial]
    return cmd + list(args)


def run(serial, *args, timeout=25):
    try:
        r = subprocess.run(
            adb_cmd(serial, *args),
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "adb timed out"
    except FileNotFoundError:
        return 1, "", "adb not found on PATH"


def list_devices():
    code, out, err = run(None, "devices", "-l")
    if code != 0:
        return [], err.strip() or "could not run adb"
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial, state = parts[0], parts[1]
        props = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        label = props.get("model") or props.get("device") or serial
        devices.append({
            "serial": serial,
            "state": state,
            "label": label.replace("_", " "),
        })
    return devices, None


# mtime column: the first "YYYY-MM-DD HH:MM" in an `ls -l` line. Everything
# after it is the filename (which may contain spaces); the last integer
# before it is the size. Column padding is variable, so anchor on this.
DT_RE = re.compile(r"\s(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}(?::\d{2})?)\s")


def parse_ls_line(line):
    """Parse one `ls -AlpL` line. Names may contain spaces and brackets."""
    m = DT_RE.search(line)
    if not m:
        return None
    name = line[m.end():].strip()
    if not name or name in (".", ".."):
        return None
    head = line[:m.start()].split()
    perms = head[0][1:10] if head and len(head[0]) >= 10 else ""
    size = 0
    if head:
        try:
            size = int(head[-1].replace(",", ""))
        except ValueError:
            size = 0
    is_dir = name.endswith("/")
    if is_dir:
        name = name[:-1]
    if " -> " in name:  # symlink that could not be resolved
        name = name.split(" -> ", 1)[0]
    return {
        "name": name,
        "isDir": is_dir,
        "size": 0 if is_dir else size,
        "mtime": m.group(1) + " " + m.group(2)[:5],
        "perms": perms,
        "mode": perms_to_octal(perms),
    }


def device_list(serial, path):
    path = path or "/sdcard"
    code, out, err = run(
        serial, "shell", f"cd {sh_quote(path)} && ls -AlpL --color=never"
    )
    if code != 0:
        return None, (err.strip() or out.strip() or "cannot open folder")
    entries = []
    for line in out.splitlines():
        if not line.strip() or line.startswith("total "):
            continue
        e = parse_ls_line(line.rstrip("\r"))
        if e:
            entries.append(e)
    return entries, None


def device_free(serial, path):
    code, out, _ = run(serial, "shell", f"df -h {sh_quote(path)}")
    if code != 0:
        return None
    lines = out.strip().splitlines()
    if len(lines) < 2:
        return None
    parts = lines[-1].split()
    return parts[3] if len(parts) >= 4 else None


def local_list(path):
    path = os.path.abspath(os.path.expanduser(path or os.path.expanduser("~")))
    if not os.path.isdir(path):
        return None, None, f"not a folder: {path}"
    entries = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    is_dir = e.is_dir()
                    st = e.stat()
                    perm = stat.filemode(st.st_mode)[1:10]
                    entries.append({
                        "name": e.name,
                        "isDir": is_dir,
                        "perms": perm,
                        "mode": perms_to_octal(perm),
                        "size": 0 if is_dir else st.st_size,
                        "mtime": time.strftime(
                            "%Y-%m-%d %H:%M", time.localtime(st.st_mtime)
                        ),
                    })
                except OSError:
                    continue
    except PermissionError:
        return None, None, f"permission denied: {path}"
    return path, entries, None


# ------------------------------------------------------------------- transfers

PCT_RE = re.compile(r"\[\s*(\d+)%\]\s*(.*)$")
SUMMARY_RE = re.compile(r"(\d+) bytes in ([\d.]+)s")

JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_Q = queue.Queue()
JOB_SEQ = [0]


def new_job(direction, serial, src, dst, name, size):
    with JOBS_LOCK:
        JOB_SEQ[0] += 1
        jid = JOB_SEQ[0]
        JOBS[jid] = {
            "id": jid, "direction": direction, "serial": serial,
            "src": src, "dst": dst, "name": name, "size": size,
            "status": "queued", "percent": 0, "detail": "",
            "error": "", "bytes": 0, "speed": "", "eta": "",
            "started": 0, "elapsed": 0, "proc": None, "cancel": False,
        }
    JOB_Q.put(jid)
    return jid


def job_public(j):
    return {k: v for k, v in j.items() if k not in ("proc",)}


def run_job(jid):
    with JOBS_LOCK:
        j = JOBS[jid]
        if j["cancel"]:
            j["status"] = "canceled"
            return
        j["status"] = "running"
        j["started"] = time.time()
        direction, serial, src, dst = (
            j["direction"], j["serial"], j["src"], j["dst"]
        )
        size = j["size"]

    verb = "push" if direction == "push" else "pull"
    cmd = adb_cmd(serial, verb, src, dst)

    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            cmd, stdout=slave, stderr=slave, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        os.close(master)
        os.close(slave)
        with JOBS_LOCK:
            JOBS[jid].update(status="error", error=str(exc))
        return
    os.close(slave)
    with JOBS_LOCK:
        JOBS[jid]["proc"] = proc

    tail = ""
    buf = b""
    try:
        while True:
            try:
                chunk = os.read(master, 8192)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            text = buf.decode("utf-8", "replace")
            buf = b""
            for piece in re.split(r"[\r\n]", text):
                piece = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", piece).strip()
                if not piece:
                    continue
                tail = piece
                m = PCT_RE.search(piece)
                if m:
                    pct = int(m.group(1))
                    with JOBS_LOCK:
                        jj = JOBS[jid]
                        jj["percent"] = pct
                        jj["detail"] = os.path.basename(m.group(2).strip())
                        el = time.time() - jj["started"]
                        jj["elapsed"] = el
                        if size and pct > 0 and el > 0.5:
                            done = size * pct / 100.0
                            rate = done / el
                            jj["bytes"] = int(done)
                            jj["speed"] = human(rate) + "/s"
                            if rate > 0:
                                jj["eta"] = fmt_eta((size - done) / rate)
    finally:
        try:
            os.close(master)
        except OSError:
            pass
    rc = proc.wait()

    with JOBS_LOCK:
        j = JOBS[jid]
        j["elapsed"] = time.time() - j["started"]
        j["proc"] = None
        if j["cancel"]:
            j["status"] = "canceled"
            j["detail"] = "canceled"
        elif rc == 0:
            j["status"] = "done"
            j["percent"] = 100
            j["eta"] = ""
            sm = SUMMARY_RE.search(tail)
            if sm:
                nbytes, secs = int(sm.group(1)), float(sm.group(2))
                j["bytes"] = nbytes
                if secs > 0:
                    j["speed"] = human(nbytes / secs) + "/s"
            j["detail"] = "completed"
        else:
            j["status"] = "error"
            j["error"] = tail or f"adb exited with code {rc}"


def worker():
    while True:
        jid = JOB_Q.get()
        try:
            run_job(jid)
        except Exception as exc:  # keep the worker alive
            with JOBS_LOCK:
                JOBS[jid].update(status="error", error=repr(exc))
        finally:
            JOB_Q.task_done()


def external_transfers(exclude_pids=()):
    """adb push/pull processes started outside this app (e.g. a terminal).

    Unplugging while one of those is mid-write would truncate the file, so
    the safe-disconnect flow refuses until they finish.
    """
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) in exclude_pids:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = [a.decode("utf-8", "replace")
                        for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if len(args) < 2 or os.path.basename(args[0]) != "adb":
            continue
        if any(a in ("push", "pull") for a in args[1:5]):
            found.append({"pid": int(pid), "cmd": " ".join(args)})
    return found


def safe_disconnect(serial, force=False):
    """Flush pending writes so the cable can be pulled without corruption."""
    with JOBS_LOCK:
        active = [j["name"] for j in JOBS.values()
                  if j["status"] in ("queued", "running")]
        our_pids = {j["proc"].pid for j in JOBS.values() if j.get("proc")}
    if active:
        return {"blocked": "active", "names": active}

    ext = external_transfers(exclude_pids=our_pids)
    if ext and not force:
        return {"blocked": "external", "procs": ext}

    steps = []
    # sync(2) on the device: push cached writes down to flash. After a large
    # transfer this is the step that makes unplugging safe.
    code, out, err = run(serial, "shell", "sync", timeout=180)
    if code != 0:
        return {"error": err.strip() or out.strip() or "sync failed"}
    steps.append("Flushed pending writes to storage")

    # Belt and braces: on Android 11+ adb push already registers files with
    # MediaStore via FUSE, so this is usually a no-op, but it costs ~100ms
    # and covers files replaced in place.
    code, _, _ = run(
        serial, "shell",
        "content call --uri content://media/external "
        "--method scan_volume --arg external_primary",
        timeout=60,
    )
    if code == 0:
        steps.append("Refreshed the phone's media library")

    # A network device (adb connect host:port) can be genuinely detached.
    # A USB device cannot -- it is just safe to physically unplug.
    over_network = ":" in serial and not serial.startswith("usb:")
    if over_network:
        code, out, err = run(None, "disconnect", serial, timeout=20)
        if code != 0:
            return {"error": err.strip() or out.strip() or "disconnect failed"}
        steps.append(f"Detached {serial}")

    return {"ok": True, "steps": steps, "network": over_network,
            "forced": bool(ext)}


SPECIAL = {"s": True, "S": True, "t": True, "T": True}


def mount_id(serial, side, path):
    """Filesystem id, so we can tell an instant rename from a real copy."""
    if side == "device":
        code, out, _ = run(serial, "shell", f"stat -c %d {sh_quote(path)}")
        out = out.strip()
        return out if code == 0 and out.isdigit() else None
    try:
        return str(os.stat(path).st_dev)
    except OSError:
        return None


def move_entries(side, serial, items, dest, dry_run=False):
    """Move items into dest, refusing anything that would lose data."""
    dest = (dest or "").rstrip("/") or "/"
    results = []
    for it in items:
        src = it.get("path") or ""
        name = it.get("name") or os.path.basename(src.rstrip("/"))
        if not src or not name:
            continue
        parent = src.rstrip("/").rsplit("/", 1)[0] or "/"
        target = dest + "/" + name

        if parent == dest:
            results.append({"name": name, "error": "Already in this folder."})
            continue
        # Moving a folder inside itself would eat it.
        if dest == src.rstrip("/") or dest.startswith(src.rstrip("/") + "/"):
            results.append({"name": name,
                            "error": "Cannot move a folder into itself."})
            continue
        if path_busy(src):
            results.append({"name": name,
                            "error": "It is part of a transfer in progress."})
            continue
        if dry_run:
            results.append({"name": name, "ok": True})
            continue

        if side == "device":
            # mv -n exits 0 even when it refuses to clobber, so verify after.
            script = (
                f"if [ ! -e {sh_quote(src)} ]; then echo ERR:missing; "
                f"elif [ ! -d {sh_quote(dest)} ]; then echo ERR:nodest; "
                f"elif [ -e {sh_quote(target)} ]; then echo ERR:exists; "
                f"else mv -n {sh_quote(src)} {sh_quote(dest)}/; "
                f"if [ -e {sh_quote(target)} ] && [ ! -e {sh_quote(src)} ]; "
                f"then echo OK; else echo ERR:failed; fi; fi"
            )
            # A cross-filesystem move copies the bytes, so allow real time.
            code, out, err = run(serial, "shell", script, timeout=900)
            res = out.strip().splitlines()[-1].strip() if out.strip() else ""
            msg = {
                "OK": None,
                "ERR:exists": f"\u201c{name}\u201d already exists there.",
                "ERR:missing": f"\u201c{name}\u201d is no longer there.",
                "ERR:nodest": "That destination folder is gone.",
            }.get(res, err.strip() or out.strip() or "move failed")
            results.append({"name": name} | ({} if msg is None else {"error": msg}))
            if msg is None:
                results[-1]["ok"] = True
        else:
            if not os.path.lexists(src):
                results.append({"name": name, "error": f"\u201c{name}\u201d is no longer there."})
            elif not os.path.isdir(dest):
                results.append({"name": name, "error": "That destination folder is gone."})
            elif os.path.lexists(target):
                results.append({"name": name, "error": f"\u201c{name}\u201d already exists there."})
            else:
                try:
                    shutil.move(src, target)
                    results.append({"name": name, "ok": True})
                except (OSError, shutil.Error) as exc:
                    results.append({"name": name, "error": str(exc)})
    return results


# Paths that must never be handed to rm -rf, even by accident.
PROTECTED = {
    "/", "/sdcard", "/storage", "/storage/emulated", "/storage/emulated/0",
    "/storage/self", "/storage/self/primary", "/data", "/data/local",
    "/data/local/tmp", "/system", "/mnt", "/mnt/user", "/mnt/user/0",
}


def delete_entries(side, serial, items, dry_run=False):
    """Delete items, refusing anything protected or mid-transfer."""
    results = []
    for it in items:
        # "/".rstrip("/") is "", which would fall through the guards below
        src = (it.get("path") or "").rstrip("/") or (
            "/" if it.get("path") else "")
        name = it.get("name") or os.path.basename(src) or src
        is_dir = bool(it.get("isDir"))
        if not src or not name:
            continue
        if src in PROTECTED or os.path.expanduser("~").rstrip("/") == src:
            results.append({"name": name,
                            "error": "That folder is protected."})
            continue
        if path_busy(src):
            results.append({"name": name,
                            "error": "It is part of a transfer in progress."})
            continue

        if dry_run:
            info = {"name": name, "isDir": is_dir,
                    "size": int(it.get("size") or 0), "ok": True}
            if is_dir:
                if side == "device":
                    code, out, _ = run(
                        serial, "shell",
                        f"find {sh_quote(src)} -type f 2>/dev/null | wc -l",
                        timeout=60)
                    if code == 0 and out.strip().isdigit():
                        info["files"] = int(out.strip())
                else:
                    n = 0
                    for _r, _d, fs in os.walk(src):
                        n += len(fs)
                    info["files"] = n
            results.append(info)
            continue

        if side == "device":
            script = (
                f"if [ ! -e {sh_quote(src)} ]; then echo ERR:missing; "
                f"else rm -rf {sh_quote(src)}; "
                f"if [ -e {sh_quote(src)} ]; then echo ERR:failed; "
                f"else echo OK; fi; fi"
            )
            code, out, err = run(serial, "shell", script, timeout=300)
            res = out.strip().splitlines()[-1].strip() if out.strip() else ""
            if res == "OK":
                results.append({"name": name, "ok": True})
            elif res == "ERR:missing":
                results.append({"name": name, "error": "Already gone."})
            else:
                results.append({"name": name,
                                "error": err.strip() or out.strip()
                                or "could not delete"})
        else:
            try:
                if os.path.islink(src) or os.path.isfile(src):
                    os.remove(src)
                elif os.path.isdir(src):
                    shutil.rmtree(src)
                else:
                    results.append({"name": name, "error": "Already gone."})
                    continue
                results.append({"name": name, "ok": True})
            except OSError as exc:
                results.append({"name": name,
                                "error": exc.strerror or str(exc)})
    return results


def perms_to_octal(perm):
    """'rw-rw----' -> '660'. Handles setuid/setgid/sticky as a 4th digit."""
    if len(perm) < 9:
        return ""
    digits, special = [], 0
    for i in range(3):
        r, w, x = perm[i * 3], perm[i * 3 + 1], perm[i * 3 + 2]
        bits = (4 if r == "r" else 0) + (2 if w == "w" else 0)
        if x in ("x", "s", "t"):
            bits += 1
        if x in ("s", "S"):
            special |= 4 if i == 0 else 2
        if x in ("t", "T"):
            special |= 1
        digits.append(str(bits))
    return ("" if not special else str(special)) + "".join(digits)


# Emulated storage is a FUSE mount: modes are synthesized from the file's
# location and owning app, so chmod there exits 0 but changes nothing.
EMULATED = ("/sdcard", "/storage/emulated", "/mnt/user", "/storage/self")


def is_emulated(path):
    return any(path == e or path.startswith(e + "/") for e in EMULATED)


MODE_RE = re.compile(r"^[0-7]{3,4}$")


def chmod_entry(side, serial, parent, name, mode):
    """Change a mode, then re-read it and report what actually happened."""
    mode = (mode or "").strip()
    if not MODE_RE.match(mode):
        return {"error": "Mode must be 3 or 4 octal digits, e.g. 664."}
    full = parent.rstrip("/") + "/" + name

    if side == "device":
        if is_emulated(full):
            return {
                "error": "This is the phone's emulated storage (a FUSE mount). "
                         "Android synthesises these permissions from the "
                         "folder and owning app, so they cannot be changed "
                         "here \u2014 chmod reports success but does nothing.",
                "emulated": True,
            }
        code, out, err = run(
            serial, "shell",
            f"chmod {mode} {sh_quote(full)} 2>&1; stat -c %a {sh_quote(full)}",
            timeout=30,
        )
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        if code != 0 or not lines:
            return {"error": err.strip() or out.strip() or "chmod failed"}
        actual = lines[-1]
        if not MODE_RE.match(actual):
            return {"error": actual}
        if actual.lstrip("0") != mode.lstrip("0"):
            return {"error": f"The phone kept the mode at {actual}. This "
                             f"filesystem does not allow that change.",
                    "mode": actual}
        return {"ok": True, "mode": actual}

    try:
        os.chmod(full, int(mode, 8))
        actual = oct(os.stat(full).st_mode & 0o7777)[2:]
    except OSError as exc:
        return {"error": exc.strerror or str(exc)}
    if actual.lstrip("0") != mode.lstrip("0"):
        return {"error": f"Mode stayed at {actual}.", "mode": actual}
    return {"ok": True, "mode": actual}


def path_busy(path):
    """True if a queued/running transfer touches this path."""
    with JOBS_LOCK:
        for j in JOBS.values():
            if j["status"] not in ("queued", "running"):
                continue
            if j["src"] == path or j["src"].startswith(path.rstrip("/") + "/"):
                return True
            if j["dst"].rstrip("/") == path.rstrip("/"):
                return True
    return False


def rename_entry(side, serial, parent, old, new):
    """Rename one item, refusing to overwrite anything that already exists."""
    new = new.strip()
    if not new or "/" in new or new in (".", ".."):
        return {"error": "Invalid name."}
    if new == old:
        return {"ok": True}

    src = parent.rstrip("/") + "/" + old
    if path_busy(src):
        return {"error": "That item is part of a transfer in progress."}

    if side == "device":
        # mv -n exits 0 even when it declines to clobber, so check first and
        # verify after rather than trusting the exit code.
        script = (
            f"cd {sh_quote(parent)} || {{ echo ERR:nodir; exit 0; }}; "
            f"if [ -e {sh_quote(new)} ]; then echo ERR:exists; "
            f"elif [ ! -e {sh_quote(old)} ]; then echo ERR:missing; "
            f"else mv -n {sh_quote(old)} {sh_quote(new)}; "
            f"if [ -e {sh_quote(new)} ] && [ ! -e {sh_quote(old)} ]; "
            f"then echo OK; else echo ERR:failed; fi; fi"
        )
        code, out, err = run(serial, "shell", script, timeout=30)
        res = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if code != 0 and not res:
            return {"error": err.strip() or "rename failed"}
        return {
            "OK": {"ok": True},
            "ERR:exists": {"error": f"\u201c{new}\u201d already exists here."},
            "ERR:missing": {"error": f"\u201c{old}\u201d is no longer there."},
            "ERR:nodir": {"error": "That folder is no longer available."},
        }.get(res, {"error": err.strip() or out.strip() or "rename failed"})

    dst = os.path.join(parent, new)
    if os.path.lexists(dst):
        return {"error": f"\u201c{new}\u201d already exists here."}
    if not os.path.lexists(os.path.join(parent, old)):
        return {"error": f"\u201c{old}\u201d is no longer there."}
    try:
        os.rename(os.path.join(parent, old), dst)
    except OSError as exc:
        return {"error": exc.strerror or str(exc)}
    return {"ok": True}


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_eta(secs):
    secs = int(max(0, secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs // 3600}h {(secs % 3600) // 60}m"


# ---------------------------------------------------------------- http server


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        # Local-only bind + custom header + token: blocks cross-site requests.
        if self.headers.get("X-ADB-Transfer") != TOKEN:
            self._send(403, {"error": "forbidden"})
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path

        if p == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                html = f.read().replace(b"__TOKEN__", TOKEN.encode())
            return self._send(200, html, "text/html; charset=utf-8")

        if p == "/displays.js":
            with open(os.path.join(HERE, "displays.js"), "rb") as f:
                return self._send(200, f.read(),
                                  "text/javascript; charset=utf-8")

        if not p.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        if not self._authed():
            return

        if p == "/api/devices":
            devs, err = list_devices()
            return self._send(200, {"devices": devs, "error": err})

        if p == "/api/local":
            path = (q.get("path") or [os.path.expanduser("~")])[0]
            real, entries, err = local_list(path)
            if err:
                return self._send(200, {"error": err})
            du = shutil.disk_usage(real)
            return self._send(200, {
                "path": real, "entries": entries,
                "free": human(du.free),
                "home": os.path.expanduser("~"),
            })

        if p == "/api/device":
            serial = (q.get("serial") or [""])[0]
            path = (q.get("path") or ["/sdcard"])[0]
            entries, err = device_list(serial, path)
            if err:
                return self._send(200, {"error": err})
            return self._send(200, {
                "path": path, "entries": entries,
                "free": device_free(serial, path),
                "emulated": is_emulated(path),
            })

        if p == "/api/displays":
            return self._send(200, displays.state())

        if p == "/api/jobs":
            with JOBS_LOCK:
                jobs = [job_public(j) for j in JOBS.values()]
            jobs.sort(key=lambda j: j["id"], reverse=True)
            return self._send(200, {"jobs": jobs})

        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        if not p.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        if not self._authed():
            return
        body = self._body()

        if p.startswith("/api/displays/"):
            return self._send(200, display_action(p[len("/api/displays/"):],
                                                  body))

        if p == "/api/transfer":
            direction = body.get("direction")
            serial = body.get("serial") or ""
            dest = body.get("dest") or ""
            items = body.get("items") or []
            if direction not in ("push", "pull") or not dest or not items:
                return self._send(400, {"error": "bad request"})
            if not dest.endswith("/"):
                dest += "/"
            ids = []
            for it in items:
                src = it.get("path")
                if not src:
                    continue
                ids.append(new_job(
                    direction, serial, src, dest,
                    it.get("name") or os.path.basename(src.rstrip("/")),
                    int(it.get("size") or 0),
                ))
            return self._send(200, {"queued": ids})

        if p == "/api/cancel":
            jid = int(body.get("id") or 0)
            with JOBS_LOCK:
                j = JOBS.get(jid)
                if not j:
                    return self._send(404, {"error": "no such job"})
                j["cancel"] = True
                proc = j["proc"]
                if j["status"] == "queued":
                    j["status"] = "canceled"
            if proc and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except OSError:
                    proc.terminate()
            return self._send(200, {"ok": True})

        if p == "/api/disconnect":
            serial = body.get("serial") or ""
            if not serial:
                return self._send(400, {"error": "no device"})
            return self._send(
                200, safe_disconnect(serial, bool(body.get("force")))
            )

        if p == "/api/rename":
            old = body.get("old") or ""
            parent = body.get("path") or ""
            if not old or not parent:
                return self._send(400, {"error": "bad request"})
            return self._send(200, rename_entry(
                body.get("side"), body.get("serial") or "",
                parent, old, body.get("new") or "",
            ))

        if p == "/api/chmod":
            return self._send(200, chmod_entry(
                body.get("side"), body.get("serial") or "",
                body.get("path") or "", body.get("name") or "",
                body.get("mode") or "",
            ))

        if p == "/api/move":
            side = body.get("side")
            serial = body.get("serial") or ""
            dest = body.get("dest") or ""
            items = body.get("items") or []
            if not dest or not items:
                return self._send(400, {"error": "bad request"})
            if body.get("dryRun"):
                src_parent = (items[0].get("path") or "").rstrip("/")
                src_parent = src_parent.rsplit("/", 1)[0] or "/"
                a = mount_id(serial, side, src_parent)
                b = mount_id(serial, side, dest)
                return self._send(200, {
                    "results": move_entries(side, serial, items, dest, True),
                    "sameMount": bool(a and b and a == b),
                })
            return self._send(
                200, {"results": move_entries(side, serial, items, dest)}
            )

        if p == "/api/delete":
            items = body.get("items") or []
            if not items:
                return self._send(400, {"error": "bad request"})
            return self._send(200, {"results": delete_entries(
                body.get("side"), body.get("serial") or "",
                items, bool(body.get("dryRun")),
            )})

        if p == "/api/clear":
            with JOBS_LOCK:
                for jid in [
                    k for k, v in JOBS.items()
                    if v["status"] in ("done", "error", "canceled")
                ]:
                    del JOBS[jid]
            return self._send(200, {"ok": True})

        if p == "/api/mkdir":
            name = (body.get("name") or "").strip()
            parent = body.get("path") or ""
            if not name or "/" in name:
                return self._send(400, {"error": "invalid folder name"})
            if body.get("side") == "device":
                full = parent.rstrip("/") + "/" + name
                code, out, err = run(
                    body.get("serial") or "", "shell",
                    f"mkdir -p {sh_quote(full)}",
                )
                if code != 0:
                    return self._send(
                        200, {"error": err.strip() or out.strip() or "failed"}
                    )
            else:
                try:
                    os.makedirs(os.path.join(parent, name), exist_ok=True)
                except OSError as exc:
                    return self._send(200, {"error": str(exc)})
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})


def display_action(action, body):
    name = body.get("name") or ""
    if action == "power":
        return displays.set_power(name, bool(body.get("on")))
    if action == "apply":
        outs = body.get("outputs")
        if not isinstance(outs, list) or not outs:
            return {"error": "bad request"}
        return displays.apply(outs)
    if action == "keep":
        return displays.keep()
    if action == "revert":
        return displays.revert()
    if action == "brightness":
        return displays.set_gamma_brightness(name, body.get("value"))
    if action == "backlight":
        return displays.set_backlight(name, body.get("percent"))
    if action == "layout/save":
        return displays.save_layout(body.get("title"), body.get("outputs") or [])
    if action == "layout/delete":
        return displays.delete_layout(body.get("title") or "")
    if action == "layout/apply":
        want, err = displays.resolve_layout(body.get("title") or "")
        return {"error": err} if err else displays.apply(want)
    return {"error": "unknown display action"}


def main():
    port = int(os.environ.get("ADB_TRANSFER_PORT", "8767"))
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  adb-transfer running at {url}")
    print("  Press Ctrl+C to stop.\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")


if __name__ == "__main__":
    main()
