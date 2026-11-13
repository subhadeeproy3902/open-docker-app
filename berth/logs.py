"""Logs explorer, Grafana/Loki style: a log-volume histogram coloured by level (drag to zoom),
time ranges or live tail, level filters with counts, regex search, and several containers at once."""
import calendar
import collections
import csv
import functools
import json
import re
import threading
import time

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from . import charts
from .engine import demux
from .util import PageHeader, bg, cname, esc, flat_page, fmt_bytes, icon_button, label, labels, thread

LEVELS = ("debug", "info", "warn", "error")
COL = {"error": "#f0605d", "warn": "#e8b04a", "info": "#5b9cf5", "debug": "#8a90a0"}
RANGES = [("live", "Live", 300), ("300", "5 min", 300), ("900", "15 min", 900), ("3600", "1 h", 3600),
          ("21600", "6 h", 21600), ("86400", "24 h", 86400), ("604800", "7 d", 604800)]

_ERR = re.compile(r"\b(ERROR|ERR|FATAL|CRITICAL|CRIT|PANIC|Traceback|Exception|panic:)\b")
_ERR_I = re.compile(r"(\"level\"\s*:\s*\"|level=|lvl=)(error|err|fatal|critical|crit|panic)", re.I)
_WARN = re.compile(r"\b(WARN|WARNING)\b|(\"level\"\s*:\s*\"|level=|lvl=)(warn|warning)", re.I)
_DEBUG = re.compile(r"\b(DEBUG|TRACE)\b|(\"level\"\s*:\s*\"|level=|lvl=)(debug|trace)", re.I)


def level_of(text):
    head = text[:400]
    if _ERR.search(head) or _ERR_I.search(head):
        return "error"
    if _WARN.search(head):
        return "warn"
    if _DEBUG.search(head):
        return "debug"
    return "info"


def parse_ts(ts):
    try:
        base = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        frac = ts[19:].split("Z")[0].split("+")[0]
        return base + (float("0" + frac) if frac.startswith(".") else 0.0)
    except ValueError:
        return time.time()


def split_lines(raw, tty, carry=None):
    """bytes from a logs stream → list of (stream, line) plus leftover state."""
    buf, pend = carry or (b"", {1: b"", 2: b""})
    if tty:
        pend[1] += raw
    else:
        frames, buf = demux(buf + raw)
        for s, d in frames:
            pend[s] += d
    out = []
    for s in (1, 2):
        *done, pend[s] = pend[s].split(b"\n")
        out += [(s, ln.decode(errors="replace").rstrip("\r")) for ln in done]
    return out, (buf, pend)


_KV = re.compile(r'(?<![\w.-])([A-Za-z_][\w.-]*)=("(?:[^"\\]|\\.)*"|[^\s"]+)')
_CONT = re.compile(r"^(\s+\S|at \S|Caused by:|\.\.\. \d+ (more|common frames)|\s*\^)")
_LEVEL_RANK = {lv: i for i, lv in enumerate(("debug", "info", "warn", "error"))}


@functools.lru_cache(maxsize=30000)
def fields(text):
    """Structured fields of a log line (#29): a JSON object's top-level values, or logfmt key=value pairs.
    A tuple of (name, value), so it can be cached."""
    t = text.strip()
    if t.startswith("{") and t.endswith("}"):
        try:
            d = json.loads(t)
        except ValueError:
            d = None
        if isinstance(d, dict):
            return tuple((k, v if isinstance(v, str) else json.dumps(v)) for k, v in d.items()
                         if not isinstance(v, (dict, list)))[:40]
    return tuple((k, v[1:-1] if v.startswith('"') else v) for k, v in _KV.findall(t))[:40]


def group_multiline(ents, last=None):
    """Fold stack-trace continuation lines (indented, "at …", "Caused by:") into the line before them from the same
    container, so a Python / Java / Node trace is one entry. last: {source: entry} carried over from earlier batches;
    returns (entries, last, changed) where changed lists earlier entries that got more lines."""
    last = dict(last or {})
    out, changed, here = [], [], {}          # here: source → index in out of its latest line from this batch
    for e in ents:
        p = last.get(e[1])
        if p is not None and _CONT.match(e[3]) and e[0] - p[0] < 2 and p[3].count("\n") < 200:
            lv = max(p[2], e[2], key=_LEVEL_RANK.get)
            merged = (p[0], p[1], lv, p[3] + "\n" + e[3])
            if e[1] in here:
                out[here[e[1]]] = merged
            else:                              # continues a line from an earlier batch
                orig = next((o for o, _n in changed if _n is p), p)
                changed = [(o, n) for o, n in changed if n is not p] + [(orig, merged)]
            last[e[1]] = merged
            continue
        here[e[1]] = len(out)
        out.append(e)
        last[e[1]] = e
    return out, last, changed


