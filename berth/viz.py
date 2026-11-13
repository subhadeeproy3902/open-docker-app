"""Two drawings, with cairo like the charts (no web view):

- the Compose dependency graph of a stack (#14): a node per service (state colour, health dot), arrows for
  depends_on (with its condition), a frame per network, volume nodes linked to the services that mount them;
- the network map (#33): one frame per network with its containers, published ports on the edge, live traffic,
  and a reachability check ("can api reach db:5432?") run from inside a container's network.

Click a container to open it."""
import json
import math
import subprocess

from gi.repository import Adw, Gtk, Pango, PangoCairo

from . import charts, compose
from .util import bg, cname, fmt_rate, host, labels
from .util import set_busy  # noqa: E402

NODE_W, NODE_H, GAP_X, GAP_Y, PAD = 170, 54, 90, 26, 40


def _fg(widget):
    c = widget.get_color()
    return c.red, c.green, c.blue


def _rrect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _text(cr, widget, x, y, text, size=10, bold=False, width=None, alpha=1.0, align=0):
    lay = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription.from_string(f"Sans {'Bold ' if bold else ''}{size}")
    lay.set_font_description(fd)
    lay.set_text(text, -1)
    if width:
        lay.set_width(int(width * Pango.SCALE))
        lay.set_ellipsize(Pango.EllipsizeMode.END)
        lay.set_alignment(Pango.Alignment.CENTER if align else Pango.Alignment.LEFT)
    r, g, b = _fg(widget)
    cr.set_source_rgba(r, g, b, alpha)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, lay)


def _state_colour(c):
    if not c:
        return "#77767b"
    st = c.get("State")
    if st == "running":
        return "#e5a50a" if "(unhealthy)" in c.get("Status", "") else "#2ec27e"
    return "#e01b24" if st in ("dead",) or (st == "exited" and "(0)" not in c.get("Status", "")) else "#77767b"


class Canvas(Gtk.DrawingArea):
    """Scrollable drawing with clickable boxes: self.hits = [(x, y, w, h, callback)]."""

    def __init__(self, draw):
        super().__init__()
        self.hits, self._draw_fn = [], draw
        self.set_draw_func(lambda _a, cr, w, h: self._draw(cr, w, h))
        click = Gtk.GestureClick()
        click.connect("released", self._click)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        self.add_controller(motion)

    def _draw(self, cr, w, h):
        self.hits = []
        self._draw_fn(self, cr, w, h)

    def _hit(self, x, y):
        return next((fn for (hx, hy, hw, hh, fn) in self.hits if hx <= x <= hx + hw and hy <= y <= hy + hh), None)

    def _click(self, _g, _n, x, y):
        fn = self._hit(x, y)
        if fn:
            fn()

    def _motion(self, _c, x, y):
        self.set_cursor_from_name("pointer" if self._hit(x, y) else None)


# ── Compose dependency graph (#14) ──
def stack_model(containers, project):
    """Services, edges, networks and volumes of a stack — from the compose file when Berth knows it, else from the
    containers' labels and settings."""
    wd, files = compose.project_info(containers, project)
    by_svc = {labels(c).get("com.docker.compose.service"): c for c in containers
              if labels(c).get("com.docker.compose.project") == project}
    services, edges, vols = {}, [], {}
    cfg = None
    if files:
        try:
            r = subprocess.run(host(compose.compose_argv(project, wd, files, "config", "--format", "json")),
                               capture_output=True, text=True, timeout=60, cwd=wd or None)
            cfg = json.loads(r.stdout) if r.returncode == 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            cfg = None
    if cfg:
        for svc, d in (cfg.get("services") or {}).items():
            services[svc] = {"networks": sorted((d.get("networks") or {"default": None}).keys()),
                             "container": by_svc.get(svc)}
            for dep, cond in (d.get("depends_on") or {}).items():
                edges.append((svc, dep, (cond or {}).get("condition", "service_started")))
            for v in d.get("volumes") or []:
                if v.get("type") == "volume" and v.get("source"):
                    vols.setdefault(v["source"], []).append(svc)
    else:
        for svc, c in by_svc.items():
            nets = sorted(((c.get("NetworkSettings") or {}).get("Networks") or {}).keys())
            services[svc] = {"networks": [n.removeprefix(project + "_") for n in nets], "container": c}
            for dep in (labels(c).get("com.docker.compose.depends_on") or "").split(","):
                if dep:
                    name, _, cond = dep.partition(":")
                    edges.append((svc, name, cond.partition(":")[0] or "service_started"))
            for m in c.get("Mounts") or []:
                if m.get("Type") == "volume" and m.get("Name"):
                    vols.setdefault(m["Name"].removeprefix(project + "_"), []).append(svc)
    return {"services": services, "edges": [e for e in edges if e[1] in services], "volumes": vols,
            "from_file": bool(cfg)}


