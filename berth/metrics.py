"""App metrics (#30): a container's own Prometheus endpoint (/metrics), scraped every few seconds while its Metrics
tab is on screen — no Prometheus or Grafana needed. Counters are shown as a rate per second. Exporters Berth knows
(node, Postgres, Redis, nginx, Node.js, Python prometheus_client, Go, Java) open with a few useful charts; any
other series can be added from the list."""
import collections
import re
import time

from gi.repository import Adw, GLib, Gtk

from . import charts
from .util import bg, esc, fmt_bytes, label

COMMON_PORTS = (9100, 9187, 9121, 9113, 9090, 9464, 9091, 9104, 9216, 8080, 8000, 3000, 5000, 9000)
PATHS = ("/metrics", "/actuator/prometheus", "/prometheus")
_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([^\s]+)")
STARTERS = [          # (prefix that identifies the exporter, [series])
    ("node_", ["node_load1", "node_memory_MemAvailable_bytes", "node_cpu_seconds_total",
               "node_network_receive_bytes_total"]),
    ("pg_", ["pg_up", "pg_stat_database_numbackends", "pg_stat_database_xact_commit", "pg_stat_database_tup_fetched"]),
    ("redis_", ["redis_connected_clients", "redis_memory_used_bytes", "redis_commands_processed_total",
                "redis_keyspace_hits_total"]),
    ("nginx_", ["nginx_connections_active", "nginx_http_requests_total", "nginx_connections_accepted"]),
    ("nodejs_", ["nodejs_eventloop_lag_seconds", "nodejs_heap_size_used_bytes", "process_cpu_seconds_total",
                 "http_requests_total"]),
    ("python_", ["process_resident_memory_bytes", "process_cpu_seconds_total", "python_gc_objects_collected_total",
                 "http_requests_total"]),
    ("go_", ["go_goroutines", "go_memstats_alloc_bytes", "process_cpu_seconds_total", "process_resident_memory_bytes"]),
    ("jvm_", ["jvm_memory_used_bytes", "jvm_threads_live_threads", "process_cpu_usage", "http_server_requests_seconds_count"]),
    ("process_", ["process_resident_memory_bytes", "process_cpu_seconds_total", "process_open_fds"]),
]


def parse(text):
    """{metric: {"type", "help", "value": sum over its series, "n": number of series}}. Histogram / summary parts
    (_bucket, _sum, _count) are kept as their own names."""
    types, helps, out = {}, {}, {}
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            p = line.split(None, 3)
            if len(p) == 4:
                types[p[2]] = p[3].strip()
            continue
        if line.startswith("# HELP "):
            p = line.split(None, 3)
            if len(p) >= 3:
                helps[p[2]] = p[3] if len(p) == 4 else ""
            continue
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m or m.group(1).endswith("_bucket"):
            continue
        try:
            v = float(m.group(3))
        except ValueError:
            continue
        if v != v:                            # NaN
            continue
        d = out.setdefault(m.group(1), {"value": 0.0, "n": 0})
        d["value"] += v
        d["n"] += 1
    for name, d in out.items():
        base = re.sub(r"_(total|sum|count|created)$", "", name)
        d["type"] = types.get(name) or types.get(base) or ("counter" if name.endswith("_total") else "gauge")
        d["help"] = helps.get(name) or helps.get(base) or ""
    return out


def _backend(engine, ins):
    from .db import Backend
    return Backend(engine, ins)


def candidates(ins):
    lab = (ins.get("Config") or {}).get("Labels") or {}
    out = []
    if lab.get("prometheus.io/port"):
        out.append((int(lab["prometheus.io/port"]), lab.get("prometheus.io/path") or "/metrics"))
    exposed = [int(p.split("/")[0]) for p in ((ins.get("Config") or {}).get("ExposedPorts") or {}) if p.endswith("/tcp")]
    for port in exposed + [p for p in COMMON_PORTS if p not in exposed]:
        for path in PATHS:
            if (port, path) not in out:
                out.append((port, path))
    return out


