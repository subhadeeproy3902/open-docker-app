"""Small shared helpers: background work, formatting, settings, dialogs."""
import json
import os
import weakref
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from gi.repository import Adw, Gio, GLib, GObject, Gtk

APP_ID = "io.github.subhadeeproy3902.Berth"
VERSION = "1.3.0"
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


# ── diagnostics: every error lands in ~/.cache/berth/berth.log (≈2 MB max), so problems can be reported ──
LOG_PATH = os.path.join(GLib.get_user_cache_dir(), "berth", "berth.log")
_log_lock = threading.Lock()


def log_error(where, exc=None, tb=None):
    import traceback
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        text = tb or ("".join(traceback.format_exception(exc)) if isinstance(exc, BaseException) else str(exc or ""))
        with _log_lock, open(LOG_PATH, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{where}] {text.rstrip()}\n")
    except OSError:
        pass


def install_error_log():
    """Uncaught errors (including those inside GTK callbacks) go to the log as well as stderr."""
    import traceback
    prev = sys.excepthook

    def hook(t, v, tb):
        log_error("uncaught", tb="".join(traceback.format_exception(t, v, tb)))
        prev(t, v, tb)
    sys.excepthook = hook


def diagnostic_report(store=None):
    import platform
    lines = [f"Berth {VERSION}", f"Python {platform.python_version()} · {platform.platform()}",
             f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()} · "
             f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()} · "
             f"renderer {os.environ.get('GSK_RENDERER', 'default')}"]
    if store is not None:
        info = store.info or {}
        lines.append(f"Engine {store.engine.path} · up={store.up} · Docker {info.get('ServerVersion', '?')} · "
                     f"{len(store.containers)} containers")
    try:
        with open("/proc/self/status") as f:
            lines += [ln.strip() for ln in f if ln.startswith(("VmRSS", "VmHWM"))]
    except OSError:
        pass
    lines.append("--- last log lines ---")
    try:
        with open(LOG_PATH) as f:
            lines += f.read().splitlines()[-150:]
    except OSError:
        lines.append("(no errors logged)")
    return "\n".join(lines)


def bg(fn, *args, done=None, error=None):
    """Run fn(*args) on a worker; call done(result) / error(exc) back on the UI thread. Every failure is logged."""
    def finish(ok, value):
        cb = done if ok else error
        try:
            if cb:
                cb(value)
            elif not ok:
                print("berth:", value)
        except Exception as e:  # noqa: BLE001
            log_error(f"callback of {getattr(fn, '__qualname__', fn)}", e)
            raise
        return False

    def run():
        try:
            value, ok = fn(*args), True
        except Exception as e:  # noqa: BLE001 — every failure goes back to the UI as a message
            value, ok = e, False
            log_error(getattr(fn, "__qualname__", str(fn)), e)
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
    """Docker's status in a few characters for a table cell: "Up 2 d", "Exited 137 · 3 h", "Unhealthy · 4 min".
    The full text goes in the tooltip (status_tooltip)."""
    import re
    s = re.sub(r"About an? ", "1 ", s.strip())
    s = s.replace("Less than a second", "0 s")
    for a, b in ((" seconds", " s"), (" second", " s"), (" minutes", " min"), (" minute", " min"),
                 (" hours", " h"), (" hour", " h"), (" days", " d"), (" day", " d"),
                 (" weeks", " wk"), (" week", " wk"), (" months", " mo"), (" month", " mo"), (" years", " y"),
                 (" year", " y")):
        s = s.replace(a, b)
    m = re.match(r"^Exited \((-?\d+)\) (.*?)(?: ago)?$", s)
    if m:
        return f"Exited {m.group(1)} · {m.group(2)}"
    if s.startswith("Restarting"):
        return "Restarting"
    m = re.match(r"^Up (.*?)(?: \((healthy|unhealthy|health: starting|Paused)\))?$", s)
    if m:
        up, h = m.group(1), m.group(2)
        return {"unhealthy": f"Unhealthy · {up}", "health: starting": f"Starting · {up}",
                "Paused": f"Paused · {up}", "healthy": f"Up {up} · healthy"}.get(h, f"Up {up}")
    return s


EXIT_CODES = {0: "finished normally", 1: "the application stopped with an error",
              2: "wrong arguments or a shell error", 125: "Docker couldn't start it",
              126: "the command can't be run (permissions?)", 127: "the command was not found",
              130: "stopped with Ctrl+C (SIGINT)", 134: "aborted (SIGABRT)",
              137: "killed (SIGKILL) — out of memory, or it didn't stop in time",
              139: "crashed: segmentation fault (SIGSEGV)", 143: "stopped normally (SIGTERM, e.g. docker stop)",
              255: "exit status out of range"}


