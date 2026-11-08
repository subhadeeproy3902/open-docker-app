"""Live container metrics from cgroup v2 files — a few small file reads per sample,
no Docker API polling. Falls back to the Engine's one-shot stats if the cgroup
layout is unknown. The thread sleeps whenever nothing on screen needs numbers."""
import collections
import os
import re
import threading
import time

from gi.repository import GLib

HIST = 900                # samples kept (30 min at 2 s)


def _read(p):
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return ""


def _cgroup_dir(cid):
    for p in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope",
              f"/sys/fs/cgroup/docker/{cid}",
              f"/sys/fs/cgroup/docker.slice/docker-{cid}.scope"):
        if os.path.exists(p + "/cpu.stat"):
            return p
    return None


class Sampler:
    def __init__(self, engine, on_sample):
        self.engine, self.on_sample = engine, on_sample
        self.interval = 2.0
        self.want = threading.Event()        # set = someone is looking at live numbers
        self.lock = threading.Lock()
        self.running = {}                    # id -> name, set by the store
        self._meta = {}                      # id -> (cgroup dir | None, pid)
        self._prev = {}
        self.series = {}                     # id -> deque[(t, cpu%, mem, rx/s, tx/s, rd/s, wr/s)]
        self.host = collections.deque(maxlen=HIST)   # (t, host cpu%, used, total, docker cpu% of machine, docker mem)
        self._hprev = None
        self.ncpu = os.cpu_count() or 1
        threading.Thread(target=self._loop, daemon=True).start()

    def set_running(self, mapping):
        with self.lock:
            self.running = dict(mapping)
            for gone in set(self.series) - set(mapping):
                self.series.pop(gone, None)
                self._prev.pop(gone, None)
                self._meta.pop(gone, None)

    def latest(self, cid):
        d = self.series.get(cid)
        return d[-1] if d else None

    def points(self, cid):
        with self.lock:
            return list(self.series.get(cid) or ())

    def host_points(self):
        with self.lock:
            return list(self.host)

    def all_points(self):
        with self.lock:
            return {k: list(v) for k, v in self.series.items()}

    # ── internals ──
    def _meta_for(self, cid):
        m = self._meta.get(cid)
        if m is None:
            pid = 0
            try:
                pid = (self.engine.inspect(cid).get("State") or {}).get("Pid", 0)
            except Exception:  # noqa: BLE001
                pass
            m = self._meta[cid] = (_cgroup_dir(cid), pid)
        return m

    @staticmethod
    def _counters(cg, pid):
        cpu = re.search(r"usage_usec (\d+)", _read(cg + "/cpu.stat"))
        if not cpu:
            return None
        cur = int(_read(cg + "/memory.current") or 0)
        inact = re.search(r"^inactive_file (\d+)", _read(cg + "/memory.stat"), re.M)
        mem = max(0, cur - (int(inact.group(1)) if inact else 0))
        rd = wr = 0
        for line in _read(cg + "/io.stat").splitlines():
            for kv in line.split()[1:]:
                k, _, v = kv.partition("=")
                if k == "rbytes":
                    rd += int(v)
                elif k == "wbytes":
                    wr += int(v)
        rx = tx = 0
        if pid:
            for line in _read(f"/proc/{pid}/net/dev").splitlines()[2:]:
                name, _, rest = line.partition(":")
                f = rest.split()
                if name.strip() != "lo" and len(f) >= 9:
                    rx += int(f[0])
                    tx += int(f[8])
        return int(cpu.group(1)) / 1e6, mem, rx, tx, rd, wr

    def _api_counters(self, cid):
        s = self.engine.stats_once(cid)
        cpu = s["cpu_stats"]["cpu_usage"]["total_usage"] / 1e9
        mem = s.get("memory_stats", {}).get("usage", 0) - s.get("memory_stats", {}).get("stats", {}).get("inactive_file", 0)
        rx = sum(n.get("rx_bytes", 0) for n in (s.get("networks") or {}).values())
        tx = sum(n.get("tx_bytes", 0) for n in (s.get("networks") or {}).values())
        rd = wr = 0
        for e in (s.get("blkio_stats", {}).get("io_service_bytes_recursive") or []):
            if e.get("op", "").lower() == "read":
                rd += e.get("value", 0)
            elif e.get("op", "").lower() == "write":
                wr += e.get("value", 0)
        return cpu, max(0, mem), rx, tx, rd, wr

    @staticmethod
    def _host():
        """(total jiffies, idle jiffies, used bytes, total bytes) — None where the platform has no /proc."""
        first = _read("/proc/stat").split("\n", 1)[0].split()[1:]
        if not first:
            return None
        v = list(map(int, first))
        mi = dict(re.findall(r"^(\w+):\s+(\d+)", _read("/proc/meminfo"), re.M))
        total = int(mi.get("MemTotal", 0)) * 1024
        return sum(v), v[3] + v[4], total - int(mi.get("MemAvailable", 0)) * 1024, total

    def _loop(self):
        while True:
            self.want.wait()
            t0 = time.time()
            with self.lock:
                running = dict(self.running)
            dcpu = dmem = 0.0
            for cid in running:
                cg, pid = self._meta_for(cid)
                try:
                    c = self._counters(cg, pid) if cg else self._api_counters(cid)
                except Exception:  # noqa: BLE001
                    c = None
                if c is None:
                    continue
                now = time.time()
                p = self._prev.get(cid)
                self._prev[cid] = (now, *c)
                if not p or now - p[0] > self.interval * 4:   # stale after a pause: restart the delta
                    continue
                dt = max(1e-3, now - p[0])
                cpu = max(0.0, (c[0] - p[1]) / dt * 100)
                rates = [max(0.0, (c[i] - p[i + 1]) / dt) for i in (2, 3, 4, 5)]
                dcpu += cpu
                dmem += c[1]
                with self.lock:
                    self.series.setdefault(cid, collections.deque(maxlen=HIST)).append((now, cpu, c[1], *rates))
            hostinfo = self._host()
            if hostinfo is None:                  # macOS / Windows: only the containers' own numbers
                with self.lock:
                    self.host.append((time.time(), None, None, None, dcpu / self.ncpu, dmem))
            else:
                tot, idle, used, total = hostinfo
                if self._hprev and t0 - self._hprev[2] < self.interval * 4:
                    dtot = max(1, tot - self._hprev[0])
                    hcpu = 100 * (1 - (idle - self._hprev[1]) / dtot)
                    with self.lock:
                        self.host.append((time.time(), hcpu, used, total, dcpu / self.ncpu, dmem))
                self._hprev = (tot, idle, t0)
            GLib.idle_add(self.on_sample)
            time.sleep(max(0.2, self.interval - (time.time() - t0)))
