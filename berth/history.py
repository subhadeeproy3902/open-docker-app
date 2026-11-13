"""Metrics history (#28), opt-in: one sample a minute of every running container's CPU and memory, plus what happened
(start, stop, crash, out of memory, restart, image pull) from the engine's events — written to
~/.local/share/berth/metrics/<day>.jsonl while Berth (or its tray icon) runs. Nothing runs when Berth is closed.
Kept for the chosen number of days and at most 100 MB; older days are deleted."""
import json
import os
import re
import time

from gi.repository import GLib

from .stats import _cgroup_dir, _read

DIR = os.path.join(GLib.get_user_data_dir(), "berth", "metrics")
CAP = 100 * 2**20
EVENTS = {"start": "started", "die": "stopped", "oom": "ran out of memory", "restart": "restarted",
          "pull": "pulled"}           # (kill is left out: a normal stop sends it too)
COLOUR = {"start": "#2ec27e", "die": "#77767b", "crash": "#e01b24", "oom": "#e01b24", "restart": "#e5a50a",
          "pull": "#3584e4"}
RANK = {"oom": 5, "crash": 4, "restart": 3, "die": 2, "start": 1, "pull": 1}


def _name(c):
    return ((c.get("Names") or ["?"])[0]).lstrip("/")


class Recorder:
    def __init__(self):
        self.prev = {}                   # cid -> (time, cpu seconds)
        self.last_events = time.time()

    def tick(self, engine, containers, keep_days=7):
        """Runs on a worker thread, once a minute."""
        now = time.time()
        local = engine.path.startswith(("/", "unix://"))
        sample = {}
        meta = {}
        for c in containers:
            if c.get("State") != "running":
                continue
            cid = c["Id"]
            cpu_s = mem = None
            cg = _cgroup_dir(cid) if local else None
            if cg:
                m = re.search(r"usage_usec (\d+)", _read(cg + "/cpu.stat"))
                cpu_s = int(m.group(1)) / 1e6 if m else None
                cur = int(_read(cg + "/memory.current") or 0)
                inact = re.search(r"^inactive_file (\d+)", _read(cg + "/memory.stat"), re.M)
                mem = max(0, cur - (int(inact.group(1)) if inact else 0))
            else:
                try:
                    st = engine.stats_once(cid)
                    cpu_s = st["cpu_stats"]["cpu_usage"]["total_usage"] / 1e9
                    ms = st.get("memory_stats") or {}
                    mem = max(0, ms.get("usage", 0) - (ms.get("stats") or {}).get("inactive_file", 0))
                except Exception:  # noqa: BLE001
                    continue
            if cpu_s is None:
                continue
            p = self.prev.get(cid)
            self.prev[cid] = (now, cpu_s)
            if p and now > p[0]:
                name = _name(c)
                sample[name] = [round(max(0.0, (cpu_s - p[1]) / (now - p[0]) * 100), 2), mem]
                proj = (c.get("Labels") or {}).get("com.docker.compose.project")
                if proj:
                    meta[name] = proj
        events = []
        try:
            for ev in engine.events_between(self.last_events, now, {"type": ["container", "image"],
                                                                     "event": list(EVENTS)}):
                act = (ev.get("Action") or ev.get("status") or "").split(":")[0]
                attrs = (ev.get("Actor") or {}).get("Attributes") or {}
                if act == "die" and attrs.get("exitCode") not in (None, "0", "143", "137"):
                    act = "crash"                      # stopped by itself with an error
                events.append([round(ev.get("timeNano", 0) / 1e9 or ev.get("time", now), 1),
                               attrs.get("name") or attrs.get("image") or ev.get("id", "")[:12], act])
        except Exception:  # noqa: BLE001 — history must never break Berth
            pass
        self.last_events = now
        if sample or events:
            write({"t": round(now), "c": sample, "p": meta, "e": events}, keep_days)
        return len(sample)


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def write(rec, keep_days=7):
    try:
        os.makedirs(DIR, exist_ok=True)
        with open(os.path.join(DIR, _day(rec["t"]) + ".jsonl"), "a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        prune(keep_days)
    except OSError:
        pass


def prune(keep_days):
    try:
        files = sorted(f for f in os.listdir(DIR) if f.endswith(".jsonl"))
    except OSError:
        return
    cutoff = _day(time.time() - keep_days * 86400)
    for f in files:
        if f[:10] < cutoff:
            os.remove(os.path.join(DIR, f))
    files = sorted(f for f in os.listdir(DIR) if f.endswith(".jsonl"))
    while len(files) > 1 and sum(os.path.getsize(os.path.join(DIR, f)) for f in files) > CAP:
        os.remove(os.path.join(DIR, files.pop(0)))


def load(t0, t1):
    """({container: [(t, cpu %, mem)]}, {container: project}, [(t, container, action)]) between t0 and t1."""
    series, projects, events = {}, {}, []
    day = t0 - 86400
    while day <= t1 + 86400:
        path = os.path.join(DIR, _day(day) + ".jsonl")
        day += 86400
        try:
            with open(path) as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = r.get("t", 0)
            for e in r.get("e") or []:
                if t0 <= e[0] <= t1:
                    events.append(tuple(e))
            if not (t0 <= t <= t1):
                continue
            for name, (cpu, mem) in (r.get("c") or {}).items():
                series.setdefault(name, []).append((t, cpu, mem))
            projects.update(r.get("p") or {})
    return series, projects, sorted(set(events))


def markers(events):
    """One marker per container per moment: a restart is die + start + restart within seconds — show "restarted"."""
    out = []
    for t, name, act in sorted(events):
        last = out[-1] if out else None
        if last and last[1] == name and t - last[0] <= 20:
            if RANK.get(act, 0) >= RANK.get(last[2], 0):
                out[-1] = (last[0], name, act)
            continue
        out.append((t, name, act))
    text = {**EVENTS, "crash": "crashed"}
    return [(t, f"{name} {text.get(act, act)}", COLOUR.get(act, "#77767b")) for t, name, act in out]


def size():
    try:
        return sum(os.path.getsize(os.path.join(DIR, f)) for f in os.listdir(DIR))
    except OSError:
        return 0