GH, VH, TOP = 68, 40, 70          # graph: service node height, volume node height, room for the legend


def layout(model):
    """Layered: a service sits one column right of everything it depends on; a volume sits under the column of the
    service that mounts it (first one), so its link stays short."""
    deps = {s: [b for a, b, _c in model["edges"] if a == s] for s in model["services"]}
    level = {}

    def lv(s, seen=()):
        if s in level:
            return level[s]
        if s in seen:
            return 0
        level[s] = 1 + max((lv(d, seen + (s,)) for d in deps[s]), default=-1)
        return level[s]
    for s in model["services"]:
        lv(s)
    cols = {}
    for s in sorted(model["services"], key=lambda s: (level[s], s)):
        cols.setdefault(level[s], []).append(s)
    pos = {}
    for col, names in cols.items():
        for i, s in enumerate(names):
            pos[s] = (PAD + col * (NODE_W + GAP_X), TOP + i * (GH + GAP_Y))
    below = {c: TOP + len(n) * (GH + GAP_Y) + 10 for c, n in cols.items()}
    for v in sorted(model["volumes"]):
        users = model["volumes"][v]
        col = level[users[0]] if users and users[0] in level else 0
        pos["vol:" + v] = (PAD + col * (NODE_W + GAP_X), below.setdefault(col, TOP))
        below[col] += VH + 16
    w = max((x for x, _y in pos.values()), default=0) + NODE_W + PAD
    h = max(y + (VH if k.startswith("vol:") else GH) for k, (_x, y) in pos.items()) + PAD if pos else 200
    return pos, w, h


def _arrow(cr, x1, y1, x2, y2):
    cr.move_to(x1, y1)
    mx = (x1 + x2) / 2
    cr.curve_to(mx, y1, mx, y2, x2, y2)
    cr.stroke()
    a = math.atan2(0, 1) if x2 >= x1 else math.pi
    cr.move_to(x2, y2)
    cr.line_to(x2 - 8 * math.cos(a - 0.45), y2 - 8 * math.sin(a - 0.45))
    cr.line_to(x2 - 8 * math.cos(a + 0.45), y2 - 8 * math.sin(a + 0.45))
    cr.close_path()
    cr.fill()


