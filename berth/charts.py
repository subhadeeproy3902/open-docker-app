"""Hand-drawn charts with cairo. They redraw only when given new data; no timers,
no animation, so an idle window costs nothing."""
import math
import time

from gi.repository import Adw, Gtk

from .util import esc

DARK = ["#8ab4ff", "#f6a96b", "#7fd49a", "#cf9cf5", "#f2cc60", "#6fd3dc", "#f28ba0", "#b7c0d1"]
LIGHT = ["#2f6fe0", "#d06a1c", "#2f9a55", "#8e4fd1", "#b88a00", "#178f99", "#cc3d63", "#5c6577"]


def palette():
    return DARK if Adw.StyleManager.get_default().get_dark() else LIGHT


def rgb(hexstr, a=1.0):
    h = hexstr.lstrip("#")
    return int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255, a


def nice(v, binary=False):
    if v <= 0:
        return 1024 * 1024 if binary else 1
    base = 1024 ** math.floor(math.log(v, 1024)) if binary else 1
    v /= base
    p = 10 ** math.floor(math.log10(v))
    n = v / p
    step = 1 if n <= 1 else 2 if n <= 2 else 2.5 if n <= 2.5 else 5 if n <= 5 else 10
    return step * p * base


class LineChart(Gtk.DrawingArea):
    """series: [(name, hexcolor, [(t, v), ...])]; fmt formats axis/tooltip values."""

    def __init__(self, fmt, height=180, binary=False, fill=False):
        super().__init__(content_height=height, hexpand=True)
        self.fmt, self.binary, self.fill = fmt, binary, fill
        self.series, self.hidden, self.window = [], set(), 600
        self.markers, self.on_click = [], None     # [(time, text, colour)] — vertical lines (restarts, pulls…)
        self.dashed = set()                        # series drawn dashed (a comparison, e.g. the day before)
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)
        click = Gtk.GestureClick()
        click.connect("released", self._clicked)
        self.add_controller(click)

    def set_data(self, series, window=None, markers=None):
        self.series = series
        if window:
            self.window = window
        if markers is not None:
            self.markers = markers
        self.queue_draw()

    def _t_at(self, x):
        L, R, T, B, pw, ph = self._geom(self.get_width(), self.get_height())
        return time.time() - self.window + (x - L) / max(1, pw) * self.window

    def _clicked(self, _g, _n, x, _y):
        if self.on_click and x >= self._geom(self.get_width(), self.get_height())[0]:
            self.on_click(self._t_at(x))

    @staticmethod
    def _ago(secs):
        if secs <= 0:
            return "now"
        if secs < 3600:
            return f"−{round(secs / 60)}m"
        if secs < 2 * 86400:
            return f"−{secs / 3600:.0f}h"
        return f"−{secs / 86400:.0f}d"

    def _geom(self, w, h):
        return 58, 8, 8, 20, w - 66, h - 28        # left, right, top, bottom, plot w, plot h

    def _draw(self, _area, cr, w, h):
        fg = self.get_color()
        L, R, T, B, pw, ph = self._geom(w, h)
        now = time.time()
        t0 = now - self.window
        vis = [s for s in self.series if s[0] not in self.hidden]
        top = max([v for s in vis for t, v in s[2] if t >= t0] or [0])
        mx = nice(top * 1.08, self.binary)
        X = lambda t: L + (t - t0) / self.window * pw
        Y = lambda v: T + ph - v / mx * ph
        cr.set_font_size(10.5)
        cr.select_font_face("monospace")
        for i in range(5):
            v = mx * i / 4
            y = round(Y(v)) + .5
            cr.set_source_rgba(fg.red, fg.green, fg.blue, .12 if i else .25)
            cr.set_line_width(1)
            if i:
                cr.set_dash([2, 3])
            cr.move_to(L, y)
            cr.line_to(w - R, y)
            cr.stroke()
            cr.set_dash([])
            cr.set_source_rgba(fg.red, fg.green, fg.blue, .55)
            txt = self.fmt(v)
            ext = cr.text_extents(txt)
            cr.move_to(L - 8 - ext.width, y + 3.5)
            cr.show_text(txt)
        for k in range(6):
            t = t0 + self.window * k / 5
            txt = self._ago(self.window - self.window * k / 5)
            ext = cr.text_extents(txt)
            x = min(w - R - ext.width, max(L, X(t) - ext.width / 2))
            cr.move_to(x, h - 5)
            cr.show_text(txt)
        for name, color, pts in vis:
            pts = [p for p in pts if p[0] >= t0]
            if len(pts) < 2:
                continue
            r, g, b, _ = rgb(color)
            cr.move_to(X(pts[0][0]), Y(pts[0][1]))
            for t, v in pts[1:]:
                cr.line_to(X(t), Y(v))
            if self.fill and len(vis) == 1:
                path = cr.copy_path()
                cr.line_to(X(pts[-1][0]), Y(0))
                cr.line_to(X(pts[0][0]), Y(0))
                cr.close_path()
                cr.set_source_rgba(r, g, b, .14)
                cr.fill()
                cr.append_path(path)
            cr.set_source_rgba(r, g, b, .55 if name in self.dashed else 1)
            cr.set_line_width(1.6)
            cr.set_line_join(1)
            if name in self.dashed:
                cr.set_dash([5, 4])
            cr.stroke()
            cr.set_dash([])
        for t, _text, colour in self.markers:              # events: a thin line and a dot at the top
            if t < t0:
                continue
            x = round(X(t)) + .5
            r, g, b, _ = rgb(colour)
            cr.set_source_rgba(r, g, b, .55)
            cr.set_line_width(1)
            cr.set_dash([2, 2])
            cr.move_to(x, T)
            cr.line_to(x, T + ph)
            cr.stroke()
            cr.set_dash([])
            cr.set_source_rgba(r, g, b, .95)
            cr.arc(x, T + 3, 3, 0, 6.2832)
            cr.fill()
        if not any(len(s[2]) > 1 for s in vis):
            cr.set_source_rgba(fg.red, fg.green, fg.blue, .45)
            cr.select_font_face("sans")
            cr.set_font_size(12)
            msg = "Collecting data…"
            ext = cr.text_extents(msg)
            cr.move_to(L + (pw - ext.width) / 2, T + ph / 2)
            cr.show_text(msg)

    def _tooltip(self, _w, x, _y, _kb, tip):
        w = self.get_width()
        L, R, T, B, pw, ph = self._geom(w, self.get_height())
        if x < L or not self.series:
            return False
        t = time.time() - self.window + (x - L) / pw * self.window
        near = max(10, self.window / 300)                  # one minute per point in long ranges
        rows = []
        stamp = None
        for name, color, pts in self.series:
            if name in self.hidden or not pts:
                continue
            p = min(pts, key=lambda q: abs(q[0] - t))
            if abs(p[0] - t) > near:
                continue
            stamp = p[0]
            rows.append((p[1], name, color))
        evs = [m for m in self.markers if abs(m[0] - t) <= max(near, self.window / pw * 4)]
        if not rows and not evs:
            return False
        rows.sort(reverse=True)
        fmt = "%H:%M:%S" if self.window <= 3600 else "%a %H:%M"
        head = time.strftime(fmt, time.localtime(stamp or t))
        lines = [f"<span foreground='{c}'>●</span> {esc(n)}  <tt>{esc(self.fmt(v))}</tt>" for v, n, c in rows[:10]]
        lines += [f"<span foreground='{c}'>▲</span> {esc(txt)}" for _t, txt, c in evs[:6]]
        if self.on_click:
            lines.append("<small>Click to see the logs from this moment</small>")
        tip.set_markup(f"<b>{head}</b>\n" + "\n".join(lines))
        return True


