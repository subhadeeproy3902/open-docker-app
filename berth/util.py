"""Small shared helpers: background work, formatting, settings, dialogs."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from gi.repository import Adw, Gio, GLib, GObject, Gtk

APP_ID = "io.github.subhadeeproy3902.Berth"
VERSION = "1.1.1"
ICON = os.environ.get("BERTH_ICON") or APP_ID     # icon name; `make install ICON=docker-desktop` swaps it


def themed(*names):
    """First icon name the current icon theme actually has (falls back to the last one)."""
    from gi.repository import Gdk
    disp = Gdk.Display.get_default()
    if disp is None:
        return names[-1]
    theme = Gtk.IconTheme.get_for_display(disp)
    return next((n for n in names if theme.has_icon(n)), names[-1])


def app_icon():
    return themed(ICON, APP_ID)

# ── background work: never block the UI thread ──
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="berth")


def bg(fn, *args, done=None, error=None):
    """Run fn(*args) on a worker; call done(result) / error(exc) back on the UI thread."""
    def finish(ok, value):
        cb = done if ok else error
        if cb:
            cb(value)
        elif not ok:
            print("berth:", value)
        return False

    def run():
        try:
            value, ok = fn(*args), True
        except Exception as e:  # noqa: BLE001 — every failure goes back to the UI as a message
            value, ok = e, False
        GLib.idle_add(finish, ok, value)

    _POOL.submit(run)


def thread(fn, *args):
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
    return t


# ── formatting ──
def fmt_bytes(b, digits=1):
    if b is None or b < 0:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB")
    i, b = 0, float(b)
    while b >= 1024 and i < len(units) - 1:
        b /= 1024
        i += 1
    if i == 0:
        return f"{int(b)} B"
    return f"{b:.0f} {units[i]}" if b >= 100 else f"{b:.{digits}f} {units[i]}"


def fmt_rate(b):
    return fmt_bytes(b) + "/s"


def fmt_pct(v):
    if v is None:
        return "—"
    return f"{v:.0f}%" if v >= 100 else f"{v:.1f}%"


def ago(ts):
    d = time.time() - ts
    for unit, sec in (("y", 31536000), ("mo", 2592000), ("d", 86400), ("h", 3600), ("min", 60)):
        if d >= sec:
            return f"{int(d // sec)} {unit} ago"
    return "just now"


def short_status(s):
    import re
    s = re.sub(r"^Up ", "Up ", s)
    s = re.sub(r"About an? ", "1 ", s)
    s = s.replace("Less than a second", "0 s")
    for a, b in ((" seconds", " s"), (" second", " s"), (" minutes", " min"), (" minute", " min"),
                 (" hours", " h"), (" hour", " h"), (" days", " d"), (" day", " d"),
                 (" weeks", " wk"), (" week", " wk"), (" months", " mo"), (" month", " mo")):
        s = s.replace(a, b)
    s = s.replace(" (healthy)", " · healthy").replace(" (unhealthy)", " · unhealthy")
    s = s.replace(" (health: starting)", " · starting").replace(" (Paused)", " · paused")
    m = re.match(r"^Exited \((\d+)\) (.*) ago$", s)
    return f"Exited ({m.group(1)}) · {m.group(2)} ago" if m else s


def state_of(c):
    st, status = c.get("State", ""), c.get("Status", "")
    if st == "running":
        if "(unhealthy)" in status:
            return "unhealthy"
        if "health: starting" in status:
            return "starting"
        return "running"
    return {"paused": "paused", "restarting": "restarting", "dead": "dead", "created": "created"}.get(st, "exited")


def cname(c):
    return (c.get("Names") or ["/?"])[0].lstrip("/")


def labels(c):
    return c.get("Labels") or {}


# ── settings (~/.config/berth/settings.json) ──
class Settings:
    DEFAULTS = {"theme": "system", "saver": True, "interval": 2, "window": [1240, 800], "expanded": {},
                "software": False, "range": 600, "page": "containers", "tray": False, "engine": "",
                "text_scale": 100, "density": "comfortable", "strong_borders": False, "reduce_motion": False,
                "large_icons": False}

    def __init__(self):
        self.path = os.path.join(GLib.get_user_config_dir(), "berth", "settings.json")
        self.data = dict(self.DEFAULTS)
        try:
            with open(self.path) as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def __getitem__(self, k):
        return self.data.get(k, self.DEFAULTS.get(k))

    def __setitem__(self, k, v):
        self.data[k] = v
        self.save()

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass


# ── list items: one GObject per row, with signals the bound cells listen to ──
class Row(GObject.Object):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "stats": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, kind, key, data=None):
        super().__init__()
        self.kind, self.key, self.data = kind, key, data or {}
        self.children = None      # Gio.ListStore for group rows
        self.extra = {}


# ── desktop integration ──
def open_uri(uri):
    try:
        Gio.AppInfo.launch_default_for_uri(uri, None)
    except GLib.Error as e:
        print("berth: cannot open", uri, e)


# ── platform layer ──
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"
IN_FLATPAK = os.path.exists("/.flatpak-info")


def host(argv):
    """Commands like docker / kubectl / pkexec live on the host; inside a Flatpak they are reached through flatpak-spawn."""
    argv = list(argv)
    return ["flatpak-spawn", "--host"] + argv if IN_FLATPAK else argv


def host_which(name):
    if not IN_FLATPAK:
        return shutil.which(name)
    try:
        r = subprocess.run(host(["sh", "-c", f"command -v {name}"]), capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _terminal_prefix():
    env = os.environ.get("TERMINAL")
    cands = ([env] if env else []) + ["ghostty", "kgx", "gnome-terminal", "konsole", "kitty", "alacritty",
                                     "foot", "wezterm", "xfce4-terminal", "tilix", "xterm"]
    for t in cands:
        path = host_which(t)
        if not path:
            continue
        name = os.path.basename(t)
        if name in ("gnome-terminal", "kgx", "tilix"):
            return [path, "--"]
        if name == "wezterm":
            return [path, "start", "--"]
        if name in ("kitty", "foot"):
            return [path]
        return [path, "-e"]
    return None


def run_in_terminal(argv):
    """Open the user's terminal running argv. Returns False when no terminal could be found."""
    import shlex
    if IS_MAC:
        script = shlex.join(argv).replace("\\", "\\\\").replace('"', '\\"')
        spawn(["osascript", "-e", f'tell application "Terminal" to do script "{script}"', "-e",
               'tell application "Terminal" to activate'])
        return True
    if IS_WIN:
        spawn(["cmd", "/c", "start", "cmd", "/k", subprocess.list2cmdline(argv)])
        return True
    t = _terminal_prefix()
    if not t:
        return False
    spawn(t + list(argv))
    return True


