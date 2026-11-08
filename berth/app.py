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
from . import theme  # noqa: E402
from .theme import apply_accessibility  # noqa: E402
from .files import cleanup_helpers  # noqa: E402
from .k8s import K8sPage  # noqa: E402
from .logs import LogsPage  # noqa: E402
from .pages import BuildHistoryPage, BuildsPage, DiskPage, ImagesPage, NetworksPage, ResourcesPage, VolumesPage  # noqa: E402
from .settings_page import SettingsPage  # noqa: E402
from .store import Store  # noqa: E402
from .util import APP_ID, VERSION, app_icon, themed, Settings, fmt_bytes, fmt_pct, icon_button, label, spawn  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PAGES = [("containers", ContainersPage), ("images", ImagesPage), ("volumes", VolumesPage), ("networks", NetworksPage),
         ("logs", LogsPage), ("resources", ResourcesPage), ("disk", DiskPage),
         ("builds", BuildHistoryPage), ("cache", BuildsPage),
         ("kubernetes", K8sPage),
         ("settings", SettingsPage)]
# Sidebar sections: the first page of each group gets a small heading; Settings sits apart at the bottom.
GROUPS = {"containers": "Manage", "logs": "Observe", "builds": "Build", "kubernetes": "Orchestrate"}
THEMES = {"system": Adw.ColorScheme.DEFAULT, "light": Adw.ColorScheme.FORCE_LIGHT, "dark": Adw.ColorScheme.FORCE_DARK}


class Window(Adw.ApplicationWindow):
    def __init__(self, app, settings):
        w, h = settings["window"] or (1240, 800)
        super().__init__(application=app, title="Berth", default_width=w, default_height=h, icon_name=app_icon())
        self.settings = settings
        self.store = Store(settings["engine"] or None)
        self.store.sampler.interval = float(settings["interval"] or 2)
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        split = Adw.NavigationSplitView(sidebar_width_fraction=0.18, min_sidebar_width=210, max_sidebar_width=260)
        self.toasts.set_child(split)

        # sidebar
        side = Adw.ToolbarView()
        sh = Adw.HeaderBar()
        sh.set_title_widget(Adw.WindowTitle(title="Berth"))
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
            self.sidebar.append(row)
        self.sidebar.set_header_func(self._sidebar_header)
        self.sidebar.connect("row-selected", lambda _l, r: r and self.go(r.key, from_sidebar=True))
        self.sidebar.set_vexpand(True)
        side.set_content(Gtk.ScrolledWindow(child=self.sidebar, vexpand=True))
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
        self.off_page = Adw.StatusPage(icon_name="system-shutdown-symbolic", title="Docker Engine is not running",
                                       description="Berth needs a running Docker Engine.")
        start = Gtk.Button(label="Start Docker Engine", halign=Gtk.Align.CENTER)
        start.add_css_class("pill")
        start.add_css_class("suggested-action")
        start.connect("clicked", lambda *_: self._engine_start())
        self.off_page.set_child(start)
        off.set_content(self.off_page)
        self.stack.add_named(off, "off")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.stack)
        content.append(self._statusbar())
        split.set_content(Adw.NavigationPage(title="Berth", child=content))

        self.store.connect("engine", lambda _s, up: self._engine(up))
        self.store.connect("containers", lambda *_: self._counts())
        self.store.connect("images", lambda *_: self._counts())
        self.store.connect("volumes", lambda *_: self._counts())
        self.store.connect("stats", lambda *_: self._status_stats())
        self.store.connect("containers", lambda *_: self._tray_state())
        self.store.connect("engine", lambda *_: self._tray_state())
        self.connect("notify::is-active", lambda *_: self.update_saver())
        self.connect("notify::suspended", lambda *_: self.update_saver())
        self.connect("close-request", self._on_close)
        # Responsive columns: on narrow windows the least important table columns step aside.
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1180sp"))
        for col in (self.pages["containers"].col_image, self.pages["containers"].col_ports,
                    self.pages["images"].col_id, self.pages["images"].c_created):
            bp.add_setter(col, "visible", False)
        for prop in ("min-children-per-line", "max-children-per-line"):   # resource tiles: 4 across → 2 × 2
            bp.add_setter(self.pages["resources"].tiles_box, prop, 2)
        self.add_breakpoint(bp)     # one breakpoint only: libadwaita applies just the last matching one
        self.set_size_request(760, 520)
        apply_accessibility(self, settings)
        self.page = settings["page"] if settings["page"] in self.pages else "containers"
        self.select_row(self.page)
        self.stack.set_visible_child_name("off")
        self._sync_theme_btn()
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._sync_theme_btn())

    def _sidebar_header(self, row, before):
        title = GROUPS.get(row.key)
        if row.key == "settings":
            sep = Gtk.Separator(margin_top=8, margin_bottom=6, margin_start=8, margin_end=8)
            row.set_header(sep)
        elif title:
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
        try:
            st = os.statvfs(root) if root else None
        except OSError:
            st = None
        self.sb_disk.set_text(f"Docker disk {fmt_bytes(st.f_bavail * st.f_frsize)} free" if st else "")
        self.sb_disk.set_tooltip_text(f"Free space where Docker keeps its data ({root})" if st else None)
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
        self.counts["volumes"].set_text(str(len(self.store.volumes)) if self.store.volumes is not None else "")

    # ── navigation ──
    def select_row(self, key):
        for i, (k, _c) in enumerate(PAGES):
            if k == key:
                self.sidebar.select_row(self.sidebar.get_row_at_index(i))

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
        flags = Gio.ApplicationFlags.NON_UNIQUE if shot else Gio.ApplicationFlags.DEFAULT_FLAGS
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
        act("search", lambda *_: self.win.focus_search())
        act("go", lambda _a, p: self.win.go(PAGES[p.get_int32()][0]), GLib.VariantType.new("i"))
        self.set_accels_for_action("app.preferences", ["<Control>comma"])
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.search", ["<Control>f"])
        for i in range(min(9, len(PAGES))):
            self.set_accels_for_action(f"app.go({i})", [f"<Control>{i + 1}"])

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
        d.set_body("<tt>Ctrl+F</tt>  Search the current page\n<tt>Ctrl+1…9</tt>  Containers, Images, Volumes, Networks, "
                   "Logs, Resource usage, Disk usage, Builds\n<tt>Ctrl+,</tt>  Settings\n<tt>Ctrl+Q</tt>  Quit")
        d.add_response("ok", "Close")
        d.present(self.win)


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
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
    return App(shot, page).run(argv)