def exit_code(status):
    import re
    m = re.match(r"^Exited \((-?\d+)\)", status or "")
    return int(m.group(1)) if m else None


def status_tooltip(status):
    """The full Docker status, plus what an exit code means."""
    code = exit_code(status)
    if code is None:
        return status or None
    meaning = EXIT_CODES.get(code) or (f"killed by signal {code - 128}" if 128 < code < 160 else "set by the application")
    return f"{status}\nExit code {code}: {meaning}"


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
                "large_icons": False, "db_history": {}, "routes": [], "proxy_ports": [80, 443],
                "send_port": 8787, "backups": [], "notify": True, "update_check": True, "update_skip": "", "stacks": [],
                "safe_update": False, "alert_cpu": 0, "alert_mem": 0, "alert_disk": 0,
                "scan_schedule": "off", "scan_last": 0, "registries": [], "log_queries": {}, "db_saved": {}, "db_external": [], "mcp_actions": False,
                "ai_idle_stop": 30}

    def __init__(self):
        self.path = os.path.join(GLib.get_user_config_dir(), "berth", "settings.json")
        self.data = dict(self.DEFAULTS)
        existed = False
        try:
            with open(self.path) as f:
                self.data.update(json.load(f))
            existed = True
        except (OSError, ValueError):
            pass
        self.data.setdefault("onboarded", existed)      # first run shows the welcome; upgrades don't

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
    if ellipsize and not kw.get("wrap"):              # wrapped text must never be cut short
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
        from gi.repository import Pango
        self._t = Gtk.Label(label=title, xalign=0, css_classes=["page-title"], ellipsize=Pango.EllipsizeMode.END)
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


def table_card(child, fit=False):
    """A table inside a rounded card with page margins (instead of running edge to edge).
    fit: the card is only as tall as its rows (and scrolls once they reach the bottom of the page) — for lists,
    so a short list doesn't sit in a tall empty box."""
    sw = child if isinstance(child, Gtk.ScrolledWindow) else Gtk.ScrolledWindow(child=child)
    sw.set_vexpand(not fit)
    if fit:
        sw.set_propagate_natural_height(True)
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["table-card"], vexpand=not fit,
                   valign=Gtk.Align.START if fit else Gtk.Align.FILL, overflow=Gtk.Overflow.HIDDEN)
    card.append(sw)
    return card


def toolbar_row(*widgets):
    """The row under a page's header: search first, then filters. Same spacing on every page.
    It wraps onto a second line when the window is narrow or the font is large, instead of overflowing."""
    row = Adw.WrapBox(child_spacing=8, line_spacing=8, css_classes=["page-toolbar"])
    for w in widgets:
        if w is not None and not (isinstance(w, Gtk.Box) and w.get_first_child() is None and w.get_hexpand()):
            row.append(w)                 # (empty hexpand spacers mean nothing in a wrapping row)
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
    page.connect("shown", lambda p: GLib.idle_add(ensure_tooltips, nav))
    root = nav.find_page("list")
    if root is None:
        nav.push(page)
    else:
        nav.replace([root, page])


def ordering(a, b):
    return Gtk.Ordering.SMALLER if a < b else Gtk.Ordering.LARGER if a > b else Gtk.Ordering.EQUAL


def release_on_leave(page, cleanup):
    """Run cleanup() once a navigation page has really left the window (popped or replaced), not when it is merely
    covered by another page. GTK4 never "destroy"s a page that a store signal still references, so pages that
    subscribe to the store must disconnect here, or every page ever opened stays in memory (with its logs)."""
    state = {"done": False}

    def check():
        if not state["done"] and page.get_root() is None:
            state["done"] = True
            try:
                cleanup()
            except Exception as e:  # noqa: BLE001 — cleanup must never break navigation
                log_error("cleanup", e)
        return False
    page.connect("hidden", lambda *_: GLib.timeout_add(400, check))


def per_size(n, sm, md, lg, xl, xxl, xxxl):
    """Pick a value for the window's size class (see app.SIZES)."""
    return {"sm": sm, "md": md, "lg": lg, "xl": xl, "2xl": xxl, "3xl": xxxl}.get(n, lg)


def set_columns(flowbox, n):
    """Exactly n columns (min = max): the size class already guarantees the room, and a free FlowBox would wrap
    unevenly (3 + 1)."""
    flowbox.set_max_children_per_line(max(n, flowbox.get_min_children_per_line()))
    flowbox.set_min_children_per_line(n)
    flowbox.set_max_children_per_line(n)


WIDE = weakref.WeakSet()


