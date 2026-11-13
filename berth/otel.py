"""OpenTelemetry traces (#30, "later" part): a tiny OTLP/HTTP receiver inside Berth, so apps in containers can send
their traces here and see them as a span waterfall — no collector, Jaeger or Tempo needed.

- Opt-in (Traces page), only while Berth runs, spans kept in memory only (the newest 20,000).
- Listens on 127.0.0.1 and on Docker's bridge gateway (local Linux engines), never on every network.
- Accepts /v1/traces as protobuf (the SDKs' default) or JSON, gzip or not. A small protobuf reader is included,
  so Berth keeps using only the standard library."""
import collections
import gzip
import http.server
import json
import struct
import threading
import time

from gi.repository import Adw, GLib, Gtk, Pango

from . import charts
from .util import esc, label

PORT = 4318
SPANS = collections.deque(maxlen=20000)
LOCK = threading.Lock()
_servers = []


# ── protobuf (wire format) ──
def _varint(b, i):
    shift = val = 0
    while True:
        c = b[i]
        i += 1
        val |= (c & 0x7F) << shift
        if not c & 0x80:
            return val, i
        shift += 7


def fields(b):
    """[(field number, value)] of one protobuf message (value: int, or bytes for length-delimited)."""
    out, i, n = [], 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = struct.unpack_from("<Q", b, i)[0], i + 8
        elif wt == 2:
            ln, i = _varint(b, i)
            v, i = bytes(b[i:i + ln]), i + ln
        elif wt == 5:
            v, i = struct.unpack_from("<I", b, i)[0], i + 4
        else:
            raise ValueError(f"wire type {wt}")
        out.append((f, v))
    return out


def _any(b):
    for f, v in fields(b):
        if f == 1:
            return v.decode(errors="replace")
        if f == 2:
            return bool(v)
        if f == 3:
            return v - (1 << 64) if v >= 1 << 63 else v
        if f == 4:
            return struct.unpack("<d", struct.pack("<Q", v))[0]
    return ""


def _kv(b):
    k = v = None
    for f, x in fields(b):
        if f == 1:
            k = x.decode(errors="replace")
        elif f == 2:
            v = _any(x)
    return k, v


def from_protobuf(body):
    out = []
    for f, rs in fields(body):
        if f != 1:
            continue
        service, scopes = "unknown", []
        for g, x in fields(rs):
            if g == 1:                                  # Resource
                for h, kv in fields(x):
                    if h == 1:
                        k, v = _kv(kv)
                        if k == "service.name":
                            service = str(v)
            elif g == 2:
                scopes.append(x)
        for ss in scopes:
            for g, sp in fields(ss):
                if g != 2:
                    continue
                s = {"service": service, "attrs": {}, "status": 0, "parent": ""}
                for h, x in fields(sp):
                    if h == 1:
                        s["trace"] = x.hex()
                    elif h == 2:
                        s["id"] = x.hex()
                    elif h == 4:
                        s["parent"] = x.hex()
                    elif h == 5:
                        s["name"] = x.decode(errors="replace")
                    elif h == 6:
                        s["kind"] = x
                    elif h == 7:
                        s["start"] = x
                    elif h == 8:
                        s["end"] = x
                    elif h == 9:
                        k, v = _kv(x)
                        if k and len(s["attrs"]) < 30:
                            s["attrs"][k] = v
                    elif h == 15:
                        for st, val in fields(x):
                            if st == 3:
                                s["status"] = val
                out.append(s)
    return out


def from_json(body):
    d = json.loads(body)
    out = []
    for rs in d.get("resourceSpans") or []:
        service = "unknown"
        for a in ((rs.get("resource") or {}).get("attributes") or []):
            if a.get("key") == "service.name":
                service = next(iter((a.get("value") or {}).values()), service)
        for ss in rs.get("scopeSpans") or []:
            for sp in ss.get("spans") or []:
                out.append({"service": str(service), "trace": sp.get("traceId", ""), "id": sp.get("spanId", ""),
                            "parent": sp.get("parentSpanId", ""), "name": sp.get("name", ""),
                            "start": int(sp.get("startTimeUnixNano", 0)), "end": int(sp.get("endTimeUnixNano", 0)),
                            "status": int((sp.get("status") or {}).get("code", 0)),
                            "attrs": {a.get("key"): next(iter((a.get("value") or {}).values()), "")
                                      for a in (sp.get("attributes") or [])[:30]}})
    return out


# ── receiver ──
class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/traces":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(n, 32 * 2**20))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        js = "json" in (self.headers.get("Content-Type") or "")
        try:
            spans = from_json(body) if js else from_protobuf(body)
        except Exception:  # noqa: BLE001 — a bad payload must not stop the receiver
            self.send_error(400)
            return
        with LOCK:
            SPANS.extend(s for s in spans if s.get("trace") and s.get("id"))
        reply = b"{}" if js else b""
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if js else "application/x-protobuf")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


