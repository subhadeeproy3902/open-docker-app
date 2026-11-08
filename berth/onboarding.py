"""First-run welcome: connect an engine, make Berth yours, run a first container.

Modelled on what a Docker Desktop first run has to achieve (a working engine, a first container) without the parts
people skip anyway: no account, no survey, no terms wall. Every step can be skipped; it never blocks the app."""
import webbrowser

from gi.repository import Adw, GLib, Gtk

from . import theme
from .engine import diagnose, discover, socket_path
from .util import IS_LINUX, IS_MAC, IS_WIN, app_icon, bg, label

SAMPLE_IMAGE, SAMPLE_NAME, SAMPLE_PORT = "docker/welcome-to-docker:latest", "berth-welcome", 8088
STATE_TEXT = {"running": ("Running", "ok"), "stopped": ("Stopped", "warn"), "denied": ("No access", "bad"),
              "missing": ("Not installed", "bad"), "unreachable": ("Unreachable", "bad"), None: ("Checking…", "muted")}


def run_sample(store, progress, done, error):
    """Pull and start the welcome sample on port 8088 (or start it again if it already exists). Runs in the background."""
    def work():
        eng = store.engine
        for c in eng.containers():
            if f"/{SAMPLE_NAME}" in c.get("Names", []):
                if c.get("State") != "running":
                    eng.action(c["Id"], "start")
                return c["Id"]
        st = eng.pull(SAMPLE_IMAGE)
        GLib.idle_add(progress, "Downloading the sample image…")
        while True:
            c = st.read(1.0)
            if c == b"":
                break
        GLib.idle_add(progress, "Starting it…")
        r = eng.create_container(SAMPLE_NAME, {
            "Image": SAMPLE_IMAGE, "ExposedPorts": {"80/tcp": {}},
            "HostConfig": {"PortBindings": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(SAMPLE_PORT)}]}}})
        eng.action(r["Id"], "start")
        return r["Id"]
    bg(work, done=done, error=error)


def _step(icon_widget, title, subtitle):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["onb-step"])
    if icon_widget:
        box.append(icon_widget)
    box.append(label(title, css=["onb-title"], xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER))
    if subtitle:
        box.append(label(subtitle, css=["onb-subtitle"], xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER))
    return box


def _feature(icon, title, text):
    row = Gtk.Box(spacing=14, css_classes=["onb-feature"])
    row.append(Gtk.Image(icon_name=icon, pixel_size=22, valign=Gtk.Align.START, css_classes=["onb-feature-icon"]))
    col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    col.append(label(title, css=["heading"]))
    col.append(label(text, css=["dim-label"], wrap=True))
    row.append(col)
    return row


