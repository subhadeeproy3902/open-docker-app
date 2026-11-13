"""Berth — a light, native Docker desktop for Linux (GTK 4 + libadwaita)."""
import os
import sys

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402

# Draw with OpenGL, not GTK's default Vulkan: creating a Vulkan instance loads every GPU driver, which takes ~2 s and
# wakes a sleeping discrete GPU on hybrid laptops (measured: GL 80 ms, Vulkan 2065 ms). The renderer is only picked
# when the first window is realized, so setting it here (after importing, before any window) is in time.
# Its name changed: GTK 4.14–4.19 call it "ngl" ("gl" was the old, removed one); 4.20+ call it "gl". Anyone can override.
if "GSK_RENDERER" not in os.environ and Gtk.get_major_version() == 4 and Gtk.get_minor_version() >= 14:
    os.environ["GSK_RENDERER"] = "gl" if Gtk.get_minor_version() >= 20 else "ngl"

from .containers import ContainersPage  # noqa: E402
from .db_page import DatabasesPage  # noqa: E402
from .sharing_page import SharingPage  # noqa: E402
from .send_page import SendPage  # noqa: E402
from .backups_page import BackupsPage  # noqa: E402
from .stacks_page import StacksPage  # noqa: E402
from . import theme  # noqa: E402
from .theme import apply_accessibility  # noqa: E402
from .files import cleanup_helpers  # noqa: E402
from .k8s import K8sPage  # noqa: E402
from .logs import LogsPage  # noqa: E402
from .pages import BuildHistoryPage, BuildsPage, DiskPage, ImagesPage, NetworksPage, ResourcesPage, VolumesPage  # noqa: E402
from .settings_page import SettingsPage  # noqa: E402
from .store import Store  # noqa: E402
from .util import ensure_tooltips, open_uri, APP_ID, VERSION, app_icon, themed, Settings, fmt_bytes, fmt_pct, icon_button, label, spawn  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PAGES = [("containers", ContainersPage), ("stacks", StacksPage), ("images", ImagesPage), ("volumes", VolumesPage), ("networks", NetworksPage),
         ("backups", BackupsPage),
         ("databases", DatabasesPage), ("sharing", SharingPage), ("send", SendPage),
         ("logs", LogsPage), ("resources", ResourcesPage), ("disk", DiskPage),
         ("builds", BuildHistoryPage), ("cache", BuildsPage),
         ("kubernetes", K8sPage),
         ("settings", SettingsPage)]
# Sidebar sections: the first page of each group gets a small heading; Settings sits apart at the bottom.
GROUPS = {"containers": "Manage", "databases": "Connect", "logs": "Observe", "builds": "Build",
          "kubernetes": "Orchestrate"}
# window size classes: (name, from, to) in sp — like Tailwind's sm … 3xl
SIZES = [("sm", 0, 900), ("md", 900, 1280), ("lg", 1280, 1600), ("xl", 1600, 1920), ("2xl", 1920, 2560),
         ("3xl", 2560, None)]
THEMES = {"system": Adw.ColorScheme.DEFAULT, "light": Adw.ColorScheme.FORCE_LIGHT, "dark": Adw.ColorScheme.FORCE_DARK}


