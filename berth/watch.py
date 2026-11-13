"""Background checks the user turns on in Settings → Alerts: CPU, memory and Docker-disk thresholds, and scheduled
vulnerability scans. app.py calls tick() once a minute; when every alert is off it returns at once, so nothing is read.

A check is a few cgroup file reads per running container on a local Linux engine, or one stats call per container
elsewhere (macOS, Windows, remote engines) — and only while at least one alert is on."""
import os
import re
import shutil
import time

from .stats import _cgroup_dir, _read

CPU_FOR = 5 * 60          # CPU must stay above the limit this long (one-off peaks are normal)
REPEAT = 60 * 60          # the same alert again at most once an hour
DISK_REPEAT = 6 * 60 * 60


def _name(c):
    return ((c.get("Names") or ["?"])[0]).lstrip("/")


class Alerts:
    def __init__(self):
        self.prev = {}        # cid -> (time, cpu seconds)
        self.hot_since = {}   # cid -> first time it was above the CPU limit
        self.sent = {}        # alert key -> time it was last sent

    def _due(self, key, now, every=REPEAT):
        if now - self.sent.get(key, 0) < every:
            return False
        self.sent[key] = now
        return True

    def check(self, engine, containers, info, cpu_limit, mem_limit, disk_limit):
        """Runs on a worker thread. Returns [(key, title, body)] for the notifications to show now."""
        out, now = [], time.time()
        local = engine.path.startswith(("/", "unix://"))
        host_mem = info.get("MemTotal") or 0
        if cpu_limit or mem_limit:
            running = [c for c in containers if c.get("State") == "running"]
            for gone in set(self.prev) - {c["Id"] for c in running}:
                self.prev.pop(gone, None)
                self.hot_since.pop(gone, None)
            for c in running:
                cid, name = c["Id"], _name(c)
                cpu_s, mem, limit = self._read(engine, cid, local)
                if cpu_s is None:
                    continue
                if cpu_limit:
                    p = self.prev.get(cid)
                    self.prev[cid] = (now, cpu_s)
                    if p and now - p[0] > 1:
                        pct = (cpu_s - p[1]) / (now - p[0]) * 100       # like `docker stats`: 100 % = one core
                        if pct >= cpu_limit:
                            since = self.hot_since.setdefault(cid, now)
                            if now - since >= CPU_FOR and self._due(f"cpu-{cid}", now):
                                out.append((f"cpu-{cid}", f"{name} is busy",
                                            f"CPU above {cpu_limit} % for {int((now - since) / 60)} minutes "
                                            f"(now {pct:.0f} %)."))
                        else:
                            self.hot_since.pop(cid, None)
                if mem_limit:
                    cap = limit if limit and (not host_mem or limit < host_mem) else host_mem
                    if cap and mem / cap * 100 >= mem_limit and self._due(f"mem-{cid}", now):
                        what = "its memory limit" if cap == limit and limit != host_mem else "this computer's memory"
                        out.append((f"mem-{cid}", f"{name} is using a lot of memory",
                                    f"{mem / cap * 100:.0f} % of {what} ({mem / 2**20:.0f} MB)."))
        if disk_limit and local:
            free = disk_free(info)
            if free and free[1] < disk_limit and self._due("disk", now, DISK_REPEAT):
                out.append(("disk", "Docker is running out of disk space",
                            f"Only {free[1]:.0f} % free ({free[0] / 2**30:.1f} GB) where Docker keeps its data. "
                            "Disk usage → Clean up frees space."))
        return out

    @staticmethod
    def _read(engine, cid, local):
        """(cpu seconds, memory in use, memory limit or 0)."""
        cg = _cgroup_dir(cid) if local else None
        if cg:
            m = re.search(r"usage_usec (\d+)", _read(cg + "/cpu.stat"))
            cur = int(_read(cg + "/memory.current") or 0)
            inact = re.search(r"^inactive_file (\d+)", _read(cg + "/memory.stat"), re.M)
            mx = _read(cg + "/memory.max").strip()
            return (int(m.group(1)) / 1e6 if m else None, max(0, cur - (int(inact.group(1)) if inact else 0)),
                    int(mx) if mx.isdigit() else 0)
        try:
            s = engine.stats_once(cid)
        except Exception:  # noqa: BLE001
            return None, 0, 0
        ms = s.get("memory_stats") or {}
        mem = ms.get("usage", 0) - (ms.get("stats") or {}).get("inactive_file", 0)
        return s["cpu_stats"]["cpu_usage"]["total_usage"] / 1e9, max(0, mem), ms.get("limit", 0)


def disk_free(info):
    """(free bytes, free %) where Docker keeps its data, or None (remote engine, unknown folder)."""
    root = info.get("DockerRootDir")
    try:
        du = shutil.disk_usage(root) if root and os.path.exists(root) else None
    except (OSError, ValueError):
        du = None
    return (du.free, du.free / du.total * 100) if du and du.total else None


# ── scheduled vulnerability scans ──
SCAN_EVERY = {"daily": 86400, "weekly": 7 * 86400}


def scan_due(period, last):
    return period in SCAN_EVERY and time.time() - (last or 0) >= SCAN_EVERY[period]


def scan_in_use(engine, images, containers):
    """Scan every tagged image a container uses (one after another). Returns [(ref, critical, high)]."""
    from .scan import counts, run_scan
    used = {c.get("ImageID") for c in containers}
    refs = []
    for im in images:
        if im["Id"] in used:
            tags = [t for t in (im.get("RepoTags") or []) if t != "<none>:<none>"]
            if tags:
                refs.append(tags[0])
    res = []
    for ref in refs:
        try:
            c = counts(run_scan(engine, ref, engine.path)[1])
            res.append((ref, c["CRITICAL"], c["HIGH"]))
        except Exception:  # noqa: BLE001
            continue
    return res
