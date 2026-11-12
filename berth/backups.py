"""Scheduled backups of databases and volumes.

Schedules live in settings.json ("backups"); what happened (last run, result) lives in a separate state file, so the
app and `berth --run-backups` (a systemd user timer on Linux) never overwrite each other's bookkeeping. Backups run
while Berth is open — also in the tray — and missed ones are caught up at start. On Linux a timer also runs them
while Berth is closed. Old files beyond "keep" are removed, only ones Berth named for that schedule."""
import json
import os
import shutil
import subprocess
import sys
import time
import uuid

from gi.repository import GLib

from . import db, dbtools

STATE = os.path.join(GLib.get_user_state_dir(), "berth", "backups.json")
LOCK = os.path.join(GLib.get_user_cache_dir(), "berth", "backups.lock")
EVERY = [(1, "Every hour"), (6, "Every 6 hours"), (24, "Every day"), (168, "Every week")]


def default_folder():
    docs = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS) or os.path.expanduser("~")
    return os.path.join(docs, "Berth backups")


def new_schedule(kind, target, db_name="", every=24, keep=7, folder=None):
    return {"id": uuid.uuid4().hex[:10], "kind": kind, "target": target, "db": db_name or "", "every": int(every),
            "keep": int(keep), "folder": folder or default_folder()}


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def prefix(s):
    """Files of one schedule start with this (and nothing else Berth writes does)."""
    return dbtools.safe(s["target"]) + ("-" + dbtools.safe(s["db"]) if s.get("db") else "") + "-"


def files_of(s):
    """This schedule's backups, newest first: [(path, size, mtime)]."""
    out = []
    try:
        for name in os.listdir(s["folder"]):
            if name.startswith(prefix(s)) and not name.endswith(".part"):
                p = os.path.join(s["folder"], name)
                st = os.stat(p)
                out.append((p, st.st_size, st.st_mtime))
    except OSError:
        pass
    return sorted(out, key=lambda x: -x[2])


def is_due(s, state, now=None):
    last = (state.get(s["id"]) or {}).get("last", 0)
    return (now or time.time()) - last >= s["every"] * 3600 - 60


def next_run(s, state):
    last = (state.get(s["id"]) or {}).get("last", 0)
    return max(time.time(), last + s["every"] * 3600)


def run_one(engine, s, progress=None):
    """Make one backup now. Returns (path, bytes)."""
    os.makedirs(s["folder"], exist_ok=True)
    if s["kind"] == "volume":
        from .files import export_volume
        dest = os.path.join(s["folder"], f"{prefix(s)}{dbtools.stamp()}.tar.gz")
        part = dest + ".part"
        try:
            n = export_volume(engine, s["target"], part)
            os.replace(part, dest)
        finally:
            if os.path.exists(part):
                os.remove(part)
        return dest, n
    try:
        ins = engine.inspect(s["target"])
    except Exception as e:
        raise db.DbError(f"The container {s['target']} isn't there any more ({e})")
    b = db.open_backend(engine, ins)
    if b is None or not dbtools.can_backup(b):
        raise db.DbError(f"{s['target']} isn't a database Berth can back up")
    b.connect()
    if s.get("db"):
        b.db = s["db"]
    dest = os.path.join(s["folder"], f"{prefix(s)}{dbtools.stamp()}.{dbtools.BACKUP_EXT[b.kind]}")
    part = dest + ".part"
    try:
        n = dbtools.backup(b, part, progress)
        os.replace(part, dest)
    finally:
        if os.path.exists(part):
            os.remove(part)
    return dest, n


def prune(s):
    for p, _size, _t in files_of(s)[max(1, int(s.get("keep", 7))):]:
        try:
            os.remove(p)
        except OSError:
            pass


def _lock():
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    try:
        if os.path.exists(LOCK) and time.time() - os.path.getmtime(LOCK) > 6 * 3600:
            os.remove(LOCK)                      # left behind by a crash
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except OSError:
        return False


def run_due(engine, schedules, only=None, force=False):
    """Run the schedules that are due (or `only` one id, now). Returns [(schedule, ok, message)]."""
    if not _lock():
        return []
    results = []
    try:
        for s in schedules:
            if only and s["id"] != only:
                continue
            state = load_state()
            if not force and not is_due(s, state):
                continue
            try:
                path, n = run_one(engine, s)
                prune(s)
                ok, msg = True, f"{os.path.basename(path)} · {_size(n)}"
            except Exception as e:                 # one failing schedule must not stop the others
                ok, msg = False, str(e)[:400]
            state = load_state()
            state[s["id"]] = {"last": time.time(), "ok": ok, "message": msg}
            _save_state(state)
            results.append((s, ok, msg))
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass
    return results


def _size(n):
    return db._size(n)


# ── Linux: a user timer, so backups also happen while Berth is closed ──
UNIT = "berth-backups"


def timer_supported():
    return sys.platform.startswith("linux") and not os.environ.get("FLATPAK_ID") and bool(shutil.which("systemctl"))


def _berth_cmd():
    exe = shutil.which("berth")
    if exe:
        return [exe]
    return [sys.executable, "-m", "berth"]


def set_timer(on):
    """Install (or remove) an hourly user timer that runs `berth --run-backups`. Returns True when active."""
    if not timer_supported():
        return False
    d = os.path.join(GLib.get_user_config_dir(), "systemd", "user")
    svc, tim = os.path.join(d, UNIT + ".service"), os.path.join(d, UNIT + ".timer")
    try:
        if not on:
            subprocess.run(["systemctl", "--user", "disable", "--now", UNIT + ".timer"], capture_output=True, timeout=20)
            for p in (svc, tim):
                if os.path.exists(p):
                    os.remove(p)
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=20)
            return False
        os.makedirs(d, exist_ok=True)
        cmd = " ".join(_quote(a) for a in _berth_cmd() + ["--run-backups"])
        with open(svc, "w") as f:
            f.write(f"[Unit]\nDescription=Berth scheduled backups\n\n[Service]\nType=oneshot\nExecStart={cmd}\n")
        with open(tim, "w") as f:
            f.write("[Unit]\nDescription=Berth scheduled backups (hourly check)\n\n[Timer]\nOnBootSec=10min\n"
                    "OnUnitActiveSec=1h\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=20)
        r = subprocess.run(["systemctl", "--user", "enable", "--now", UNIT + ".timer"], capture_output=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _quote(a):
    return '"' + a.replace('"', '\\"') + '"' if any(c in a for c in ' "\'\\') else a


def cli():
    """`berth --run-backups`: run what is due and exit (no window). Exit code 1 if any backup failed."""
    from .engine import Engine
    path = os.path.join(GLib.get_user_config_dir(), "berth", "settings.json")
    try:
        with open(path) as f:
            settings = json.load(f)
    except (OSError, ValueError):
        settings = {}
    schedules = settings.get("backups") or []
    if not schedules:
        return 0
    try:
        engine = Engine(settings.get("engine") or None)
        engine.req("GET", "/_ping", timeout=5)
    except Exception as e:
        print(f"berth: the Docker engine isn't reachable: {e}", file=sys.stderr)
        return 0                                   # not an error: Docker is simply off; the next run catches up
    res = run_due(engine, schedules)
    for s, ok, msg in res:
        print(f"{'ok  ' if ok else 'FAIL'} {s['target']}: {msg}")
    return 1 if any(not ok for _s, ok, _m in res) else 0