class Window(Adw.ApplicationWindow):
    def __init__(self, app, settings):
        w, h = settings["window"] or (1240, 800)
        super().__init__(application=app, title="Berth", default_width=w, default_height=h, icon_name=app_icon())
        self.settings = settings
        from . import compose as _compose
        _compose.SETTINGS = settings              # remembered stacks (Stacks page)
        self.store = Store(settings["engine"] or None)
        self.store.sampler.interval = float(settings["interval"] or 2)
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        split = Adw.NavigationSplitView(sidebar_width_fraction=0.18, min_sidebar_width=210, max_sidebar_width=260)
        self.toasts.set_child(split)

        # sidebar
        side = Adw.ToolbarView()
        sh = Adw.HeaderBar()
        brand = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, css_classes=["brand"])
        brand.append(Gtk.Image(icon_name=app_icon(), pixel_size=24))
        brand.append(Gtk.Label(label="Berth", css_classes=["brand-name"]))
        sh.set_title_widget(brand)
        menu = Gio.Menu()
        theme = Gio.Menu()
        for k, lbl in (("system", "Follow system"), ("light", "Light"), ("dark", "Dark")):
            theme.append(lbl, f"app.theme::{k}")
        menu.append_submenu("Theme", theme)
        menu.append("Settings", "app.preferences")
        menu.append("Keyboard shortcuts", "app.shortcuts")
        menu.append("Welcome tour", "app.welcome")
        menu.append("About Berth", "app.about")
        sh.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu", primary=True))
        self.theme_btn = icon_button("weather-clear-night-symbolic", "Switch light / dark", self.toggle_theme)
        sh.pack_start(self.theme_btn)
        side.add_top_bar(sh)
        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar.add_css_class("navigation-sidebar")
        self.counts = {}
        # Settings sits apart, pinned under the list: always visible, however many pages there are
        self.sidebar_end = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["navigation-sidebar"])
        for key, cls in PAGES:
            row = Gtk.ListBoxRow()
            row.key = key
            box = Gtk.Box(spacing=12, margin_top=2, margin_bottom=2)
            box.append(Gtk.Image(icon_name=themed(cls.icon, "power-profile-performance-symbolic") if "monitor" in cls.icon else cls.icon))
            box.append(label(cls.title, hexpand=True))
            cnt = label("", css=["dim-label", "caption", "numeric"], ellipsize=False)
            box.append(cnt)
            self.counts[key] = cnt
            row.set_child(box)
            (self.sidebar_end if key == "settings" else self.sidebar).append(row)
        self.sidebar.set_header_func(self._sidebar_header)
        self.sidebar.connect("row-selected", lambda _l, r: r and (self.sidebar_end.unselect_all(),
                                                                  self.go(r.key, from_sidebar=True)))
        self.sidebar_end.connect("row-selected", lambda _l, r: r and (self.sidebar.unselect_all(),
                                                                      self.go(r.key, from_sidebar=True)))
        self.sidebar.set_vexpand(True)
        sbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        find = Gtk.Button(tooltip_text="Go to anything — a container, stack, image, page or task (Ctrl+K)",
                          css_classes=["sidebar-find"], action_name="app.palette")
        fb = Gtk.Box(spacing=8)
        fb.append(Gtk.Image(icon_name="system-search-symbolic"))
        fb.append(Gtk.Label(label="Search", xalign=0, hexpand=True))
        fb.append(Gtk.Label(label="⌘K" if sys.platform == "darwin" else "Ctrl+K",
                            css_classes=["caption", "dim-label", "kbd-hint"]))
        find.set_child(fb)
        sbox.append(find)
        sbox.append(Gtk.ScrolledWindow(child=self.sidebar, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        sbox.append(Gtk.Separator(margin_start=18, margin_end=18))
        sbox.append(self.sidebar_end)
        side.set_content(sbox)
        self.engine_box = Gtk.Box(spacing=8, css_classes=["engine-card"])
        self.engine_dot = Gtk.Box(valign=Gtk.Align.CENTER, css_classes=["dot", "exited"])
        self.engine_lbl = label("Connecting…", css=["caption"])
        self.engine_btn = icon_button("system-shutdown-symbolic", "Stop Docker Engine", self.engine_toggle)
        self.engine_box.append(self.engine_dot)
        self.engine_box.append(self.engine_lbl)
        self.engine_lbl.set_hexpand(True)
        self.engine_box.append(self.engine_btn)
        side.add_bottom_bar(self.engine_box)
        split.set_sidebar(Adw.NavigationPage(title="Berth", child=side))

        # content
        # Not homogeneous: each page is only as wide as itself, so one wide page can't push the others out of the window.
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.NONE, vexpand=True,
                               hhomogeneous=False, vhomogeneous=False)
        self.pages = {}
        for key, cls in PAGES:
            p = cls(self, self.store, settings)
            self.pages[key] = p
            self.stack.add_named(p, key)
        off = Adw.ToolbarView()
        off.add_top_bar(Adw.HeaderBar())
        self.off_page = Adw.StatusPage(icon_name=app_icon(), title="Docker Engine is not running",
                                       description="Berth needs a running Docker Engine.")
        start = Gtk.Button(label="Start Docker Engine", halign=Gtk.Align.CENTER)
        start.add_css_class("pill")
        start.add_css_class("suggested-action")
        start.connect("clicked", lambda *_: self._engine_start())
        self.off_page.set_child(start)
        off.set_content(self.off_page)
        self.stack.add_named(off, "off")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.update_banner = Adw.Banner(revealed=False, button_label="What's new")
        self.update_banner.connect("button-clicked", lambda b: (open_uri(getattr(b, "url", "")),
                                                                b.set_revealed(False)))
        content.append(self.update_banner)
        content.append(self.stack)
        content.append(self._statusbar())
        split.set_content(Adw.NavigationPage(title="Berth", child=content))

        self.store.connect("engine", lambda _s, up: self._engine(up))
        self.store.connect("containers", lambda *_: self._counts())
        self.store.connect("images", lambda *_: self._counts())
        self.store.connect("volumes", lambda *_: self._counts())
        self.store.connect("stats", lambda *_: self._status_stats())
        self.store.connect("containers", lambda *_: self._tray_state())
        self.store.connect("trouble", lambda _s, cid, title, body: self.notify(title, body, key=f"ctr-{cid[:12]}"))
        self.store.connect("engine", lambda *_: self._tray_state())
        self.connect("notify::is-active", lambda *_: self.update_saver())
        self.connect("notify::suspended", lambda *_: self.update_saver())
        self.connect("close-request", self._on_close)
        # dialogs (and the pages they cover) get tooltips for their stock icon buttons as they open
        self.connect("notify::visible-dialog", lambda w, _p: w.get_visible_dialog() and
                     GLib.timeout_add(250, lambda: ensure_tooltips(w.get_visible_dialog() or w)))
        GLib.timeout_add_seconds(30, self._sweep_links)
        GLib.timeout_add_seconds(20, lambda: self._check_update() and False)
        try:                                           # charts draw with cairo through PyGObject's bridge
            gi.require_foreign("cairo")
        except (ImportError, AttributeError):
            GLib.timeout_add_seconds(4, lambda: self.toast(
                "Charts can't draw: install python3-gi-cairo (Debian, Ubuntu), python3-cairo (Fedora, Arch) or "
                "python3-gobject-cairo (openSUSE)") and False)
        GLib.timeout_add_seconds(60, lambda: self._run_backups() and False)   # catch up shortly after start
        GLib.timeout_add_seconds(600, self._run_backups)
        self._watch_busy, self._last_img_check = set(), 0
        GLib.timeout_add_seconds(60, self._watch)                              # alerts, update check, scheduled scans
        # Responsive layout: one size class at a time (sm … 3xl). Each class is a breakpoint whose "apply" tells every
        # page; pages pick their own column counts, and style.css scales spacing and type per class. The ranges are in
        # sp, so a larger text size behaves like a smaller screen (accessibility first).
        self.size_class = None
        for name, lo, hi in SIZES:
            cond = " and ".join(c for c in (f"min-width: {lo}sp" if lo else "", f"max-width: {hi - 1}sp" if hi else "")
                                if c)
            bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(cond))
            bp.connect("apply", lambda _b, n=name: self.set_size_class(n))
            self.add_breakpoint(bp)
        self.set_size_request(760, 520)
        apply_accessibility(self, settings)
        self.page = settings["page"] if settings["page"] in self.pages else "containers"
        self.select_row(self.page)
        self.stack.set_visible_child_name("off")
        self._sync_theme_btn()
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._sync_theme_btn())

    def set_size_class(self, n):
        if n == self.size_class:
            return
        for name, _lo, _hi in SIZES:
            self.remove_css_class(f"size-{name}")
        self.add_css_class(f"size-{n}")
        self.size_class = n
        wide = n not in ("sm", "md")                     # the least important table columns step aside below lg
        for col in (self.pages["containers"].col_image, self.pages["containers"].col_ports,
                    self.pages["images"].col_id, self.pages["images"].c_created):
            col.set_visible(wide)
        for p in self.pages.values():
            if hasattr(p, "on_size"):
                p.on_size(n)
        from .util import WIDE
        for w in list(WIDE):                       # detail pages and dialogs with wide forms
            w.on_size(n)

    def _sidebar_header(self, row, before):
        title = GROUPS.get(row.key)
        if title:
            row.set_header(label(title.upper(), css=["sidebar-heading"], margin_top=4 if before is None else 14,
                                 margin_bottom=2, margin_start=12))
        else:
            row.set_header(None)

    # ── status bar ──
    def _statusbar(self):
        bar = Gtk.Box(spacing=0, css_classes=["statusbar"])
        self.sb_engine = label("", css=["caption"], ellipsize=False)
        self.sb_cpu = label("", css=["caption", "numeric"], ellipsize=False)
        self.sb_mem = label("", css=["caption", "numeric"], ellipsize=False)
        self.sb_disk = label("", css=["caption", "numeric"], ellipsize=False)
        self.sb_saver = label("", css=["caption", "dim-label"], hexpand=True, xalign=1.0)
        bar.append(self.sb_engine)
        for w in (self.sb_cpu, self.sb_mem, self.sb_disk):
            seg = Gtk.Box(visible=False)          # separator + value, hidden while the value is empty (engine off)
            seg.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=12, margin_end=12,
                                     css_classes=["statusbar-sep"]))
            seg.append(w)
            w.connect("notify::label", lambda lb, _p, sg=seg: sg.set_visible(bool(lb.get_label())))
            bar.append(seg)
        self.sb_bad = Gtk.Button(visible=False, valign=Gtk.Align.CENTER, tooltip_text="Show the unhealthy containers")
        self.sb_bad.add_css_class("flat")
        self.sb_bad.add_css_class("statusbar-alert")
        self.sb_bad.connect("clicked", lambda *_: (self.go("containers"), self.pages["containers"].show_unhealthy()))
        bar.append(self.sb_bad)
        self.sb_share = Gtk.Button(visible=False, valign=Gtk.Align.CENTER, tooltip_text="Containers shared on the "
                                   "internet — click to see or stop them")
        self.sb_share.add_css_class("flat")
        self.sb_share.add_css_class("statusbar-share")
        self.sb_share.connect("clicked", lambda *_: self.go("sharing"))
        bar.append(self.sb_share)
        bar.append(self.sb_saver)
        return bar

    def _status_stats(self):
        h = self.store.sampler.host_points()
        if h:
            _t, hcpu, used, total, dcpu, dmem = h[-1]
            self.sb_cpu.set_text(f"Containers CPU {fmt_pct(dcpu)}")
            self.sb_cpu.set_tooltip_text(f"All containers together use {fmt_pct(dcpu)} of the computer's CPU.\n"
                                         f"The whole computer is at {fmt_pct(hcpu)}.")
            self.sb_mem.set_text(f"RAM {fmt_bytes(dmem)}")
            self.sb_mem.set_tooltip_text(f"Containers use {fmt_bytes(dmem)}.\n"
                                         f"The computer uses {fmt_bytes(used)} of {fmt_bytes(total, 0)}.")

    def _disk_text(self):
        root = self.store.info.get("DockerRootDir")
        local = self.store.engine.path.startswith(("/", "unix://", "npipe://"))   # a remote engine's disk isn't here
        try:
            import shutil
            du = shutil.disk_usage(root) if root and local else None              # works on Linux, macOS, Windows
        except (OSError, ValueError):
            du = None
        self.sb_disk.set_text(f"Docker disk {fmt_bytes(du.free)} free" if du else "")
        self.sb_disk.set_tooltip_text(f"Free space where Docker keeps its data ({root})" if du else None)
        return True

    # ── engine ──
    def _engine(self, up):
        self.engine_dot.set_css_classes(["dot", "running" if up else "exited"])
        self.engine_lbl.set_text("Engine running" if up else "Engine stopped")
        self.engine_btn.set_icon_name("system-shutdown-symbolic")
        self.engine_btn.set_tooltip_text("Stop Docker Engine" if up else "Start Docker Engine")
        self.sb_engine.set_text("● Engine running" if up else "● Engine stopped")
        self.sb_engine.set_tooltip_text(self.store.engine.path)
        self.sb_engine.set_css_classes(["caption", "ok" if up else "bad"])
        self.stack.set_visible_child_name(self.page if up else "off")
        if up:
            from .util import bg
            bg(cleanup_helpers, self.store.engine, error=lambda e: None)
            GLib.timeout_add(800, lambda: (self._disk_text(), self.pages[self.page].on_show() if hasattr(self.pages[self.page], "on_show") else None) and False)
            GLib.timeout_add_seconds(60, self._disk_text)
        self.update_saver()

    def _systemctl(self, verb):
        if sys.platform != "linux":
            self.toast("Start or stop Docker from Docker Desktop, Colima, OrbStack or Rancher Desktop")
            return
        path = self.store.engine.path
        if path.startswith("tcp://"):
            self.toast("This is a remote engine — start or stop it on that machine")
            return
        unit = "podman.socket" if "podman" in path else "docker"
        if path.startswith(GLib.get_user_runtime_dir()):
            spawn(["systemctl", "--user", verb, unit])
        elif "podman" in path:
            spawn(["pkexec", "systemctl", verb, "podman.socket", "podman.service"])
        elif verb == "start":
            spawn(["pkexec", "systemctl", "start", "docker.service"])
        else:
            spawn(["pkexec", "systemctl", "stop", "docker.socket", "docker.service"])

    def _engine_start(self):
        self.toast("Starting Docker Engine — your system may ask for your password")
        self._systemctl("start")

    def engine_toggle(self):
        if not self.store.up:
            return self._engine_start()
        from .util import confirm
        confirm(self, "Stop Docker Engine?", "Every running container stops. Your system asks for your password.",
                "Stop Engine", lambda: (self._systemctl("stop"), self.toast("Stopping Docker Engine…")))

    def _sweep_links(self):
        """Remove public links whose expiry time has passed. No engine call unless such a link exists."""
        from . import sharing
        now = __import__("time").time()
        if self.store.up and any(sharing.expires_at(c) and sharing.expires_at(c) <= now + 1
                                 for c in sharing.tunnels(self.store.containers)):
            from .util import bg
            bg(sharing.remove_expired, self.store.engine,
               done=lambda gone: gone and (self.toast(f"Public link expired and stopped: {', '.join(gone)}"),
                                           self.store.refresh("containers")),
               error=lambda e: None)
        return True

    def _counts(self):
        cs = self.store.containers
        run = sum(1 for c in cs if c.get("State") == "running")
        self.counts["containers"].set_text(f"{run}/{len(cs)}" if cs else "")
        from .util import state_of
        bad = [c for c in cs if state_of(c) == "unhealthy"]
        self.sb_bad.set_visible(bool(bad))
        if bad:
            self.sb_bad.set_label(f"⚠ {len(bad)} unhealthy")
            self.sb_bad.set_tooltip_text("Unhealthy: " + ", ".join(c["Names"][0].lstrip("/") for c in bad[:8]) + " — click to show")
        self.counts["images"].set_text(str(len(self.store.images)) if self.store.images is not None else "")
        live = sum(1 for c in cs if (c.get("Labels") or {}).get("io.berth.role") == "tunnel" and c["State"] == "running")
        self.sb_share.set_visible(live > 0)
        self.sb_share.set_label(f"● {live} public link{'s' if live != 1 else ''}")
        from .db import detect
        nd = sum(1 for c in cs if detect(c))
        self.counts["databases"].set_text(str(nd) if nd else "")
        self.counts["volumes"].set_text(str(len(self.store.volumes)) if self.store.volumes is not None else "")

    # ── navigation ──
    def select_row(self, key):
        for lb in (self.sidebar, self.sidebar_end):
            i = 0
            while (r := lb.get_row_at_index(i)) is not None:
                if r.key == key:
                    lb.select_row(r)
                    (self.sidebar_end if lb is self.sidebar else self.sidebar).unselect_all()
                    return
                i += 1

    def go(self, key, from_sidebar=False):
        if key not in self.pages:
            return
        self.page = key
        self.settings["page"] = key
        if not from_sidebar:
            self.select_row(key)
        if self.store.up:
            self.stack.set_visible_child_name(key)
            p = self.pages[key]
            if hasattr(p, "on_show"):
                p.on_show()
        self.update_saver()

    def show_container(self, cid, tab=None):
        self.go("containers")
        self.pages["containers"].open_detail(cid, tab)

    def toast(self, msg):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(msg)), timeout=4))

    def notify(self, title, body="", toast=False, key=None):
        """A desktop notification (also when Berth is in the tray), plus a toast when the window is in front.
        Off in Settings → Notifications: then only the toast."""
        if toast or self.is_active():
            self.toast(f"{title}: {body}" if body else title)
        if not self.settings["notify"] or self.is_active():
            return
        try:
            n = Gio.Notification.new(title)
            if body:
                n.set_body(body[:300])
            n.set_default_action("app.show-window")
            self.get_application().send_notification(key, n)
        except Exception:                         # no notification service: the toast is enough
            self.toast(f"{title}: {body}" if body else title)

    def _check_update(self):
        if not self.settings["update_check"] or os.environ.get("FLATPAK_ID") or os.environ.get("BERTH_NO_UPDATE_CHECK"):
            return False
        from . import update
        from .util import bg

        def done(r):
            if r and r[0] != self.settings["update_skip"]:
                self.update_banner.set_title(f"Berth {r[0]} is out — you have {VERSION}")
                self.update_banner.url = r[1]
                self.update_banner.set_revealed(True)
        bg(update.check, done=done, error=lambda e: None)
        return False

    def _run_backups(self):
        """Scheduled backups that are due (every 10 min, and once after start to catch up missed ones)."""
        from . import backups
        sch = self.settings["backups"] or []
        if not (sch and self.store.up):
            return True
        state = backups.load_state()
        if not any(backups.is_due(x, state) for x in sch):
            return True
        from .util import bg

        def done(res):
            for x, ok, msg in res:
                if not ok:
                    self.notify("Backup failed", f"{x['target']}: {msg[:200]}", key=f"backup-{x['id']}")
            if "backups" in self.pages:
                self.pages["backups"].refresh()
        bg(backups.run_due, self.store.engine, sch, done=done, error=lambda e: None)
        return True

    def _watch(self):
        """Once a minute: resource alerts (only when one is on), the daily image-update check (also while the Images
        page is closed) and scheduled vulnerability scans. See watch.py."""
        import time
        from . import watch
        from .util import bg
        s, st = self.settings, self.store
        if not st.up:
            return True
        now = time.time()
        if "images" in self.pages and now - self._last_img_check > 3600:
            if st.images is None:
                st.refresh("images")                       # loaded by the next tick
            else:
                self._last_img_check = now
                self.pages["images"]._daily_check()
        if (s["alert_cpu"] or s["alert_mem"] or s["alert_disk"]) and "alerts" not in self._watch_busy:
            if not hasattr(self, "alerts"):
                self.alerts = watch.Alerts()
            self._watch_busy.add("alerts")

            def shown(res):
                self._watch_busy.discard("alerts")
                for key, title, body in res:
                    self.notify(title, body, key=key)
            bg(self.alerts.check, st.engine, list(st.containers or []), dict(st.info or {}), s["alert_cpu"],
               s["alert_mem"], s["alert_disk"], done=shown, error=lambda e: self._watch_busy.discard("alerts"))
        if watch.scan_due(s["scan_schedule"], s["scan_last"]) and st.images and "scan" not in self._watch_busy:
            self._watch_busy.add("scan")
            s["scan_last"] = now

            def scanned(res):
                self._watch_busy.discard("scan")
                bad = [f"{r} ({c} critical, {h} high)" for r, c, h in res if c or h]
                self.notify("Scheduled scan: " + (f"{len(bad)} of {len(res)} images need attention" if bad else
                                                  f"{len(res)} images, nothing critical or high"),
                            ", ".join(bad[:4]) + (" …" if len(bad) > 4 else ""), key="scheduled-scan")
                if "images" in self.pages:
                    self.pages["images"]._fill()
            bg(watch.scan_in_use, st.engine, list(st.images), list(st.containers or []), done=scanned,
               error=lambda e: self._watch_busy.discard("scan"))
        return True

    # ── resource saver: sample only while someone can see the numbers ──
    def update_saver(self):
        visible = self.get_visible() and not self.get_property("suspended")
        focused = self.is_active() or not self.settings["saver"]
        want = bool(self.store.up and visible and focused)
        self.store.want_stats(want)
        self.sb_saver.set_text("" if want or not self.store.up else
                               "Live stats paused — resource saver (window not focused)" if visible else "Live stats paused")

    # ── theme ──
    def apply_theme(self, name):
        self.settings["theme"] = name
        theme.apply(name)
        a = self.get_application().lookup_action("theme")
        if a:
            a.set_state(GLib.Variant.new_string(name))
        st = self.pages.get("settings")
        if st is not None and hasattr(st, "theme"):
            want = ["system", "light", "dark"].index(name)
            if st.theme.get_selected() != want:
                st.theme.set_selected(want)

    def toggle_theme(self):
        self.apply_theme("light" if Adw.StyleManager.get_default().get_dark() else "dark")

    def _sync_theme_btn(self):
        dark = Adw.StyleManager.get_default().get_dark()
        self.theme_btn.set_icon_name("weather-clear-symbolic" if dark else "weather-clear-night-symbolic")
        for p in self.pages.values():
            if hasattr(p, "update"):
                p.update(force=True) if p.get_mapped() else None

    def focus_search(self):
        p = self.pages.get(self.page)
        if p and hasattr(p, "focus_search"):
            p.focus_search()

    def _on_close(self, *_):
        self.settings["window"] = [self.get_width(), self.get_height()]
        self.pages["containers"].save_state()
        app = self.get_application()
        if app.tray is not None and not app.quitting:
            self.set_visible(False)          # stays in the tray; live stats pause while hidden
            self.update_saver()
            return True
        return False

    def set_tray(self, on):
        self.get_application().set_tray(on)

    def _tray_state(self):
        app = self.get_application()
        if app.tray is not None:
            cs = self.store.containers
            app.tray.update(bool(self.store.up), sum(1 for c in cs if c.get("State") == "running"), len(cs))

    # ── dev: render the window to a PNG without touching the screen ──
    def screenshot(self, path):
        w, h = self.get_width(), self.get_height()
        snap = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(self).snapshot(snap, w, h)
        node = snap.to_node()
        if node is None:
            raise RuntimeError("nothing rendered yet")
        r = Gsk.CairoRenderer()
        r.realize_for_display(Gdk.Display.get_default())
        rect = Graphene.Rect()
        rect.init(0, 0, w, h)
        r.render_texture(node, rect).save_to_png(path)
        r.unrealize()