class GraphDialog(Adw.Dialog):
    def __init__(self, win, store, project):
        super().__init__(title=f"Graph · {project}", content_width=980, content_height=640)
        self.win, self.store, self.project, self.model = win, store, project, None
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.note = Gtk.Label(xalign=0, wrap=True, css_classes=["caption", "dim-label"], margin_start=18,
                              margin_end=18, margin_bottom=6,
                              label="Arrows point to what a service waits for (depends_on); the tags under a service "
                                    "are its networks; round boxes are volumes. Click a service to open it.")
        self.canvas = Canvas(self._draw)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.note)
        box.append(Gtk.ScrolledWindow(child=self.canvas, vexpand=True))
        tv.set_content(box)
        self.set_child(tv)
        self._h = store.connect("containers", lambda *_: self.canvas.queue_draw())
        self.connect("closed", lambda *_: store.disconnect(self._h))
        sm = Adw.StyleManager.get_default()
        self._t = sm.connect("notify::dark", lambda *_: self.canvas.queue_draw())
        self.connect("closed", lambda *_: sm.disconnect(self._t))
        bg(stack_model, list(store.containers), project, done=self._loaded,
           error=lambda e: self.note.set_text(f"Couldn't read the stack: {e}"))

    def _loaded(self, model):
        self.model = model
        self.pos, w, h = layout(model)
        self.canvas.set_content_width(int(w))
        self.canvas.set_content_height(int(h))
        self.set_content_width(min(1280, max(640, int(w) + 40)))
        self.set_content_height(min(800, max(360, int(h) + 120)))
        if not model["from_file"]:
            self.note.set_text(self.note.get_text() + " (The compose file wasn't found — read from the containers.)")
        self.canvas.queue_draw()

    def _draw(self, area, cr, w, h):
        m = self.model
        if not m:
            return
        pal = charts.palette()
        fg = _fg(area)
        # legend: one colour per network; each service shows the networks it is on
        nets = sorted({n for d in m["services"].values() for n in d["networks"]})
        ncol = {n: pal[(i + 2) % len(pal)] for i, n in enumerate(nets)}
        x = PAD
        _text(cr, area, x, PAD - 14, "Networks:", size=8, alpha=0.6)
        x += 62
        for n in nets:
            cr.set_source_rgba(*charts.rgb(ncol[n], 0.85))
            _rrect(cr, x, PAD - 13, 10, 10, 3)
            cr.fill()
            _text(cr, area, x + 14, PAD - 15, n, size=8, alpha=0.8)
            x += 30 + 7 * len(n)
        # depends_on arrows
        cr.set_line_width(1.6)
        for a, b, cond in m["edges"]:
            ax, ay = self.pos[a]
            bx, by = self.pos[b]
            cr.set_source_rgba(*fg, 0.6)
            _arrow(cr, ax, ay + GH / 2, bx + NODE_W, by + GH / 2)
            label = {"service_healthy": "healthy", "service_completed_successfully": "completed"}.get(cond, "")
            if label:
                _text(cr, area, (ax + bx + NODE_W) / 2 - 22, (ay + by) / 2 + GH / 2 - 16, label, size=8, alpha=0.7)
        # volume links: from the bottom of each user to the volume
        for v, users in m["volumes"].items():
            vx, vy = self.pos["vol:" + v]
            for u in users:
                sx, sy = self.pos[u]
                cr.set_source_rgba(*fg, 0.3)
                cr.set_dash([3, 3])
                cr.set_line_width(1.2)
                cr.move_to(sx, sy + GH / 2)                 # along the left edge: never through other boxes
                cr.curve_to(sx - 22, sy + GH / 2, vx - 22, vy + VH / 2, vx, vy + VH / 2)
                cr.stroke()
                cr.set_dash([])
        # service nodes
        for s, d in m["services"].items():
            x, y = self.pos[s]
            c = d["container"]
            fresh = self.store.by_id(c["Id"]) if c else None
            c = fresh or c
            _rrect(cr, x, y, NODE_W, GH, 10)
            cr.set_source_rgba(*fg, 0.06)
            cr.fill_preserve()
            cr.set_source_rgba(*charts.rgb(_state_colour(c), 0.9))
            cr.set_line_width(2)
            cr.stroke()
            cr.arc(x + 14, y + 17, 4.5, 0, 2 * math.pi)
            cr.set_source_rgba(*charts.rgb(_state_colour(c)))
            cr.fill()
            _text(cr, area, x + 24, y + 8, s, size=10, bold=True, width=NODE_W - 32)
            sub = (c.get("Image", "") if c else "not created") + ("" if not c or c.get("State") == "running"
                                                                 else f" · {c.get('State')}")
            _text(cr, area, x + 12, y + 28, sub, size=8, width=NODE_W - 20, alpha=0.65)
            nx = x + 12
            for n in d["networks"]:
                tw = 12 + 6 * len(n)
                cr.set_source_rgba(*charts.rgb(ncol.get(n, pal[0]), 0.22))
                _rrect(cr, nx, y + 46, tw, 14, 7)
                cr.fill()
                _text(cr, area, nx + 6, y + 46, n, size=7, alpha=0.9)
                nx += tw + 4
            if c:
                area.hits.append((x, y, NODE_W, GH, lambda cid=c["Id"]: (self.close(),
                                                                             self.win.show_container(cid))))
        # volume nodes
        for v, users in m["volumes"].items():
            x, y = self.pos["vol:" + v]
            _rrect(cr, x, y, NODE_W, VH, VH / 2)
            cr.set_source_rgba(*charts.rgb(pal[1], 0.12))
            cr.fill_preserve()
            cr.set_source_rgba(*charts.rgb(pal[1], 0.6))
            cr.set_line_width(1.2)
            cr.stroke()
            _text(cr, area, x + 10, y + 4, f"volume {v}", size=9, bold=True, width=NODE_W - 20, align=1)
            _text(cr, area, x + 10, y + 21, f"shared by {len(users)} services" if len(users) > 1 else
                  f"used by {users[0]}", size=8, width=NODE_W - 20, alpha=0.65, align=1)


# ── network map + reachability (#33) ──
CHECK = r'''h="$1"; p="$2"
ip=$(nslookup "$h" 2>/dev/null | awk '/^Name:/ {n=1} n && /^Address/ {print $NF; exit}')
[ -z "$ip" ] && ip=$(getent hosts "$h" 2>/dev/null | awk '{print $1; exit}')
echo "ip=$ip"
if [ -n "$p" ]; then nc -z -w 3 "$h" "$p" 2>/dev/null && echo port=open || echo port=closed; fi'''