class WidePrefs(Gtk.ScrolledWindow):
    """Like Adw.PreferencesPage, but it uses big screens: a wider column that grows with the size class, and two
    columns of groups on 2xl / 3xl instead of one narrow strip in the middle."""

    def __init__(self, two_at=("2xl", "3xl")):
        """two_at: the size classes that get two columns (detail pages use them from lg up)."""
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.groups, self.two_at = [], two_at
        self.clamp = Adw.Clamp(maximum_size=720, tightening_threshold=600)
        self.cols = Gtk.Box(spacing=32, homogeneous=True, css_classes=["wide-prefs"])
        self.clamp.set_child(self.cols)
        self.set_child(self.clamp)
        self.n = 0
        self.want = 1
        self._layout(1)
        WIDE.add(self)                       # the window tells every WidePrefs when its size class changes
        self.connect("map", lambda *_: self.on_size(getattr(self.get_root(), "size_class", "md") or "md"))

    def add(self, group):
        self.groups.append(group)
        self._layout(self.want, force=True)

    def remove(self, group):
        if group in self.groups:
            self.groups.remove(group)
        if group.get_parent():
            group.get_parent().remove(group)
        self._layout(self.want, force=True)

    def on_size(self, n):
        two = n in self.two_at
        self.clamp.set_maximum_size(per_size(n, 720, 760, 820, 900, 1480, 1760) if not two or n in ("2xl", "3xl")
                                    else per_size(n, 720, 760, 1180, 1400, 1480, 1760))
        self.want = 2 if two else 1
        self._layout(self.want)

    def _layout(self, n, force=False):
        n = max(1, min(n, len(self.groups)))          # one group: one column, never an empty half
        if n == self.n and not force:
            return
        self.n = n
        for g in self.groups:
            if g.get_parent():
                g.get_parent().remove(g)
        while (c := self.cols.get_first_child()):
            self.cols.remove(c)
        boxes = [Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, valign=Gtk.Align.START) for _ in range(n)]
        for b in boxes:
            self.cols.append(b)
        # balance by rough height: fill the shorter column
        heights = [0] * n
        for g in self.groups:
            i = heights.index(min(heights))
            boxes[i].append(g)
            heights[i] += 1 + sum(1 for _ in _iter_rows(g))


def _iter_rows(group):
    w = group.get_first_child()
    stack = [w] if w else []
    while stack:
        x = stack.pop()
        if isinstance(x, Adw.PreferencesRow):
            yield x
            continue
        c = x.get_first_child()
        while c:
            stack.append(c)
            c = c.get_next_sibling()


# Buttons drawn by GTK / libadwaita themselves (back arrows, dialog close buttons…) come without a tooltip.
# Every icon-only button must say what it does, so these get one when their screen opens.
STOCK_TIPS = {"go-previous-symbolic": "Back", "window-close-symbolic": "Close", "open-menu-symbolic": "Main menu",
              "view-more-symbolic": "More actions", "edit-find-symbolic": "Search", "go-next-symbolic": "Next",
              "pan-down-symbolic": "Show options", "list-add-symbolic": "Add", "user-trash-symbolic": "Delete",
              "view-refresh-symbolic": "Reload", "edit-copy-symbolic": "Copy", "document-edit-symbolic": "Edit",
              "adw-external-link-symbolic": "Open", "go-up-symbolic": "Up one folder", "edit-clear-symbolic": "Clear"}


def ensure_tooltips(root):
    """Give every icon-only button under root a tooltip if it has none (run when a page or dialog opens)."""
    stack = [root]
    while stack:
        w = stack.pop()
        if isinstance(w, (Gtk.Button, Gtk.MenuButton)) and not w.get_tooltip_text() and not w.get_label():
            ch = w.get_child() if hasattr(w, "get_child") else None
            icon = (w.get_icon_name() if hasattr(w, "get_icon_name") else None) or \
                (ch.get_icon_name() if isinstance(ch, Gtk.Image) else None)
            if icon in STOCK_TIPS:
                target = w.get_parent() if isinstance(w.get_parent(), Gtk.MenuButton) else w
                if not target.get_tooltip_text():
                    target.set_tooltip_text(STOCK_TIPS[icon])
        c = w.get_first_child()
        while c:
            stack.append(c)
            c = c.get_next_sibling()
    return False


def follow_content(dialog, width):
    """Let a dialog grow and shrink with its content while it is open (results that arrive later, sections that
    appear). Without this an Adw.Dialog keeps the size it had when it opened. The width stays fixed."""
    dialog.set_follows_content_size(True)
    child = dialog.get_child()
    if child is not None:
        child.set_size_request(width, -1)