class App(Adw.Application):
    def __init__(self, shot=None, page=None):
        flags = (Gio.ApplicationFlags.NON_UNIQUE if shot or os.environ.get("BERTH_SELF_TEST")
                 else Gio.ApplicationFlags.DEFAULT_FLAGS)
        super().__init__(application_id=APP_ID, flags=flags)
        self.settings = Settings()
        self.shot, self.start_page = shot, page
        self.win = None
        self.tray = None
        self.quitting = False

    def do_startup(self):
        Adw.Application.do_startup(self)
        theme.apply(self.settings["theme"] or "system")
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(HERE, "style.css"))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        icons = os.path.join(os.path.dirname(HERE), "data", "icons")
        if os.path.isdir(icons):
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(icons)

        def act(name, fn, param=None, state=None):
            a = Gio.SimpleAction.new_stateful(name, param, state) if state is not None else Gio.SimpleAction.new(name, param)
            a.connect("activate", fn)
            self.add_action(a)
            return a
        act("theme", lambda a, p: self.win.apply_theme(p.get_string()), GLib.VariantType.new("s"),
            GLib.Variant.new_string(self.settings["theme"] or "system"))
        act("preferences", lambda *_: self.win.go("settings"))
        act("about", lambda *_: self._about())
        act("shortcuts", lambda *_: self._shortcuts())
        act("welcome", lambda *_: self._welcome())
        act("quit", lambda *_: self._really_quit() if self.win else self.quit())
        act("show-window", lambda *_: self._show() if hasattr(self, "_show") else self.activate())
        act("search", lambda *_: self.win.focus_search())
        act("palette", lambda *_: self.win and __import__("berth.palette", fromlist=["x"]).Palette(self.win)
            .present(self.win))
        act("go", lambda _a, p: self.win.go(PAGES[p.get_int32()][0]), GLib.VariantType.new("i"))
        self.set_accels_for_action("app.preferences", ["<Primary>comma"])
        self.set_accels_for_action("app.quit", ["<Primary>q"])
        self.set_accels_for_action("app.search", ["<Primary>f"])
        self.set_accels_for_action("app.palette", ["<Primary>k", "<Primary>p"])
        for i in range(min(9, len(PAGES))):
            self.set_accels_for_action(f"app.go({i})", [f"<Primary>{i + 1}"])

    def set_tray(self, on):
        self.settings["tray"] = on
        if on and self.tray is None:
            from .tray import Tray
            try:
                self.tray = Tray(app_icon(), self._show, lambda: self.win.engine_toggle(), self._really_quit)
                self.hold()
                self.win._tray_state()
            except GLib.Error as e:
                self.tray = None
                self.win.toast(f"Tray not available: {e.message}")
        elif not on and self.tray is not None:
            self.tray.stop()
            self.tray = None
            self.release()

    def _show(self):
        self.win.set_visible(True)
        self.win.present()
        self.win.update_saver()

    def _really_quit(self):
        self.quitting = True
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
            self.release()
        self.win.close()
        self.quit()

    def do_activate(self):
        if self.win and not self.win.get_visible():
            return self._show()
        if not self.win:
            self.win = Window(self, self.settings)
            if self.settings["tray"]:
                GLib.idle_add(lambda: self.set_tray(True) or False)
            if self.start_page:
                self.win.page = self.start_page
                self.win.select_row(self.start_page)
        self.win.present()
        if not self.settings["onboarded"] and not self.shot and not os.environ.get("BERTH_NO_WELCOME"):
            GLib.timeout_add(300, lambda: self._welcome() and False)
        if os.environ.get("BERTH_OPEN"):            # dev: open a container's details once the list is loaded
            def open_it():
                c = next((c for c in self.win.store.containers
                          if os.environ["BERTH_OPEN"] in [n.lstrip("/") for n in c.get("Names", [])]), None)
                if c:
                    self.win.show_container(c["Id"], os.environ.get("BERTH_TAB"))
                return False
            GLib.timeout_add(2500, open_it)
        if os.environ.get("BERTH_SELF_TEST"):
            GLib.timeout_add_seconds(3, lambda: self._self_test() and False)
        if self.shot:
            tries = {"n": 0}

            def take():
                try:
                    self.win.screenshot(self.shot)
                    print("saved", self.shot, flush=True)
                except RuntimeError:
                    tries["n"] += 1
                    if tries["n"] < 40:
                        return True         # caught mid-redraw; try again shortly
                    print("screenshot failed", flush=True)
                self.quit()
                return False
            GLib.timeout_add_seconds(int(os.environ.get("BERTH_SHOT_DELAY", "6")),
                                     lambda: GLib.timeout_add(50, take) and False)

    def _self_test(self):  # noqa: C901
        """`berth --self-test` (CI on Windows, macOS, Linux): open every page and the main dialogs, close them, and
        quit with exit code 1 if anything raised. Works without a Docker engine."""
        import sys as _sys
        from . import util
        errors = []
        prev = _sys.excepthook

        def hook(t, v, tb):
            errors.append(f"{t.__name__}: {v}")
            prev(t, v, tb)
        _sys.excepthook = hook
        real_log = util.log_error

        def counting(where, e=None, tb=None):
            text = f"{where}: {e or (tb or '').strip().splitlines()[-1:]}"
            if "Docker Engine" not in text and "EngineError" not in text:     # no engine on a CI runner: expected
                errors.append(text)
            real_log(where, e, tb) if tb is None else real_log(where, e, tb=tb)
        util.log_error = counting
        w = self.win
        dialogs = []

        def make():
            from .palette import Palette
            from .pages import PullDialog, RunDialog, BuildDialog
            from .compose import ComposeOpen
            from .backups_page import ScheduleDialog
            from .onboarding import Onboarding
            return [lambda: Palette(w), lambda: PullDialog(w, w.store),
                    lambda: RunDialog(w, w.store, _sample_image(w.store) or
                                      {"ref": "busybox:latest", "repo": "busybox", "tag": "latest", "id": None}),
                    lambda: BuildDialog(w, w.store), lambda: ComposeOpen(w, w.store),
                    lambda: ScheduleDialog(w.pages["backups"]), lambda: Onboarding(w)]

        def steps():
            for key, _cls in PAGES:
                w.go(key)
                yield 400
            try:
                dialogs.extend(make())
            except Exception as e:                  # an import that fails in a packaged build
                errors.append(f"dialogs: {e}")
            for mk in dialogs:
                try:
                    d = mk()
                    d.present(w)
                    yield 500
                    d.force_close()
                except Exception as e:
                    errors.append(f"{mk}: {e}")
                yield 150
        it = steps()

        def tick():
            try:
                GLib.timeout_add(next(it), tick)
            except Exception as e:                    # a crash in a step must end the test, not hang it
                if not isinstance(e, StopIteration):
                    errors.append(f"self-test step: {type(e).__name__}: {e}")
                print(f"self-test: {len(PAGES)} pages, {len(dialogs)} dialogs, {len(errors)} errors", flush=True)
                for e in errors[:20]:
                    print("  ", e, flush=True)
                self.self_test_failed = bool(errors)
                self.quit()
            return False
        tick()
        return False

    def do_shutdown(self):
        """Public links stop with Berth unless the user chose to keep them (a share should never outlive the app
        by accident)."""
        if self.win is not None:
            try:
                self.win.pages["send"].shutdown()        # file shares never outlive Berth
            except Exception:  # noqa: BLE001
                pass
        if self.win is not None and self.win.store.up and not self.shot:
            try:
                from .sharing import stop_unpinned
                stop_unpinned(self.win.store.engine)
            except Exception:  # noqa: BLE001 — never block quitting
                pass
        Adw.Application.do_shutdown(self)

    def _about(self):
        repo = "https://github.com/subhadeeproy3902/open-docker-app"
        d = Adw.AboutDialog(application_name="Berth", application_icon=app_icon(), version=VERSION,
                            developer_name="Open source · MIT licensed", license_type=Gtk.License.MIT_X11,
                            copyright="© 2026 The Berth contributors",
                            developers=[f"The Berth contributors {repo}/graphs/contributors"],
                            comments="A light, native, open-source Docker desktop. No virtual machine, no background "
                                     "service, no account: it talks straight to your Docker Engine.",
                            website=repo, issue_url=f"{repo}/issues")
        d.add_link("Contribute", f"{repo}/blob/main/CONTRIBUTING.md")
        d.add_link("Discussions", f"{repo}/discussions")
        d.present(self.win)

    def _welcome(self):
        from .onboarding import Onboarding
        Onboarding(self.win).present(self.win)

    def _shortcuts(self):
        d = Adw.AlertDialog(heading="Keyboard shortcuts")
        d.set_body_use_markup(True)
        d.set_body("<tt>Ctrl+K</tt>  Go to anything — a container, stack, image, page or task\n"
                   "<tt>Ctrl+F</tt>  Search the current page\n<tt>Ctrl+1…9</tt>  " + ", ".join(
            cls.title for _k, cls in PAGES[:9]) + "\n<tt>Ctrl+Enter</tt>  Run the query (Databases)\n"
            "<tt>Ctrl+,</tt>  Settings\n<tt>Ctrl+Q</tt>  Quit")
        d.add_response("ok", "Close")
        d.present(self.win)


def _sample_image(store):
    """An image for the self-test's Run dialog: a real one, or None when the engine has none (then the dialog opens
    with a placeholder that is never sent to an engine)."""
    for im in store.images or []:
        tags = [t for t in im.get("RepoTags") or [] if t != "<none>:<none>"]
        if tags:
            repo, _, tag = tags[0].rpartition(":")
            return {"ref": tags[0], "repo": repo, "tag": tag, "id": im["Id"]}
    return None


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    from .util import install_error_log
    install_error_log()
    if "--version" in argv:
        print(f"Berth {VERSION}")
        return 0
    shot = page = None
    if "--screenshot" in argv:
        i = argv.index("--screenshot")
        shot = argv[i + 1]
        del argv[i:i + 2]
    if "--page" in argv:
        i = argv.index("--page")
        page = argv[i + 1]
        del argv[i:i + 2]
    if "--self-test" in argv:
        argv.remove("--self-test")
        for k in ("BERTH_SELF_TEST", "BERTH_NO_WELCOME", "BERTH_NO_UPDATE_CHECK"):
            os.environ[k] = "1"
    app = App(shot, page)
    rc = app.run(argv)
    return 1 if getattr(app, "self_test_failed", False) else rc
