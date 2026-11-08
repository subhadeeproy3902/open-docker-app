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
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)

    def set_data(self, series, window=None):
        self.series = series
        if window:
            self.window = window
        self.queue_draw()

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
            txt = "now" if k == 5 else f"−{round((self.window - self.window * k / 5) / 60)}m"
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
            cr.set_source_rgba(r, g, b, 1)
            cr.set_line_width(1.6)
            cr.set_line_join(1)
            cr.stroke()
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
        rows = []
        stamp = None
        for name, color, pts in self.series:
            if name in self.hidden or not pts:
                continue
            p = min(pts, key=lambda q: abs(q[0] - t))
            if abs(p[0] - t) > 10:
                continue
            stamp = p[0]
            rows.append((p[1], name, color))
        if not rows:
            return False
        rows.sort(reverse=True)
        head = time.strftime("%H:%M:%S", time.localtime(stamp))
        tip.set_markup(f"<b>{head}</b>\n" + "\n".join(
            f"<span foreground='{c}'>●</span> {esc(n)}  <tt>{esc(self.fmt(v))}</tt>" for v, n, c in rows[:10]))
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