def entry(src, line):
    sp = line.find(" ")
    if sp > 0 and line[:4].isdigit():
        ts, text = line[:sp], line[sp + 1:]
        return (parse_ts(ts), src, level_of(text), text)
    return (time.time(), src, level_of(line), line)


class Histogram(Gtk.DrawingArea):
    """Stacked bars of log lines per time bucket. Drag across it to zoom into that span."""

    def do_measure(self, orientation, for_size):
        # 96 px tall when there is room, down to 56 px on short windows (so the page still fits at 520 px)
        return (56, 96, -1, -1) if orientation == Gtk.Orientation.VERTICAL else (120, 600, -1, -1)

    def __init__(self, on_zoom):
        super().__init__(hexpand=True)
        self.on_zoom = on_zoom
        self.entries, self.t0, self.t1, self.nb = [], 0, 1, 80
        self.buckets, self.sel = [], None
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tip)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", lambda g, x, y: setattr(self, "sel", [x, x]))
        drag.connect("drag-update", self._drag_upd)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)
        self.L, self.R, self.T, self.B = 46, 8, 6, 18

    def set_data(self, entries, t0, t1):
        self.entries, self.t0, self.t1 = entries, t0, max(t1, t0 + 1)
        w = max(1, self.get_width() - self.L - self.R)
        self.nb = max(20, min(150, w // 7))
        span = (self.t1 - self.t0) / self.nb
        b = [[0, 0, 0, 0] for _ in range(self.nb)]
        idx = {lv: i for i, lv in enumerate(LEVELS)}
        for e in entries:
            if self.t0 <= e[0] <= self.t1:
                k = min(self.nb - 1, int((e[0] - self.t0) / span))
                b[k][idx[e[2]]] += 1
        self.buckets = b
        self.queue_draw()

    def _x2t(self, x):
        pw = max(1, self.get_width() - self.L - self.R)
        return self.t0 + (min(max(x, self.L), self.L + pw) - self.L) / pw * (self.t1 - self.t0)

    def _drag_upd(self, g, dx, _dy):
        ok, sx, _sy = g.get_start_point()
        self.sel = [sx, sx + dx]
        self.queue_draw()

    def _drag_end(self, g, dx, _dy):
        ok, sx, _sy = g.get_start_point()
        a, b = sorted((sx, sx + dx))
        self.sel = None
        self.queue_draw()
        if b - a > 6:
            self.on_zoom(self._x2t(a), self._x2t(b))

    def _draw(self, _a, cr, w, h):
        fg = self.get_color()
        L, R, T, B = self.L, self.R, self.T, self.B
        pw, ph = w - L - R, h - T - B
        mx = max([sum(x) for x in self.buckets] or [0]) or 1
        top = max(2, charts.nice(mx))            # never repeat a tick label (0, 0, 1)
        cr.select_font_face("monospace")
        cr.set_font_size(10)
        for i in (0, 1, 2):
            v = top * i / 2
            y = round(T + ph - v / top * ph) + .5
            cr.set_source_rgba(fg.red, fg.green, fg.blue, .10 if i else .22)
            cr.move_to(L, y)
            cr.line_to(w - R, y)
            cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, .5)
            txt = f"{v:.0f}"
            cr.move_to(L - 6 - cr.text_extents(txt).width, y + 3.5)
            cr.show_text(txt)
        bw = pw / max(1, len(self.buckets))
        for i, cnt in enumerate(self.buckets):
            x = L + i * bw
            y = T + ph
            for lv_i in (3, 2, 1, 0):          # error at the bottom so it stands out
                n = cnt[lv_i]
                if not n:
                    continue
                hh = n / top * ph
                r, g, b, _ = charts.rgb(COL[LEVELS[lv_i]])
                cr.set_source_rgba(r, g, b, .9)
                cr.rectangle(x + .5, y - hh, max(1, bw - 1.5), hh)
                cr.fill()
                y -= hh
        span = self.t1 - self.t0
        fmt = "%H:%M:%S" if span <= 900 else "%H:%M" if span <= 86400 else "%d %b %H:%M"
        cr.set_source_rgba(fg.red, fg.green, fg.blue, .5)
        for k in range(5):
            t = self.t0 + span * k / 4
            txt = time.strftime(fmt, time.localtime(t))
            ext = cr.text_extents(txt)
            x = min(w - R - ext.width, max(L, L + pw * k / 4 - ext.width / 2))
            cr.move_to(x, h - 4)
            cr.show_text(txt)
        if self.sel:
            a, b = sorted(self.sel)
            acc = charts.rgb(charts.palette()[0])
            cr.set_source_rgba(acc[0], acc[1], acc[2], .18)
            cr.rectangle(a, T, b - a, ph)
            cr.fill()

    def _tip(self, _w, x, _y, _kb, tip):
        if not self.buckets or x < self.L:
            return False
        pw = max(1, self.get_width() - self.L - self.R)
        i = int((x - self.L) / pw * len(self.buckets))
        if not 0 <= i < len(self.buckets):
            return False
        span = (self.t1 - self.t0) / len(self.buckets)
        a = self.t0 + i * span
        cnt = self.buckets[i]
        tip.set_markup(f"<b>{time.strftime('%H:%M:%S', time.localtime(a))} – {time.strftime('%H:%M:%S', time.localtime(a + span))}</b>\n"
                       + "\n".join(f"<span foreground='{COL[lv]}'>■</span> {lv}  <tt>{cnt[j]}</tt>"
                                   for j, lv in reversed(list(enumerate(LEVELS)))) + "\n<i>drag to zoom</i>")
        return True


class LogsExplorer(Gtk.Box):
    """sources: callable → [(container_id, display name, tty)]"""
    MAX_VIEW = 6000

    def __init__(self, win, store, sources, show_source=False, picker=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.store, self.sources_fn, self.show_source = win, store, sources, show_source
        self.entries, self.names = [], []
        self.zoom = None
        self.gen = 0
        self.queue, self.qlock = [], threading.Lock()
        self.streams = []
        self.timer = 0
        self.loaded_key = None
        self._ttys = {}
        self._idle_ticks = 0

        bar = Adw.WrapBox(child_spacing=8, line_spacing=8, css_classes=["explorer-bar"])   # wraps when narrow
        if picker:
            bar.append(picker)
        self.range = Adw.ToggleGroup(can_shrink=False)
        for name, lbl, _s in RANGES:
            self.range.add(Adw.Toggle(name=name, label=lbl))
        self.range.set_active_name("900")
        self.range.connect("notify::active-name", lambda *_: self.reload())
        bar.append(self.range)
        self.search = Gtk.SearchEntry(placeholder_text="Search logs", hexpand=True)
        self.search.connect("search-changed", lambda *_: self._soon(self.render))
        self.search.connect("activate", lambda *_: self._next_hit())
        bar.append(self.search)
        self.regex = Gtk.ToggleButton(label=".*", tooltip_text="Regular expression")
        self.regex.add_css_class("monospace")
        self.regex.connect("toggled", lambda *_: self.render())
        bar.append(self.regex)
        self.append(bar)

        bar2 = Adw.WrapBox(child_spacing=6, line_spacing=6, css_classes=["explorer-bar", "second"])
        self.chips = {}
        for lv in reversed(LEVELS):
            b = Gtk.ToggleButton(active=True)
            b.add_css_class("level-chip")
            lb = Gtk.Label()
            b.set_child(lb)
            b.lb = lb
            b.connect("toggled", lambda *_: self.render())
            self.chips[lv] = b
            bar2.append(b)
        self.zoom_lbl = label("", css=["dim-label", "caption"], hexpand=True)
        bar2.append(self.zoom_lbl)
        self.reset = Gtk.Button(label="Reset zoom", visible=False)
        self.reset.add_css_class("flat")
        self.reset.connect("clicked", lambda *_: self._set_zoom(None))
        bar2.append(self.reset)
        # structured fields (#29): a popover with each field's top values; a click filters on it
        self.ffilters, self._last = {}, {}
        self.fields_btn = Gtk.MenuButton(label="Fields", tooltip_text="Filter by a field of JSON or key=value lines")
        self.fields_pop = Gtk.Popover()
        self.fields_pop.connect("show", lambda *_: self._fill_fields())
        self.fields_btn.set_popover(self.fields_pop)
        bar2.append(self.fields_btn)
        self.fchips = Gtk.Box(spacing=4)
        bar2.append(self.fchips)
        self.saved_btn = Gtk.MenuButton(icon_name="starred-symbolic", tooltip_text="Saved filters for these containers")
        self.saved_pop = Gtk.Popover()
        self.saved_pop.connect("show", lambda *_: self._fill_saved())
        self.saved_btn.set_popover(self.saved_pop)
        self.paused = Gtk.ToggleButton(icon_name="media-playback-pause-symbolic", tooltip_text="Pause the live view")
        self.paused.connect("toggled", lambda b: (b.set_icon_name("media-playback-start-symbolic" if b.get_active()
                                                                  else "media-playback-pause-symbolic"),
                                                  b.set_tooltip_text("Resume the live view" if b.get_active()
                                                                     else "Pause the live view"),
                                                  None if b.get_active() else self.render()))
        exp = Gio.Menu()
        for fmt, lbl in (("txt", "Text"), ("json", "JSON lines"), ("csv", "CSV")):
            exp.append(f"Export as {lbl}…", f"logx.export::{fmt}")
        self.export_btn = Gtk.MenuButton(icon_name="document-save-symbolic", menu_model=exp,
                                         tooltip_text="Export the lines shown (with their fields)")
        ag = Gio.SimpleActionGroup()
        a = Gio.SimpleAction.new("export", GLib.VariantType.new("s"))
        a.connect("activate", lambda _a, v: self._export(v.get_string()))
        ag.add_action(a)
        self.insert_action_group("logx", ag)
        self.ts = Gtk.ToggleButton(icon_name="preferences-system-time-symbolic", active=True, tooltip_text="Timestamps")
        self.ts.connect("toggled", lambda *_: self.render())
        self.wrap = Gtk.ToggleButton(icon_name="format-justify-left-symbolic", tooltip_text="Wrap lines")
        self.wrap.connect("toggled", lambda b: self.text.set_wrap_mode(Gtk.WrapMode.CHAR if b.get_active() else Gtk.WrapMode.NONE))
        self.follow = Gtk.ToggleButton(icon_name="go-bottom-symbolic", active=True, tooltip_text="Keep scrolled to the newest line")
        self.follow.connect("toggled", lambda b: b.get_active() and GLib.idle_add(self._to_end))
        for w in (self.saved_btn, self.paused, self.ts, self.wrap, self.follow, self.export_btn,
                  icon_button("edit-copy-symbolic", "Copy shown lines", self._copy, css=()),
                  icon_button("view-refresh-symbolic", "Reload", self.reload, css=())):
            bar2.append(w)
        self.append(bar2)

        hcard = Gtk.Box(css_classes=["card", "explorer-hist"])
        self.hist = Histogram(self._set_zoom)
        hcard.append(self.hist)
        self.append(hcard)
        self.status = label("", css=["dim-label", "caption", "explorer-status"])
        self.append(self.status)

        self.text = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                 left_margin=12, right_margin=12, top_margin=4, bottom_margin=12)
        self.text.add_css_class("logview")
        self.buf = self.text.get_buffer()
        self.buf.create_tag("ts", foreground_rgba=_rgba("#8a90a0"))
        for lv in LEVELS:
            if lv != "info":
                self.buf.create_tag(lv, foreground_rgba=_rgba(COL[lv]))
        self.buf.create_tag("hit", background_rgba=_rgba("#f2cc60", .45))
        self.src_tags = []
        self.sw = Gtk.ScrolledWindow(child=self.text, vexpand=True)
        self.empty = Adw.StatusPage(icon_name="text-x-generic-symbolic", title="No log lines here",
                                    description="Nothing matches in this time range. Try a longer range, "
                                                "clear the search, or switch on more levels.", visible=False)
        self.empty.add_css_class("compact")
        over = Gtk.Overlay(child=self.sw, vexpand=True)
        over.add_overlay(self.empty)
        self.append(over)
        self.connect("map", lambda *_: self.reload(only_if_changed=True))
        self.connect("unmap", lambda *_: self._stop())
        self._soon_id = 0

    # ── loading ──
    def _range(self):
        name = self.range.get_active_name() or "900"
        for n, _l, secs in RANGES:
            if n == name:
                return n == "live", secs
        return False, 900

    def reload(self, only_if_changed=False):
        srcs = self.sources_fn()
        live, secs = self._range()
        key = (tuple(s[0] for s in srcs), self.range.get_active_name())
        if only_if_changed and key == self.loaded_key:
            if live:
                self._start_live(srcs)
            return
        self.loaded_key = key
        self._stop()
        self.gen += 1
        gen = self.gen
        self.entries, self.zoom, self._last = [], None, {}
        self.names = [s[1] for s in srcs]
        self._make_src_tags()
        self.reset.set_visible(False)
        self.zoom_lbl.set_text("")
        if not srcs:
            self.status.set_text("No containers selected.")
            self.render()
            return
        self.status.set_text(f"Loading logs from {len(srcs)} container{'s' if len(srcs) > 1 else ''}…")
        since = int(time.time() - secs)
        tail = str(max(1500, 30000 // len(srcs)))

        def work():
            out = []
            for i, (cid, _n, _t) in enumerate(srcs):
                if gen != self.gen:
                    return None
                try:
                    tty = self._tty(cid)
                    st = self.store.engine.logs(cid, tail=tail, follow=False, since=since)
                except Exception:  # noqa: BLE001
                    continue
                carry = None
                while True:
                    c = st.read(5.0)
                    if c is None:
                        continue
                    if not c:
                        break
                    lines, carry = split_lines(c, tty, carry)
                    out += [entry(i, ln) for _s, ln in lines]
                st.close()
            out.sort(key=lambda e: e[0])
            out, self._last, _c = group_multiline(out)
            return out

        def done(res):
            if res is None or gen != self.gen:
                return
            self.entries = res
            self.status.set_text("")
            self.render()
            if live:
                self._start_live(srcs)
        bg(work, done=done, error=lambda e: self.status.set_text(f"Could not read logs: {e}"))

    def _start_live(self, srcs):
        if self.streams or not self.get_mapped():
            return
        gen = self.gen
        for i, (cid, _n, _t) in enumerate(srcs):
            thread(self._follow, gen, i, cid, None)
        self.timer = GLib.timeout_add(400, self._flush)

    def _tty(self, cid):
        t = self._ttys.get(cid)
        if t is None:
            t = self._ttys[cid] = bool((self.store.engine.inspect(cid).get("Config") or {}).get("Tty"))
        return t

    def _follow(self, gen, i, cid, tty):
        try:
            tty = self._tty(cid)
            st = self.store.engine.logs(cid, tail="0", follow=True)
        except Exception:  # noqa: BLE001
            return
        self.streams.append(st)
        carry = None
        while gen == self.gen:
            c = st.read(1.0)
            if c is None:
                continue
            if not c:
                break
            lines, carry = split_lines(c, tty, carry)
            if lines:
                with self.qlock:
                    self.queue += [entry(i, ln) for _s, ln in lines]
        st.close()

    def _stop(self):
        self.gen += 0          # keep current data; just stop streams
        for st in self.streams:
            st.close()
        self.streams = []
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0

    def _flush(self):
        with self.qlock:
            q, self.queue = self.queue, []
        if not q:
            self._idle_ticks += 1
            if self._idle_ticks % 5 == 0:          # slide the live window every 2 s when quiet
                self._histo()
            return True
        self._idle_ticks = 0
        q.sort(key=lambda e: e[0])
        q, self._last, changed = group_multiline(q, self._last)
        if changed:                      # a trace continued from an earlier batch: swap in the longer entry
            idx = {id(old): new for old, new in changed}
            self.entries = [idx.get(id(e), e) for e in self.entries]
        self.entries += q
        if self.paused.get_active():
            self.paused.set_tooltip_text(f"Resume the live view ({len(q)} new lines waiting…)")
            self._counts()
            return True
        cut = time.time() - 3600
        if len(self.entries) > 60000:
            self.entries = [e for e in self.entries if e[0] >= cut][-60000:]
        shown = [e for e in q if self._keep(e)]
        if shown and self.zoom is None:
            self.empty.set_visible(False)
            self._append(shown)
        self._counts()
        self._histo()
        return True

    # ── filtering + rendering ──
    def _matcher(self):
        q = self.search.get_text()
        if not q:
            return None
        if self.regex.get_active():
            try:
                return re.compile(q, re.I)
            except re.error:
                return None
        return re.compile(re.escape(q), re.I)

    def _keep(self, e, m=None, z=None):
        if not self.chips[e[2]].get_active():
            return False
        z = z if z is not None else self.zoom
        if z and not (z[0] <= e[0] <= z[1]):
            return False
        m = m if m is not None else self._matcher()
        return (not m or bool(m.search(e[3]))) and self._fields_ok(e)

    def _fields_ok(self, e):
        if not self.ffilters:
            return True
        f = dict(fields(e[3]))
        return all(f.get(k) == v for k, v in self.ffilters.items())

    def _window(self):
        live, secs = self._range()
        now = time.time()
        return (now - secs, now)

    def _histo(self):
        t0, t1 = self.zoom or self._window()
        m = self._matcher()
        ents = [e for e in self.entries if (not m or m.search(e[3])) and self._fields_ok(e)] \
            if (m or self.ffilters) else self.entries
        self.hist.set_data(ents, t0, t1)

    def _counts(self):
        t0, t1 = self.zoom or self._window()
        m = self._matcher()
        c = dict.fromkeys(LEVELS, 0)
        for e in self.entries:
            if t0 <= e[0] <= t1 and (not m or m.search(e[3])) and self._fields_ok(e):
                c[e[2]] += 1
        for lv, b in self.chips.items():
            b.lb.set_markup(f"<span foreground='{COL[lv]}'>●</span> {lv.capitalize()}  <b>{_short(c[lv])}</b>")

    def render(self):
        m = self._matcher()
        z = self.zoom
        shown = [e for e in self.entries if self._keep(e, m, z)]
        self.buf.set_text("")
        self._append(shown[-self.MAX_VIEW:], scroll=False)
        extra = len(shown) - self.MAX_VIEW
        self.status.set_text(f"{len(shown):,} lines match" + (f" — showing the newest {self.MAX_VIEW:,}" if extra > 0 else ""))
        self.empty.set_visible(not shown and not self.status.get_text().startswith("Loading"))
        self._counts()
        self._histo()
        self._highlight(m)
        if self.follow.get_active():
            GLib.idle_add(self._to_end)

    def _append(self, ents, scroll=True):
        it = self.buf.get_end_iter()
        ts_on = self.ts.get_active()
        for t, src, lv, text in ents:
            if ts_on:
                self.buf.insert_with_tags_by_name(it, time.strftime("%H:%M:%S", time.localtime(t)) + "  ", "ts")
            if self.show_source and src < len(self.src_tags):
                self.buf.insert_with_tags(it, f"{self.names[src][:22]:<22} ", self.src_tags[src])
            if lv == "info":
                self.buf.insert(it, text + "\n")
            else:
                self.buf.insert_with_tags_by_name(it, text + "\n", lv)
        over = self.buf.get_line_count() - self.MAX_VIEW
        if over > 0:
            self.buf.delete(self.buf.get_start_iter(), self.buf.get_iter_at_line(over)[1])
        if scroll and self.follow.get_active():
            GLib.idle_add(self._to_end)

    def _make_src_tags(self):
        table = self.buf.get_tag_table()
        for t in self.src_tags:
            table.remove(t)
        pal = charts.palette()
        self.src_tags = [self.buf.create_tag(None, foreground_rgba=_rgba(pal[i % len(pal)]), weight=Pango.Weight.BOLD)
                         for i in range(len(self.names))]

    def _set_zoom(self, a, b=None):
        self.zoom = (a, b) if a is not None else None
        self.reset.set_visible(self.zoom is not None)
        self.zoom_lbl.set_text(f"Zoomed to {time.strftime('%H:%M:%S', time.localtime(a))} – "
                               f"{time.strftime('%H:%M:%S', time.localtime(b))}" if self.zoom else "")
        if self.zoom:
            self.follow.set_active(False)
        self.render()

    def _highlight(self, m):
        if not m:
            return
        text = self.buf.get_text(self.buf.get_start_iter(), self.buf.get_end_iter(), False)
        n = 0
        for mt in m.finditer(text):
            if n > 4000 or mt.end() == mt.start():
                break
            self.buf.apply_tag_by_name("hit", self.buf.get_iter_at_offset(mt.start()), self.buf.get_iter_at_offset(mt.end()))
            n += 1

    def _next_hit(self):
        m = self._matcher()
        if not m:
            return
        text = self.buf.get_text(self.buf.get_start_iter(), self.buf.get_end_iter(), False)
        cur = self.buf.get_iter_at_mark(self.buf.get_insert()).get_offset()
        mt = m.search(text, cur + 1) or m.search(text)
        if mt:
            self.follow.set_active(False)
            it = self.buf.get_iter_at_offset(mt.start())
            self.buf.place_cursor(it)
            self.text.scroll_to_iter(it, 0.2, False, 0, 0)

    def _to_end(self):
        adj = self.sw.get_vadjustment()
        adj.set_value(adj.get_upper())
        return False

    def _copy(self):
        self.get_clipboard().set(self.buf.get_text(self.buf.get_start_iter(), self.buf.get_end_iter(), False))
        self.win.toast("Copied the shown lines")

    # ── fields (#29) ──
    def _fill_fields(self):
        t0, t1 = self.zoom or self._window()
        stats = collections.defaultdict(collections.Counter)
        n = 0
        for e in reversed(self.entries):
            if not (t0 <= e[0] <= t1):
                continue
            for k, v in fields(e[3]):
                stats[k][v] += 1
            n += 1
            if n >= 20000:
                break
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=10, margin_end=10, margin_top=10,
                      margin_bottom=10)
        if not stats:
            box.append(label("No JSON or key=value fields in these lines.", css=["dim-label"]))
        keys = sorted(stats, key=lambda k: -sum(stats[k].values()))[:14]
        for k in keys:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            row.append(label(f"{k}  ·  {sum(stats[k].values()):,} lines", css=["heading", "monospace"]))
            vals = Adw.WrapBox(child_spacing=4, line_spacing=4)
            for v, c in stats[k].most_common(6):
                b = Gtk.Button(label=f"{v[:30]}  {_short(c)}", tooltip_text=f"Show only {k}={v}")
                b.add_css_class("monospace")
                b.connect("clicked", lambda *_a, k=k, v=v: (self.fields_pop.popdown(), self.add_field(k, v)))
                vals.append(b)
            row.append(vals)
            box.append(row)
        self.fields_pop.set_child(Gtk.ScrolledWindow(child=box, propagate_natural_height=True, max_content_height=480,
                                                     propagate_natural_width=True, max_content_width=520))

    def add_field(self, k, v):
        self.ffilters[k] = v
        self._chips()
        self.render()

    def _chips(self):
        while (c := self.fchips.get_first_child()) is not None:
            self.fchips.remove(c)
        for k, v in self.ffilters.items():
            b = Gtk.Button(tooltip_text="Remove this filter")
            b.set_child(label(f"{k}={v}  ✕", css=["monospace", "caption"]))
            b.add_css_class("pill")
            b.connect("clicked", lambda *_a, k=k: (self.ffilters.pop(k, None), self._chips(), self.render()))
            self.fchips.append(b)

    # ── saved filters per stack / container (#29) ──
    def _scope(self):
        return ",".join(sorted(self.names)) or "all"

    def _query(self):
        return {"q": self.search.get_text(), "re": self.regex.get_active(), "fields": dict(self.ffilters),
                "levels": [lv for lv, b in self.chips.items() if b.get_active()]}

    def _apply(self, q):
        self.ffilters = dict(q.get("fields") or {})
        self._chips()
        self.regex.set_active(bool(q.get("re")))
        for lv, b in self.chips.items():
            b.set_active(lv in (q.get("levels") or LEVELS))
        self.search.set_text(q.get("q", ""))
        self.render()

    def _fill_saved(self):
        st = self.win.settings
        saved = (st["log_queries"] or {}).get(self._scope(), [])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=10, margin_end=10, margin_top=10,
                      margin_bottom=10)
        for i, q in enumerate(saved):
            row = Gtk.Box(spacing=4)
            b = Gtk.Button(label=q["name"], hexpand=True, tooltip_text=json.dumps({k: v for k, v in q.items()
                                                                                   if k != "name"}))
            b.connect("clicked", lambda *_a, q=q: (self.saved_pop.popdown(), self._apply(q)))
            row.append(b)
            x = icon_button("user-trash-symbolic", "Forget this filter", lambda i=i: self._forget(i), css=("flat",))
            row.append(x)
            box.append(row)
        if not saved:
            box.append(label("No saved filters for these containers yet.", css=["dim-label", "caption"]))
        e = Gtk.Entry(placeholder_text="Name for the current filter")
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        go = lambda *_: e.get_text().strip() and (self._save_query(e.get_text().strip()), self.saved_pop.popdown())
        save.connect("clicked", go)
        e.connect("activate", go)
        row = Gtk.Box(spacing=4)
        row.append(e)
        row.append(save)
        box.append(row)
        self.saved_pop.set_child(box)

    def _save_query(self, name):
        st = self.win.settings
        allq = dict(st["log_queries"] or {})
        lst = [q for q in allq.get(self._scope(), []) if q["name"] != name]
        allq[self._scope()] = lst + [{"name": name, **self._query()}]
        st["log_queries"] = allq
        self.win.toast(f"Saved “{name}”")

    def _forget(self, i):
        st = self.win.settings
        allq = dict(st["log_queries"] or {})
        lst = list(allq.get(self._scope(), []))
        if 0 <= i < len(lst):
            lst.pop(i)
        allq[self._scope()] = lst
        st["log_queries"] = allq
        self._fill_saved()

    # ── export (#29) ──
    def _shown(self):
        m, z = self._matcher(), self.zoom
        return [e for e in self.entries if self._keep(e, m, z)]

    def _export(self, fmt):
        from .pages import save_dialog
        rows = self._shown()
        if not rows:
            return self.win.toast("No lines to export")

        def go(path):
            try:
                with open(path, "w", newline="") as f:
                    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"
                    if fmt == "txt":
                        for t, src, lv, text in rows:
                            f.write(f"{iso(t)} {self.names[src] if src < len(self.names) else ''} {text}\n")
                    elif fmt == "json":
                        for t, src, lv, text in rows:
                            f.write(json.dumps({"time": iso(t), "container": self.names[src] if src < len(self.names)
                                                else "", "level": lv, "message": text, "fields": dict(fields(text))})
                                    + "\n")
                    else:
                        keys = sorted({k for e in rows for k, _v in fields(e[3])})[:30]
                        w = csv.writer(f)
                        w.writerow(["time", "container", "level", "message"] + keys)
                        for t, src, lv, text in rows:
                            fd = dict(fields(text))
                            w.writerow([iso(t), self.names[src] if src < len(self.names) else "", lv, text] +
                                       [fd.get(k, "") for k in keys])
                self.win.toast(f"Exported {len(rows):,} lines to {path}")
            except OSError as e:
                self.win.toast(f"Couldn't export: {e}")
        save_dialog(self.win, "Export logs", f"logs.{fmt if fmt != 'json' else 'jsonl'}", go)

    def _soon(self, fn):
        if self._soon_id:
            GLib.source_remove(self._soon_id)
        self._soon_id = GLib.timeout_add(180, lambda: (setattr(self, "_soon_id", 0), fn()) and False)


def _short(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _rgba(hexstr, a=1.0):
    c = Gdk.RGBA()
    c.parse(hexstr)
    c.alpha = a
    return c


class LogsPage(Gtk.Box):
    """Sidebar page: logs of all running containers, one stack or one container."""
    title, icon = "Logs", "text-x-generic-symbolic"
    wants_stats = False

    def __init__(self, win, store, settings):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.store = win, store
        tv = Adw.ToolbarView(vexpand=True)
        flat_page(tv, PageHeader("Logs", "Every container's logs in one place — pick a stack or container, a time range, a level"))
        self.choices = []          # [(label, kind, value)]
        self.model = Gtk.StringList()
        self.picker = Gtk.DropDown(model=self.model, enable_search=True, tooltip_text="Which containers")
        self.picker.set_size_request(240, -1)
        self.picker.connect("notify::selected", lambda *_: self.explorer.reload())
        self.explorer = LogsExplorer(win, store, self._sources, show_source=True, picker=self.picker)
        tv.set_content(self.explorer)
        self.append(tv)
        store.connect("containers", lambda *_: self._choices())
        self._choices()

    def _choices(self):
        cs = self.store.containers
        projects = sorted({labels(c).get("com.docker.compose.project") for c in cs} - {None, ""})
        new = [("All running containers", "all", None)] + \
              [(f"Stack · {p}", "project", p) for p in projects] + \
              [(f"{cname(c)}", "container", c["Id"]) for c in sorted(cs, key=cname)]
        if [n for n, *_ in new] == [n for n, *_ in self.choices]:
            self.choices = new
            return
        cur = self.choices[self.picker.get_selected()][0] if self.choices and self.picker.get_selected() < len(self.choices) else None
        self.choices = new
        self.model.splice(0, self.model.get_n_items(), [n for n, *_ in new])
        names = [n for n, *_ in new]
        if cur in names:
            self.picker.set_selected(names.index(cur))

    def show_project(self, project):
        """Open the page on one Compose stack's logs (from the Stacks page)."""
        self._choices()
        names = [n for n, *_ in self.choices]
        if f"Stack · {project}" in names:
            self.picker.set_selected(names.index(f"Stack · {project}"))

    def _sources(self):
        if not self.choices:
            return []
        i = self.picker.get_selected()
        _l, kind, val = self.choices[min(i, len(self.choices) - 1)]
        cs = self.store.containers
        if kind == "all":
            pick = [c for c in cs if c["State"] == "running"]
        elif kind == "project":
            pick = [c for c in cs if labels(c).get("com.docker.compose.project") == val]
        else:
            pick = [c for c in cs if c["Id"] == val]
        return [(c["Id"], labels(c).get("com.docker.compose.service") or cname(c), False) for c in pick]

    def on_show(self):
        pass

    def focus_search(self):
        self.explorer.search.grab_focus()