class Sparkline(Gtk.DrawingArea):
    def __init__(self, width=72, height=20):
        super().__init__(content_width=width, content_height=height, valign=Gtk.Align.CENTER)
        self.values = []
        self.set_draw_func(self._draw)

    def set_values(self, values):
        self.values = values[-60:]
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        v = self.values
        if len(v) < 2:
            return
        hi, lo = max(v), min(v)
        span = (hi - lo) or hi or 1
        r, g, b, _ = rgb(palette()[0])
        cr.set_source_rgba(r, g, b, 1)
        cr.set_line_width(1.3)
        n = max(len(v) - 1, 1)
        for i, x in enumerate(v):
            px, py = i / n * w, h - 2 - (x - lo) / span * (h - 4)
            cr.line_to(px, py) if i else cr.move_to(px, py)
        cr.stroke()


def legend(chart, names_colors):
    """A row of toggle chips that hide/show series on the chart."""
    box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=30, row_spacing=0, column_spacing=2,
                      homogeneous=False, halign=Gtk.Align.START)
    box.add_css_class("legend")
    for name, color in names_colors:
        b = Gtk.ToggleButton(active=name not in chart.hidden)
        b.add_css_class("flat")
        b.add_css_class("legend-chip")
        lb = Gtk.Label()
        lb.set_markup(f"<span foreground='{color}'>━</span> {esc(name)}")
        b.set_child(lb)

        def toggled(btn, n=name):
            (chart.hidden.discard if btn.get_active() else chart.hidden.add)(n)
            chart.queue_draw()
        b.connect("toggled", toggled)
        box.append(b)
    return box