class Onboarding(Adw.Dialog):
    def __init__(self, win):
        super().__init__(title="Welcome to Berth", content_width=640, content_height=600, can_close=True)
        self.win, self.store, self.s = win, win.store, win.settings
        self.engines, self.states, self.checks = [], {}, {}
        tv = Adw.ToolbarView(top_bar_style=Adw.ToolbarStyle.FLAT, bottom_bar_style=Adw.ToolbarStyle.FLAT)
        hb = Adw.HeaderBar(show_title=False)
        self.skip = Gtk.Button(label="Skip")
        self.skip.add_css_class("flat")
        self.skip.connect("clicked", lambda *_: self._finish())
        hb.pack_start(self.skip)
        tv.add_top_bar(hb)
        self.carousel = Adw.Carousel(interactive=False, allow_scroll_wheel=False, vexpand=True)
        for page in (self._welcome(), self._engine(), self._appearance(), self._ready()):
            # scrolls on short windows, so Back / Continue at the bottom always stay visible
            self.carousel.append(Gtk.ScrolledWindow(child=page, hscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True,
                                                    propagate_natural_height=True))
        self.carousel.connect("page-changed", lambda *_: self._sync())
        tv.set_content(self.carousel)
        # Buttons at the sides, dots in whatever space is left: never overlaps, whatever the label or font size.
        foot = Gtk.Box(spacing=12, css_classes=["onb-footer"])
        self.back = Gtk.Button(label="Back")
        self.back.connect("clicked", lambda *_: self._go(-1))
        self.next = Gtk.Button(label="Get started")
        self.next.add_css_class("suggested-action")
        self.next.add_css_class("pill")
        self.next.connect("clicked", lambda *_: self._go(1))
        foot.append(self.back)
        foot.append(Adw.CarouselIndicatorDots(carousel=self.carousel, hexpand=True, halign=Gtk.Align.CENTER))
        foot.append(self.next)
        tv.add_bottom_bar(foot)
        self.set_child(tv)
        self.connect("closed", lambda *_: self._closed())
        self._sid = self.store.connect("engine", lambda *_: self._check())
        self._sync()

    # ── pages ──
    def _welcome(self):
        page = _step(Gtk.Image(icon_name=app_icon(), pixel_size=112, css_classes=["onb-logo"]),
                     "Welcome to Berth", "A fast, open-source home for your containers.")
        feats = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, css_classes=["onb-features"],
                        halign=Gtk.Align.CENTER)
        feats.append(_feature("network-server-symbolic", "Uses the engine you already have",
                              "No virtual machine and nothing left running in the background."))
        feats.append(_feature("utilities-system-monitor-symbolic", "Everything in one place",
                              "Containers, images, volumes, logs, live graphs and a terminal."))
        feats.append(_feature("security-high-symbolic", "No account, no telemetry",
                              "Free for everyone, for any use. MIT licensed."))
        page.append(feats)
        return page

    def _engine(self):
        page = _step(None, "Connect to your engine", "Berth works with Docker, rootless Docker, Podman, Colima, "
                                                     "OrbStack, Rancher Desktop and remote engines.")
        self.eng_group = Adw.PreferencesGroup(css_classes=["onb-wide"])
        page.append(self.eng_group)
        self.callout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["onb-callout", "card"])
        page.append(self.callout)
        bg(discover, done=self._fill_engines, error=lambda e: self._fill_engines([]))
        return page

    def _appearance(self):
        page = _step(None, "Make it yours", "You can change all of this later in Settings.")
        g = Adw.PreferencesGroup(css_classes=["onb-wide"])
        th = Adw.ToggleGroup(can_shrink=False, css_classes=["round"], valign=Gtk.Align.CENTER)
        for key, name in (("system", "System"), ("light", "Light"), ("dark", "Dark")):
            th.add(Adw.Toggle(name=key, label=name))
        th.set_active_name(self.s["theme"] or "system")
        th.connect("notify::active-name", lambda t, _p: self.win.apply_theme(t.get_active_name()))
        row = Adw.ActionRow(title="Appearance")
        row.add_suffix(th)
        g.add(row)
        sizes = [85, 100, 115, 130, 150]
        ts = Adw.ComboRow(title="Text size", subtitle="Makes every label, table and chart larger",
                          model=Gtk.StringList.new([f"{n} %" for n in sizes]))
        ts.set_selected(sizes.index(self.s["text_scale"]) if self.s["text_scale"] in sizes else 1)
        ts.connect("notify::selected", lambda r, _p: self._a11y("text_scale", sizes[r.get_selected()]))
        g.add(ts)
        dens = ["compact", "comfortable", "spacious"]
        dn = Adw.ToggleGroup(can_shrink=False, css_classes=["round"], valign=Gtk.Align.CENTER)
        for d in dens:
            dn.add(Adw.Toggle(name=d, label=d.capitalize()))
        dn.set_active_name(self.s["density"] if self.s["density"] in dens else "comfortable")
        dn.connect("notify::active-name", lambda t, _p: self._a11y("density", t.get_active_name()))
        row = Adw.ActionRow(title="Density")
        row.add_suffix(dn)
        g.add(row)
        if IS_LINUX:
            tray = Adw.SwitchRow(title="Keep Berth in the tray", subtitle="Closing the window keeps the icon")
            tray.set_active(bool(self.s["tray"]))
            tray.connect("notify::active", lambda r, _p: self.win.get_application().set_tray(r.get_active()))
            g.add(tray)
        page.append(g)
        return page

    def _ready(self):
        page = _step(Gtk.Image(icon_name="object-select-symbolic", pixel_size=40, css_classes=["onb-done-icon"],
                               halign=Gtk.Align.CENTER),
                     "You're all set", None)
        self.summary = label("", css=["onb-subtitle"], xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER)
        page.append(self.summary)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["onb-callout", "card"])
        card.append(label("Try it: run your first container", css=["heading"]))
        card.append(label("Downloads a small sample web page and serves it on "
                          f"localhost:{SAMPLE_PORT}. Remove it any time from Containers.", css=["dim-label"], wrap=True))
        btns = Gtk.Box(spacing=8)
        self.sample_btn = Gtk.Button(label="Run sample container")
        self.sample_btn.add_css_class("suggested-action")
        self.sample_btn.connect("clicked", lambda *_: self._sample())
        self.open_btn = Gtk.Button(label=f"Open localhost:{SAMPLE_PORT}", visible=False)
        self.open_btn.connect("clicked", lambda *_: webbrowser.open(f"http://localhost:{SAMPLE_PORT}"))
        btns.append(self.sample_btn)
        btns.append(self.open_btn)
        card.append(btns)
        self.sample_note = label("", css=["caption", "dim-label"], wrap=True)
        card.append(self.sample_note)
        page.append(card)
        tips = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["onb-tips"], halign=Gtk.Align.CENTER)
        for keys, text in (("Ctrl F", "search the current page"), ("Ctrl 1 … 9", "jump between pages"),
                           ("Double-click", "a container for logs, terminal, files and graphs")):
            r = Gtk.Box(spacing=10)
            r.append(label(keys, css=["onb-key"], ellipsize=False))
            r.append(label(text, css=["dim-label"]))
            tips.append(r)
        page.append(tips)
        return page

    # ── engine step ──
    def _fill_engines(self, found):
        cur = self.store.engine.path
        default = socket_path()
        addrs = [("Docker Engine" if a == default else lab, a) for lab, a in found]
        if all(a != cur for _l, a in addrs):
            addrs.insert(0, ("Docker Engine" if cur == default else "Current engine", cur))
        self.engines = addrs
        first = None
        for lab, addr in addrs:
            row = Adw.ActionRow(title=lab, subtitle=addr, activatable=True)
            row.add_css_class("monospace-subtitle")
            chk = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=addr == cur)
            if first:
                chk.set_group(first)
            first = first or chk
            chk.connect("toggled", lambda b, a=addr: b.get_active() and self._choose(a))
            row.add_prefix(chk)
            row.set_activatable_widget(chk)
            pill = label("Checking…", css=["status-pill", "muted"], valign=Gtk.Align.CENTER, ellipsize=False)
            row.add_suffix(pill)
            self.checks[addr] = pill
            self.eng_group.add(row)
        other = Adw.EntryRow(title="Another engine, e.g. tcp://192.168.1.20:2375", show_apply_button=True)
        other.connect("apply", lambda r: self._choose(r.get_text().strip()) if r.get_text().strip() else None)
        self.eng_group.add(other)
        self._check()

    def _choose(self, addr):
        if addr != self.store.engine.path:
            self.s["engine"] = addr
            self.store.switch(addr)
        self._check()

    def _check(self):
        for addr in [a for _l, a in self.engines] or [self.store.engine.path]:
            bg(diagnose, addr, done=lambda r, a=addr: self._show_state(a, r), error=lambda e: None)

    def _show_state(self, addr, result):
        state, detail = result
        self.states[addr] = (state, detail)
        pill = self.checks.get(addr)
        if pill:
            text, cls = STATE_TEXT.get(state, STATE_TEXT[None])
            pill.set_text(text)
            pill.set_css_classes(["status-pill", cls])
        if addr == self.store.engine.path:
            self._callout(state, detail)
            self._sync()

    def _callout(self, state, detail):
        while (c := self.callout.get_first_child()):
            self.callout.remove(c)
        add = self.callout.append

        def button(text, cb, suggested=False):
            b = Gtk.Button(label=text, halign=Gtk.Align.START)
            if suggested:
                b.add_css_class("suggested-action")
            b.connect("clicked", lambda *_: cb())
            return b

        def command(cmd):
            row = Gtk.Box(spacing=8)
            row.append(label(cmd, css=["onb-cmd"], hexpand=True, selectable=True))
            copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy", valign=Gtk.Align.CENTER)
            copy.add_css_class("flat")
            copy.connect("clicked", lambda *_: (self.get_clipboard().set(cmd), self.win.toast("Copied")))
            row.append(copy)
            return row

        if state == "running":
            info = self.store.info or {}
            n_c, n_i = info.get("Containers"), info.get("Images")
            add(label(f"✓  Connected to {detail and 'version ' + detail or 'the engine'}", css=["heading", "success"]))
            if n_c is not None:
                add(label(f"{n_c} containers and {n_i} images are ready to manage.", css=["dim-label"]))
        elif state == "stopped":
            add(label("The engine is installed but not running", css=["heading"]))
            if IS_LINUX and not self.store.engine.path.startswith("tcp://"):
                add(label("Berth can start it for you. Your system asks for your password once.", css=["dim-label"],
                          wrap=True))
                add(button("Start Docker Engine", self._start, True))
            elif IS_MAC:
                add(label("Start Colima, OrbStack, Rancher Desktop or Docker Desktop, then Berth connects by itself.",
                          css=["dim-label"], wrap=True))
                add(command("colima start"))
            else:
                add(label("Start the engine, then Berth connects by itself.", css=["dim-label"], wrap=True))
        elif state == "denied":
            add(label("Your user is not allowed to use Docker yet", css=["heading"]))
            add(label("Add yourself to the docker group, then log out and back in:", css=["dim-label"], wrap=True))
            add(command("sudo usermod -aG docker $USER"))
            add(label("Prefer not to? Rootless Docker and Podman need no group at all.", css=["caption", "dim-label"],
                      wrap=True))
        elif state == "missing":
            add(label("No container engine found on this computer", css=["heading"]))
            if IS_MAC:
                add(label("The lightest option is Colima (free, open source):", css=["dim-label"], wrap=True))
                add(command("brew install colima docker && colima start"))
            elif IS_WIN:
                add(label("Use Docker Desktop with “Expose daemon on tcp://localhost:2375”, or Docker Engine in WSL 2.",
                          css=["dim-label"], wrap=True))
                add(button("Open the Windows guide", lambda: webbrowser.open(
                    "https://github.com/subhadeeproy3902/open-docker-app/blob/main/docs/INSTALL-Windows.md")))
            else:
                add(label("Install Docker Engine from your distribution, for example:", css=["dim-label"], wrap=True))
                add(command("sudo pacman -S docker   ·   sudo apt install docker.io   ·   sudo dnf install moby-engine"))
                add(button("Docker Engine install guide", lambda: webbrowser.open("https://docs.docker.com/engine/install/")))
        else:
            add(label("Berth can't reach this engine", css=["heading"]))
            add(label(detail or "Check the address and that the engine is running.", css=["dim-label"], wrap=True))

    def _start(self):
        self.win._engine_start()
        self._polls = 0

        def poll():
            self._polls += 1
            self._check()
            return self.store.up is not True and self._polls < 60
        GLib.timeout_add_seconds(1, poll)

    # ── appearance ──
    def _a11y(self, key, value):
        self.s[key] = value
        theme.apply_accessibility(self.win, self.s)

    # ── sample ──
    def _sample(self):
        if not self.store.up:
            self.sample_note.set_text("Start an engine first (step 2).")
            return
        self.sample_btn.set_sensitive(False)
        self.sample_note.set_text("Preparing…")

        def done(cid):
            self.sample_btn.set_label("Running")
            self.open_btn.set_visible(True)
            self.sample_note.set_text(f"It's running as “{SAMPLE_NAME}”. Open it in your browser, or find it in Containers.")
            self.store.refresh("containers")

        def fail(e):
            self.sample_btn.set_sensitive(True)
            self.sample_note.set_text(f"Couldn't run the sample: {e}")
        run_sample(self.store, lambda t: self.sample_note.set_text(t) and False, done, fail)

    # ── navigation ──
    def _go(self, d):
        i = int(round(self.carousel.get_position())) + d
        if i >= self.carousel.get_n_pages():
            return self._finish()
        if 0 <= i:
            self.carousel.scroll_to(self.carousel.get_nth_page(i), True)

    def _sync(self):
        i = int(round(self.carousel.get_position()))
        last = i == self.carousel.get_n_pages() - 1
        self.back.set_opacity(1 if i > 0 else 0)          # keep its space so the dots never jump
        self.back.set_sensitive(i > 0)
        self.skip.set_visible(not last)
        up = self.store.up is True
        if i == 0:
            self.next.set_label("Get started")
        elif i == 1:
            self.next.set_label("Continue" if up else "Continue without an engine")
        else:
            self.next.set_label("Open Berth" if last else "Continue")
        if last:
            info = self.store.info or {}
            self.summary.set_text(f"Connected · {info.get('Containers', 0)} containers · {info.get('Images', 0)} images"
                                  if up else "Berth connects as soon as your engine runs.")
            self.summary.set_visible(True)
            self.sample_btn.set_sensitive(up and self.sample_btn.get_label() != "Running")
            if not up and not self.sample_note.get_text():
                self.sample_note.set_text("Available once an engine is running — go back to step 2 to start one.")

    def _finish(self):
        self.s["onboarded"] = True
        self.close()

    def _closed(self):
        self.s["onboarded"] = True
        if self._sid:
            self.store.disconnect(self._sid)
            self._sid = None