def terminal_argv():          # kept for older call sites
    return _terminal_prefix()


def spawn(argv):
    kw = {} if IS_WIN else {"start_new_session": True}
    subprocess.Popen(host(argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


# ── dialogs ──
def confirm(parent, heading, body, ok_label, on_ok, destructive=True, typed=None):
    """Adw alert; with typed='name' the OK button only enables once that text is typed."""
    d = Adw.AlertDialog(heading=heading, body=body)
    d.set_body_use_markup(True)
    d.add_response("cancel", "Cancel")
    d.add_response("ok", ok_label)
    d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("cancel" if destructive else "ok")
    d.set_close_response("cancel")
    if typed:
        e = Gtk.Entry(placeholder_text=typed)
        e.add_css_class("monospace")
        d.set_extra_child(e)
        d.set_response_enabled("ok", False)
        e.connect("changed", lambda w: d.set_response_enabled("ok", w.get_text() == typed))
    d.connect("response", lambda _d, r: r == "ok" and on_ok())
    d.present(parent)


def esc(s):
    return GLib.markup_escape_text(str(s if s is not None else ""))


def label(text="", css=(), xalign=0.0, ellipsize=True, **kw):
    lb = Gtk.Label(label=text, xalign=xalign, **kw)
    if ellipsize:
        from gi.repository import Pango
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    for c in css:
        lb.add_css_class(c)
    return lb


def icon_button(icon, tooltip, cb=None, css=("flat",)):
    b = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    for c in css:
        b.add_css_class(c)
    if cb:
        b.connect("clicked", lambda *_: cb())
    return b


class PageHeader(Gtk.Box):
    """Large page title + live summary line on the left, the page's main actions on the right.
    Drop-in for Adw.WindowTitle (set_title / set_subtitle / get_subtitle)."""

    def __init__(self, title, subtitle=""):
        super().__init__(spacing=16, css_classes=["page-header"])
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self._t = Gtk.Label(label=title, xalign=0, css_classes=["page-title"])
        from gi.repository import Pango
        self._s = Gtk.Label(label=subtitle, xalign=0, css_classes=["page-subtitle"], ellipsize=Pango.EllipsizeMode.END,
                            visible=bool(subtitle))
        text.append(self._t)
        text.append(self._s)
        self.append(text)
        self.actions = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self.append(self.actions)

    def add(self, widget):
        self.actions.append(widget)
        return widget

    def set_title(self, t):
        self._t.set_text(t)

    def set_subtitle(self, t):
        self._s.set_text(t or "")
        self._s.set_visible(bool(t))

    def get_subtitle(self):
        return self._s.get_text()


def flat_page(tv, header, toolbar=None):
    """The standard page frame: a flat header bar (window controls only), the page header, an optional toolbar."""
    hb = Adw.HeaderBar(show_title=False)
    tv.set_top_bar_style(Adw.ToolbarStyle.FLAT)
    tv.add_top_bar(hb)
    tv.add_top_bar(header)
    if toolbar is not None:
        tv.add_top_bar(toolbar)
    return hb


def table_card(child):
    """A table inside a rounded card with page margins (instead of running edge to edge)."""
    sw = child if isinstance(child, Gtk.ScrolledWindow) else Gtk.ScrolledWindow(child=child)
    sw.set_vexpand(True)
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["table-card"], vexpand=True, overflow=Gtk.Overflow.HIDDEN)
    card.append(sw)
    return card


def toolbar_row(*widgets):
    """The row under a page's header: search first, then filters. Same spacing on every page."""
    row = Gtk.Box(spacing=8, css_classes=["page-toolbar"])
    for w in widgets:
        if w is not None:
            row.append(w)
    return row


def factory(setup, bind, unbind=None):
    f = Gtk.SignalListItemFactory()
    f.connect("setup", lambda _f, li: li.set_child(setup()))
    f.connect("bind", lambda _f, li: bind(li.get_child(), li.get_item(), li))
    if unbind:
        f.connect("unbind", lambda _f, li: unbind(li.get_child(), li.get_item(), li))
    return f


def connect_row(widget, row, signal, fn):
    """Bind-time signal hookup that is undone automatically on rebind/unbind."""
    hs = widget.__dict__.setdefault("_berth_handlers", [])
    hs.append((row, row.connect(signal, fn)))


def disconnect_rows(widget):
    for row, h in widget.__dict__.pop("_berth_handlers", []):
        try:
            row.disconnect(h)
        except TypeError:
            pass


_page_ids = iter(range(1, 10 ** 9))


def detail_tag(kind):
    """Unique navigation tag for a details page (two pages may never share a tag)."""
    return f"{kind}-{next(_page_ids)}"


def push_detail(nav, page):
    """Show a details page directly above the list, replacing any details page already open (no duplicates,
    no half-popped pages during the slide animation)."""
    root = nav.find_page("list")
    if root is None:
        nav.push(page)
    else:
        nav.replace([root, page])


def ordering(a, b):
    return Gtk.Ordering.SMALLER if a < b else Gtk.Ordering.LARGER if a > b else Gtk.Ordering.EQUAL