def reach(engine, cid, target, port):
    """Resolve `target` and try a TCP connection from inside container `cid`'s network (a throwaway busybox in
    its network namespace, so it works for images without a shell). Returns (ip or "", open True/False/None)."""
    if not any("busybox:latest" in (i.get("RepoTags") or []) for i in engine.images()):
        st = engine.pull("busybox:latest")
        while st.read(2.0) != b"":
            pass
        st.close()
    body = {"Image": "busybox:latest", "Cmd": ["sh", "-c", CHECK, "check", target, str(port or "")],
            "Labels": {"io.github.subhadeeproy3902.berth.helper": "reach"},
            "HostConfig": {"NetworkMode": f"container:{cid}"}}
    hid = engine.create_container(None, body)["Id"]
    try:
        engine.action(hid, "start")
        engine.wait(hid, timeout=30)
        out, _err = engine.logs_text(hid)
    finally:
        try:
            engine.remove_container(hid)
        except Exception:  # noqa: BLE001
            pass
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return kv.get("ip", ""), (None if not port else kv.get("port") == "open")


class NetworkMap(Adw.Dialog):
    def __init__(self, win, store):
        super().__init__(title="Network map", content_width=1080, content_height=720)
        self.win, self.store = win, store
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        # reachability check
        bar = Gtk.Box(spacing=8, css_classes=["card-bar"])
        bar.append(Gtk.Label(label="Can", css_classes=["dim-label"]))
        self.src_names = Gtk.StringList()
        self.src = Gtk.DropDown(model=self.src_names, enable_search=True, tooltip_text="Check from this container")
        bar.append(self.src)
        bar.append(Gtk.Label(label="reach", css_classes=["dim-label"]))
        self.dst = Gtk.Entry(placeholder_text="db, api, example.com", width_chars=18)
        bar.append(self.dst)
        bar.append(Gtk.Label(label="port", css_classes=["dim-label"]))
        self.port = Gtk.Entry(placeholder_text="5432", width_chars=6, input_purpose=Gtk.InputPurpose.DIGITS)
        bar.append(self.port)
        self.go = Gtk.Button(label="Check")
        self.go.add_css_class("suggested-action")
        self.go.connect("clicked", lambda *_: self._check())
        self.dst.connect("activate", lambda *_: self._check())
        self.port.connect("activate", lambda *_: self._check())
        bar.append(self.go)
        self.result = Gtk.Label(xalign=0, hexpand=True, wrap=True, css_classes=["caption"])
        bar.append(self.result)
        box.append(bar)
        box.append(Gtk.Label(label="Networks with their containers. Ports on the right edge are published on this "
                                   "computer. Live traffic shows while Berth is sampling. Click a container to open it.",
                             xalign=0, wrap=True, css_classes=["caption", "dim-label"], margin_start=18, margin_end=18))
        self.canvas = Canvas(self._draw)
        box.append(Gtk.ScrolledWindow(child=self.canvas, vexpand=True))
        tv.set_content(box)
        self.set_child(tv)
        self._fill_sources()
        self._h = store.connect("containers", lambda *_: (self._fill_sources(), self.canvas.queue_draw()))
        self._s = store.connect("stats", lambda *_: self.canvas.queue_draw())
        self.connect("closed", lambda *_: (store.disconnect(self._h), store.disconnect(self._s)))
        if store.networks is None:
            store.refresh("networks")
        self._layout()

    def _running(self):
        return sorted((c for c in self.store.containers if c.get("State") == "running"), key=cname)

    def _fill_sources(self):
        cur = self.src.get_selected_item().get_string() if self.src.get_selected_item() else None
        names = [cname(c) for c in self._running()]
        self.src_names.splice(0, self.src_names.get_n_items(), names)
        if cur in names:
            self.src.set_selected(names.index(cur))

    def _groups(self):
        groups = {}
        for c in self.store.containers:
            for net in ((c.get("NetworkSettings") or {}).get("Networks") or {}):
                groups.setdefault(net, []).append(c)
        return dict(sorted(groups.items(), key=lambda kv: (kv[0] in ("bridge", "host", "none"), kv[0])))

    def _layout(self):
        groups = self._groups()
        per_row = 4
        h = PAD
        for _net, cs in groups.items():
            rows = max(1, math.ceil(len(cs) / per_row))
            h += 34 + rows * (NODE_H + 16) + 24
        self.canvas.set_content_width(PAD * 2 + per_row * (NODE_W + 50) + 40)
        self.canvas.set_content_height(int(h + PAD))

    def _draw(self, area, cr, w, h):
        pal = charts.palette()
        fg = _fg(area)
        groups = self._groups()
        per_row, y = 4, PAD
        fw = per_row * (NODE_W + 50) + 20
        for i, (net, cs) in enumerate(groups.items()):
            rows = max(1, math.ceil(len(cs) / per_row))
            fh = 34 + rows * (NODE_H + 16) + 6
            col = pal[(i + 2) % len(pal)]
            _rrect(cr, PAD, y, fw, fh, 12)
            cr.set_source_rgba(*charts.rgb(col, 0.07))
            cr.fill_preserve()
            cr.set_source_rgba(*charts.rgb(col, 0.5))
            cr.set_line_width(1.2)
            cr.stroke()
            _text(cr, area, PAD + 14, y + 9, net, size=10, bold=True)
            _text(cr, area, PAD + 14 + 8 * len(net) + 24, y + 11, f"{len(cs)} container{'s' if len(cs) != 1 else ''}",
                  size=8, alpha=0.6)
            for j, c in enumerate(sorted(cs, key=cname)):
                x = PAD + 14 + (j % per_row) * (NODE_W + 50)
                ny = y + 34 + (j // per_row) * (NODE_H + 16)
                _rrect(cr, x, ny, NODE_W, NODE_H, 10)
                cr.set_source_rgba(*fg, 0.06)
                cr.fill_preserve()
                cr.set_source_rgba(*charts.rgb(_state_colour(c), 0.85))
                cr.set_line_width(1.8)
                cr.stroke()
                ip = (((c.get("NetworkSettings") or {}).get("Networks") or {}).get(net) or {}).get("IPAddress", "")
                _text(cr, area, x + 12, ny + 7, cname(c), size=9, bold=True, width=NODE_W - 20)
                s = self.store.sampler.latest(c["Id"]) if hasattr(self.store, "sampler") else None
                traffic = f" · ↓{fmt_rate(s[3])} ↑{fmt_rate(s[4])}" if s and len(s) > 4 else ""
                _text(cr, area, x + 12, ny + 27, (ip or c.get("State", "")) + traffic, size=8, width=NODE_W - 20,
                      alpha=0.65)
                pubs = sorted({p.get("PublicPort") for p in c.get("Ports") or [] if p.get("PublicPort")})
                for k, port in enumerate(pubs[:2]):
                    px, py = x + NODE_W - 4, ny + 8 + k * 20
                    _rrect(cr, px, py, 46, 16, 8)
                    cr.set_source_rgba(*charts.rgb(pal[0], 0.85))
                    cr.fill()
                    _text(cr, area, px + 4, py + 1, f":{port}", size=8, bold=True, width=38, align=1)
                area.hits.append((x, ny, NODE_W, NODE_H, lambda cid=c["Id"]: (self.close(),
                                                                              self.win.show_container(cid))))
            y += fh + 24

    def _check(self):
        it = self.src.get_selected_item()
        target = self.dst.get_text().strip()
        port = self.port.get_text().strip()
        if not it or not target:
            self.result.set_text("Pick a container and type a name or address")
            return
        if port and not port.isdigit():
            self.result.set_text("The port must be a number")
            return
        src = next((c for c in self._running() if cname(c) == it.get_string()), None)
        if not src:
            return
        set_busy(self.go, True)
        self.result.set_text("Checking…")
        self.result.set_css_classes(["caption", "dim-label"])

        def done(res):
            ip, ok = res
            set_busy(self.go, False)
            name = it.get_string()
            if not ip:
                msg, good = f"{name} can't resolve “{target}” — not on a shared network, or a typo", False
            elif ok is None:
                msg, good = f"{name} resolves {target} to {ip}", True
            elif ok:
                msg, good = f"Yes — {target} ({ip}) port {port} is open from {name}", True
            else:
                msg, good = (f"{target} resolves to {ip}, but port {port} is closed — is the service listening on "
                             "0.0.0.0, and on that port?"), False
            self.result.set_text(msg)
            self.result.set_css_classes(["caption", "ok-text" if good else "warn-text"])

        def fail(e):
            set_busy(self.go, False)
            self.result.set_text(f"Couldn't check: {e}")
        bg(reach, self.store.engine, src["Id"], target, int(port) if port else None, done=done, error=fail)