def addresses(engine):
    """Where to listen: this computer, plus Docker's bridge gateway for a local Linux engine (so containers reach it)."""
    import os
    import sys
    out = ["127.0.0.1"]
    extra = os.environ.get("BERTH_OTEL_BIND")             # tests: an extra address
    if extra:
        out.append(extra)
    if sys.platform.startswith("linux") and engine.path.startswith(("/", "unix://")):
        try:
            for n in engine.networks():
                if n.get("Name") == "bridge":
                    for c in ((n.get("IPAM") or {}).get("Config") or []):
                        if c.get("Gateway"):
                            out.append(c["Gateway"])
        except Exception:  # noqa: BLE001
            pass
    return list(dict.fromkeys(out))


def start(engine):
    """Start listening; returns the addresses, raises OSError when port 4318 is taken."""
    stop()
    addrs = addresses(engine)
    for a in addrs:
        try:
            srv = http.server.ThreadingHTTPServer((a, PORT), _Handler)
        except OSError:
            if a == "127.0.0.1":
                stop()
                raise
            continue
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        _servers.append((a, srv))
    return [a for a, _s in _servers]


def stop():
    while _servers:
        _a, srv = _servers.pop()
        srv.shutdown()
        srv.server_close()


def running():
    return bool(_servers)


def traces():
    """Newest first: [{trace, service, name, start, duration ms, spans, errors}]."""
    with LOCK:
        spans = list(SPANS)
    by = {}
    for s in spans:
        by.setdefault(s["trace"], []).append(s)
    out = []
    for tid, ss in by.items():
        root = next((s for s in ss if not s.get("parent") or s["parent"].strip("0") == ""), None) or \
            min(ss, key=lambda s: s.get("start", 0))
        start = min(s.get("start", 0) for s in ss)
        end = max(s.get("end", 0) for s in ss)
        out.append({"trace": tid, "service": root["service"], "name": root.get("name", "?"), "start": start,
                    "ms": (end - start) / 1e6, "spans": len(ss), "errors": sum(1 for s in ss if s.get("status") == 2)})
    out.sort(key=lambda t: -t["start"])
    return out


def spans_of(tid):
    with LOCK:
        ss = [s for s in SPANS if s["trace"] == tid]
    kids = collections.defaultdict(list)
    ids = {s["id"] for s in ss}
    for s in ss:
        kids[s["parent"] if s.get("parent") in ids else ""].append(s)
    order = []

    def walk(pid, depth):
        for s in sorted(kids.get(pid, []), key=lambda s: s.get("start", 0)):
            order.append((depth, s))
            walk(s["id"], depth + 1)
    walk("", 0)
    return order