def listening(engine, cid):
    """TCP ports something listens on inside the container — `netstat -ltn` from a throwaway busybox in its network
    namespace (works for images without a shell)."""
    if not any("busybox:latest" in (i.get("RepoTags") or []) for i in engine.images()):
        st = engine.pull("busybox:latest")
        while st.read(2.0) != b"":
            pass
        st.close()
    hid = engine.create_container(None, {"Image": "busybox:latest", "Cmd": ["netstat", "-ltn"],
                                         "Labels": {"io.github.subhadeeproy3902.berth.helper": "ports"},
                                         "HostConfig": {"NetworkMode": f"container:{cid}"}})["Id"]
    try:
        engine.action(hid, "start")
        engine.wait(hid, timeout=20)
        out, _e = engine.logs_text(hid)
    finally:
        try:
            engine.remove_container(hid)
        except Exception:  # noqa: BLE001
            pass
    ports = set()
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 4 and f[0].startswith("tcp") and ":" in f[3]:
            p = f[3].rsplit(":", 1)[1]
            if p.isdigit():
                ports.add(int(p))
    return ports


def detect(engine, cid):
    """An HTTP client and path for the container's metrics endpoint, or None. Only ports something listens on are
    tried: the prometheus.io labels first, then exposed ports, then the usual exporter ports."""
    from .db import Http
    ins = engine.inspect(cid)
    b = _backend(engine, ins)
    try:
        open_ports = listening(engine, cid)
    except Exception:  # noqa: BLE001
        open_ports = None                       # couldn't tell: try the likely ones anyway
    for port, path in candidates(ins):
        if open_ports is not None and port not in open_ports:
            continue
        h = Http(b, port, scheme="http")
        try:
            status, body = h.request("GET", path, timeout=3)
        except Exception:  # noqa: BLE001
            continue
        if status == 200 and ("# TYPE" in body or "# HELP" in body):
            return h, port, path
    return None


def connect(engine, cid, port, path):
    from .db import Http
    return Http(_backend(engine, engine.inspect(cid)), port, scheme="http"), port, path


def scrape(http, path):
    status, body = http.request("GET", path, timeout=5)
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    return parse(body)


def _fmt_for(name):
    if name.endswith("_bytes") or "_bytes_" in name:
        return lambda v: fmt_bytes(v)
    if name.endswith("_seconds") or "_seconds_" in name:
        return lambda v: f"{v * 1000:.1f} ms" if v < 1 else f"{v:.2f} s"
    return lambda v: f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else f"{v:.3g}"