# ── UI ──
class Waterfall(Gtk.DrawingArea):
    ROW = 24

    def __init__(self):
        super().__init__(hexpand=True)
        self.rows = []
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tip)

    def show(self, rows):
        self.rows = rows
        self.set_content_height(max(40, len(rows) * self.ROW + 30))
        self.queue_draw()

    def _span(self):
        if not self.rows:
            return 0, 1
        t0 = min(s.get("start", 0) for _d, s in self.rows)
        t1 = max(s.get("end", 0) for _d, s in self.rows)
        return t0, max(1, t1 - t0)

    def _draw(self, _a, cr, w, h):
        if not self.rows:
            return
        fg = self.get_color()
        pal = charts.palette()
        services = sorted({s["service"] for _d, s in self.rows})
        col = {sv: pal[i % len(pal)] for i, sv in enumerate(services)}
        t0, span = self._span()
        left = min(320, w * 0.38)
        for i, (depth, s) in enumerate(self.rows):
            y = 6 + i * self.ROW
            cr.set_source_rgba(fg.red, fg.green, fg.blue, .85)
            cr.select_font_face("sans")
            cr.set_font_size(11)
            cr.move_to(8 + depth * 14, y + 15)
            cr.show_text((s.get("name") or "?")[:40])
            x0 = left + (s.get("start", t0) - t0) / span * (w - left - 70)
            x1 = left + (s.get("end", t0) - t0) / span * (w - left - 70)
            r, g, b, _ = charts.rgb("#e01b24" if s.get("status") == 2 else col[s["service"]])
            cr.set_source_rgba(r, g, b, .85)
            cr.rectangle(x0, y + 4, max(2, x1 - x0), self.ROW - 9)
            cr.fill()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, .6)
            cr.set_font_size(10)
            cr.move_to(min(w - 64, max(x1, x0 + 2) + 6), y + 15)
            cr.show_text(f"{(s.get('end', 0) - s.get('start', 0)) / 1e6:.1f} ms")

    def _tip(self, _w, _x, y, _kb, tip):
        i = int((y - 6) // self.ROW)
        if not 0 <= i < len(self.rows):
            return False
        s = self.rows[i][1]
        attrs = "\n".join(f"{esc(str(k))}: {esc(str(v))[:80]}" for k, v in list(s.get("attrs", {}).items())[:12])
        tip.set_markup(f"<b>{esc(s.get('name', ''))}</b> · {esc(s['service'])}\n"
                       f"{(s.get('end', 0) - s.get('start', 0)) / 1e6:.2f} ms"
                       + (" · <span foreground='#e01b24'>error</span>" if s.get("status") == 2 else "")
                       + (f"\n{attrs}" if attrs else ""))
        return True


class TracesPage(Gtk.Box):
    title, icon = "Traces", "network-transmit-receive-symbolic"
    wants_stats = False

    def __init__(self, win, store, settings):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        from .util import PageHeader, flat_page, set_busy
        self.win, self.store, self.settings, self._busy = win, store, settings, set_busy
        tv = Adw.ToolbarView(vexpand=True)
        self.header = PageHeader("Traces", "OpenTelemetry traces your apps send to Berth — kept in memory while Berth "
                                           "runs")
        self.btn = Gtk.Button(label="Start receiving", valign=Gtk.Align.CENTER)
        self.btn.add_css_class("suggested-action")
        self.btn.connect("clicked", lambda *_: self._toggle())
        self.header.add(self.btn)
        flat_page(tv, self.header)
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, position=300, css_classes=["page-body"])
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.how = Gtk.Label(xalign=0, wrap=True, selectable=True, css_classes=["caption", "dim-label"])
        top.append(self.how)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["boxed-list"])
        self.list.connect("row-activated", lambda _l, r: self._open(r.trace))
        top.append(Gtk.ScrolledWindow(child=self.list, vexpand=True))
        paned.set_start_child(top)
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8)
        self.title_lbl = label("Pick a trace above", css=["heading"])
        bottom.append(self.title_lbl)
        self.fall = Waterfall()
        bottom.append(Gtk.ScrolledWindow(child=self.fall, vexpand=True))
        paned.set_end_child(bottom)
        tv.set_content(paned)
        self.append(tv)
        self.timer = 0
        self._shown = None
        self._state()
        if settings["otel"]:
            GLib.idle_add(lambda: self._toggle() and False)

    def _state(self):
        on = running()
        self.btn.set_label("Stop receiving" if on else "Start receiving")
        self.btn.set_css_classes(["destructive-action" if on else "suggested-action"])
        addrs = [a for a, _s in _servers]
        if on:
            inner = next((a for a in addrs if a != "127.0.0.1"), None)
            self.how.set_markup(
                "Point your app's OpenTelemetry SDK here (protobuf or JSON over HTTP):\n"
                f"<tt>OTEL_EXPORTER_OTLP_ENDPOINT=http://{inner or 'host.docker.internal'}:{PORT}</tt>  "
                "(from a container)  ·  "
                f"<tt>http://localhost:{PORT}</tt>  (from this computer)\n"
                "<tt>OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf</tt>  ·  <tt>OTEL_SERVICE_NAME=your-service</tt>")
        else:
            self.how.set_text("Berth can receive OpenTelemetry traces from your apps and show each request as a "
                              f"waterfall of spans. Start receiving to listen on port {PORT} (this computer and "
                              "Docker's bridge only).")

    def _toggle(self):
        if running():
            stop()
            self.settings["otel"] = False
        else:
            try:
                start(self.store.engine)
                self.settings["otel"] = True
            except OSError as e:
                self.win.toast(f"Port {PORT} is taken ({e.strerror}) — is another collector running?")
        self._state()
        if running() and not self.timer:
            self.timer = GLib.timeout_add_seconds(2, self._refresh)
        self._refresh()

    def _refresh(self):
        if not self.get_mapped() and running():
            return True
        ts = traces()[:300]
        key = tuple((t["trace"], t["spans"]) for t in ts)
        if key != self._shown:
            self._shown = key
            while (c := self.list.get_first_child()) is not None:
                self.list.remove(c)
            for t in ts:
                row = Gtk.ListBoxRow()
                row.trace = t["trace"]
                b = Gtk.Box(spacing=10, margin_start=10, margin_end=10, margin_top=5, margin_bottom=5)
                b.append(Gtk.Label(label=time.strftime("%H:%M:%S", time.localtime(t["start"] / 1e9)),
                                   css_classes=["numeric", "dim-label"]))
                b.append(Gtk.Label(label=t["service"], css_classes=["heading"]))
                b.append(Gtk.Label(label=t["name"], xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
                b.append(Gtk.Label(label=f"{t['spans']} spans", css_classes=["dim-label"]))
                if t["errors"]:
                    b.append(Gtk.Label(label=f"{t['errors']} errors", css_classes=["status-pill", "bad"]))
                b.append(Gtk.Label(label=f"{t['ms']:.1f} ms", css_classes=["numeric"], width_chars=10, xalign=1))
                row.set_child(b)
                self.list.append(row)
            if not ts:
                self.list.append(label("No traces yet." if running() else "Not receiving.",
                                       css=["dim-label"], margin_top=12, margin_bottom=12))
        if not running():
            self.timer = 0
            return False
        return True

    def _open(self, tid):
        rows = spans_of(tid)
        t = next((x for x in traces() if x["trace"] == tid), None)
        self.title_lbl.set_text(f"{t['service']} · {t['name']} · {t['ms']:.1f} ms · {len(rows)} spans" if t else tid)
        self.fall.show(rows)

    def on_show(self):
        self._refresh()