class MetricsView(Gtk.Box):
    """The Metrics tab of a container."""
    EVERY = 5

    def __init__(self, win, store, cid):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.store, self.cid = win, store, cid
        self.endpoint, self.timer, self.last = None, 0, {}
        self.hist = collections.defaultdict(lambda: collections.deque(maxlen=360))
        self.prev = {}
        self.chosen = []
        self.charts = {}
        self.stack = Gtk.Stack(vexpand=True)
        self.status = Adw.StatusPage(icon_name="utilities-system-monitor-symbolic", title="Looking for metrics…",
                                     description="Checking the ports this container exposes for a Prometheus "
                                                 "endpoint (/metrics).")
        self.stack.add_named(self.status, "status")
        # manual endpoint
        box = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.port_e = Gtk.Entry(placeholder_text="port", width_chars=6)
        self.path_e = Gtk.Entry(text="/metrics", width_chars=18)
        go = Gtk.Button(label="Use this endpoint")
        go.connect("clicked", lambda *_: self._manual())
        for w in (self.port_e, self.path_e, go):
            box.append(w)
        self.manual = box
        self.manual.set_visible(False)
        self.status.set_child(self.manual)
        # charts
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["page-body"])
        bar = Gtk.Box(spacing=8)
        self.where = label("", css=["dim-label", "caption"], hexpand=True)
        bar.append(self.where)
        self.names = Gtk.StringList()
        self.pick = Gtk.DropDown(model=self.names, enable_search=True, tooltip_text="Add a series")
        bar.append(self.pick)
        add = Gtk.Button(label="Add chart")
        add.connect("clicked", lambda *_: self._add_pick())
        bar.append(add)
        page.append(bar)
        self.grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=2, min_children_per_line=1,
                                column_spacing=12, row_spacing=12, homogeneous=True)
        page.append(self.grid)
        self.stack.add_named(Gtk.ScrolledWindow(child=page, vexpand=True), "charts")
        self.append(self.stack)
        self.connect("map", lambda *_: self._start())
        self.connect("unmap", lambda *_: self._stop())
        bg(detect, store.engine, cid, done=self._found,
           error=lambda e: self._none(str(e)))

    def _none(self, why=""):
        self.status.set_title("No metrics endpoint found")
        self.status.set_description("Nothing on this container's ports answered like Prometheus (/metrics). If the "
                                    "app has one, type its port and path." + (f"\n{esc(why)}" if why else ""))
        self.manual.set_visible(True)

    def _manual(self):
        port, path = self.port_e.get_text().strip(), self.path_e.get_text().strip() or "/metrics"
        if port.isdigit():
            bg(connect, self.store.engine, self.cid, int(port), path if path.startswith("/") else "/" + path,
               done=self._found, error=lambda e: self._none(str(e)))

    def _found(self, ep):
        if not ep:
            return self._none()
        self.http, port, path = ep
        self.endpoint = (port, path)
        self.where.set_text(f"Scraped from port {port}{path} every {self.EVERY} s while this tab is open · "
                            "counters as a rate per second")
        self.stack.set_visible_child_name("charts")
        self._start(first=True)

    def _start(self, first=False):
        if self.endpoint and not self.timer and self.get_mapped():
            self._tick(first)
            self.timer = GLib.timeout_add_seconds(self.EVERY, self._tick)

    def _stop(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0

    def _tick(self, first=False):
        if not self.get_mapped():
            self._stop()
            return False

        def done(m):
            now = time.time()
            for name, d in m.items():
                v = d["value"]
                if d["type"] == "counter":
                    p = self.prev.get(name)
                    self.prev[name] = (now, v)
                    if not p or now <= p[0] or v < p[1]:
                        continue
                    v = (v - p[1]) / (now - p[0])
                self.hist[name].append((now, v))
            self.last = m
            if first or self.names.get_n_items() != len(m):
                self.names.splice(0, self.names.get_n_items(), sorted(m))
            if not self.chosen:
                self._starters(m)
            self._redraw()

        def fail(e):
            self.where.set_text(f"Couldn't scrape port {self.endpoint[0]}{self.endpoint[1]}: {e}")
        bg(scrape, self.http, self.endpoint[1], done=done, error=fail)
        return True

    def _starters(self, m):
        picks = []
        for prefix, series in STARTERS:
            if any(n.startswith(prefix) for n in m):
                picks += [s for s in series if s in m and s not in picks]
        if not picks:      # unknown app: its first few own series
            picks = [n for n in sorted(m) if not n.startswith(("go_", "process_", "python_", "promhttp_"))][:4]
        for n in picks[:6]:
            self._add(n)

    def _add_pick(self):
        it = self.pick.get_selected_item()
        if it:
            self._add(it.get_string())

    def _add(self, name):
        if name in self.charts:
            return
        self.chosen.append(name)
        d = self.last.get(name, {})
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["card", "chart-card"])
        head = Gtk.Box(spacing=8)
        head.append(label(name + (" /s" if d.get("type") == "counter" else ""), css=["heading", "monospace"],
                          hexpand=True))
        val = label("", css=["numeric", "dim-label"], xalign=1.0)
        head.append(val)
        x = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Remove this chart", valign=Gtk.Align.CENTER)
        x.add_css_class("flat")
        x.connect("clicked", lambda *_: self._remove(name))
        head.append(x)
        card.append(head)
        if d.get("help"):
            card.append(label(d["help"][:140], css=["caption", "dim-label"], wrap=True))
        ch = charts.LineChart(_fmt_for(name), 130, fill=True)
        card.append(ch)
        self.charts[name] = (card, ch, val)
        self.grid.append(card)
        self._redraw()

    def _remove(self, name):
        card, _ch, _v = self.charts.pop(name)
        self.chosen.remove(name)
        self.grid.remove(card.get_parent())

    def _redraw(self):
        pal = charts.palette()
        for i, (name, (_card, ch, val)) in enumerate(self.charts.items()):
            pts = list(self.hist.get(name, []))
            ch.set_data([(name, pal[i % len(pal)], pts)], window=600)
            val.set_text(_fmt_for(name)(pts[-1][1]) if pts else "…")
