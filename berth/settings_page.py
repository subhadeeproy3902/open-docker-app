"""Settings page (sidebar): appearance, resource saver, Docker Engine and its daemon.json."""
import json
import os
import subprocess
import tempfile

from gi.repository import Adw, GLib, Gtk

from . import registry, theme
from .engine import discover
from .util import (WidePrefs, IS_LINUX, LOG_PATH, VERSION, PageHeader, bg, diagnostic_report, esc, flat_page, fmt_bytes,
                   host, open_uri)

DAEMON_JSON = "/etc/docker/daemon.json"


class SettingsPage(Gtk.Box):
    title, icon = "Settings", "emblem-system-symbolic"
    wants_stats = False

    def __init__(self, win, store, settings):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.store, self.s = win, store, settings
        tv = Adw.ToolbarView(vexpand=True)
        flat_page(tv, PageHeader("Settings", "Appearance, accessibility, the Docker Engine and registries"))
        pp = WidePrefs()                                   # wider, and two columns on big screens
        self.prefs = pp
        tv.set_content(pp)
        self.append(tv)

        g = Adw.PreferencesGroup(title="Appearance")
        self.theme = Adw.ComboRow(title="Theme", subtitle="“Follow system” keeps your desktop's colours",
                                  model=Gtk.StringList.new(["Follow system", "Light", "Dark"]))
        names = ["system", "light", "dark"]
        self.theme.set_selected(names.index(settings["theme"]) if settings["theme"] in names else 0)
        self.theme.connect("notify::selected", lambda r, *_: win.apply_theme(names[r.get_selected()]))
        g.add(self.theme)
        soft = Adw.SwitchRow(title="Software rendering", subtitle="Draw without the GPU (applies after restarting Berth)",
                             active=bool(settings["software"]))
        soft.connect("notify::active", lambda r, *_: settings.__setitem__("software", r.get_active()))
        g.add(soft)
        pp.add(g)

        ga = Adw.PreferencesGroup(title="Accessibility", description="Make Berth easier to read and use. Changes apply instantly.")
        sizes = list(theme.TEXT_SIZES)
        txt = Adw.ComboRow(title="Text size", subtitle="Scales every label, table and chart",
                           model=Gtk.StringList.new([f"{v} %" + (" (default)" if v == 100 else "") for v in sizes]))
        cur = int(settings["text_scale"] or 100)
        txt.set_selected(sizes.index(cur) if cur in sizes else 1)
        txt.connect("notify::selected", lambda r, *_: self._a11y("text_scale", sizes[r.get_selected()]))
        ga.add(txt)
        dens = Adw.ComboRow(title="Density", subtitle="Row height and spacing",
                            model=Gtk.StringList.new(["Compact", "Comfortable (default)", "Spacious"]))
        dens.set_selected(list(theme.DENSITIES).index(settings["density"]) if settings["density"] in theme.DENSITIES else 1)
        dens.connect("notify::selected", lambda r, *_: self._a11y("density", theme.DENSITIES[r.get_selected()]))
        ga.add(dens)
        for key, title, sub in (("strong_borders", "Stronger borders and contrast", "Clear outlines around cards, tables, buttons and inputs"),
                                ("large_icons", "Larger icons", "Bigger icons in the sidebar, toolbars and tables"),
                                ("reduce_motion", "Reduce motion", "Turn off sliding and fading animations")):
            row = Adw.SwitchRow(title=title, subtitle=sub, active=bool(settings[key]))
            row.connect("notify::active", lambda r, *_, k=key: self._a11y(k, r.get_active()))
            ga.add(row)
        pp.add(ga)

        g2 = Adw.PreferencesGroup(title="Resource saver",
                                  description="Berth reads live numbers only while you can see them, and nothing runs once the window is closed.")
        saver = Adw.SwitchRow(title="Pause live stats when Berth is not focused", active=bool(settings["saver"]))
        saver.connect("notify::active", lambda r, *_: (settings.__setitem__("saver", r.get_active()), win.update_saver()))
        g2.add(saver)
        iv = Adw.ComboRow(title="Sample live stats every", model=Gtk.StringList.new(["1 second", "2 seconds", "5 seconds"]))
        iv.set_selected({1: 0, 2: 1, 5: 2}.get(int(settings["interval"] or 2), 1))

        def set_iv(r, *_):
            val = [1, 2, 5][r.get_selected()]
            settings["interval"] = val
            store.sampler.interval = float(val)
        iv.connect("notify::selected", set_iv)
        g2.add(iv)
        pp.add(g2)

        g3 = Adw.PreferencesGroup(title="Tray")
        tray = Adw.SwitchRow(title="Keep Berth in the tray when the window is closed",
                             subtitle="Off = closing the window quits Berth completely (lightest). On = a tray icon shows the engine "
                                      "state and running containers; live stats stay paused while the window is hidden.",
                             active=bool(settings["tray"]))
        tray.connect("notify::active", lambda r, *_: win.set_tray(r.get_active()))
        g3.add(tray)
        if IS_LINUX:                    # StatusNotifierItem trays are a Linux desktop thing
            pp.add(g3)

        self.eng = Adw.PreferencesGroup(title="Docker Engine")
        self.endpoints = []
        self.engine_pick = Adw.ComboRow(title="Connected engine", subtitle="Docker, rootless Docker, Podman or a docker context")
        self.engine_pick.connect("notify::selected", self._pick_engine)
        self.eng.add(self.engine_pick)
        self.custom = Adw.EntryRow(title="Other engine address (unix socket path or tcp://host:port)", show_apply_button=True)
        self.custom.connect("apply", lambda e: self._switch(e.get_text().strip()))
        self.eng.add(self.custom)
        self.eng_state = Adw.ActionRow(title="Status")
        self.eng_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self.eng_btn.connect("clicked", lambda *_: win.engine_toggle())
        self.eng_state.add_suffix(self.eng_btn)
        self.eng.add(self.eng_state)
        self.info_rows = {}
        for key in ("Version", "Storage driver", "Data folder", "Socket", "CPUs / memory", "Operating system"):
            r = Adw.ActionRow(title=key, subtitle="—", subtitle_selectable=True)
            self.info_rows[key] = r
            self.eng.add(r)
        dj = Adw.ActionRow(title="Engine configuration", subtitle=f"Edit {DAEMON_JSON} (logging, registries, proxies, address pools…)",
                           activatable=True)
        dj.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        dj.connect("activated", lambda *_: DaemonJsonDialog(win).present(win))
        if IS_LINUX:                    # elsewhere the engine is configured in Docker Desktop / Colima / OrbStack
            self.eng.add(dj)
        pp.add(self.eng)
        # local addresses proxy: visible and removable from Settings too (issue #26)
        self.proxy_g = Adw.PreferencesGroup(title="Local addresses proxy",
                                            description="One small Caddy container serves every name.localhost "
                                                        "address. Nothing else runs for it.")
        self.proxy_row = Adw.ActionRow(title="Proxy", subtitle="Not set up")
        for text, tip, fn, css in (("Stop", "Stop the proxy; your addresses are kept", lambda: win.pages["sharing"]._stop(),
                                    None),
                                   ("Remove", "Remove the proxy container; your addresses are kept for later",
                                    lambda: win.pages["sharing"]._remove(), "destructive-action")):
            b = Gtk.Button(label=text, tooltip_text=tip, valign=Gtk.Align.CENTER)
            if css:
                b.add_css_class(css)
            b.connect("clicked", lambda _b, f=fn: f())
            self.proxy_row.add_suffix(b)
        self.proxy_g.add(self.proxy_row)
        manage = Adw.ActionRow(title="Addresses, ports and public links", subtitle="Open Ports &amp; sharing",
                               activatable=True)
        manage.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        manage.connect("activated", lambda *_: win.go("sharing"))
        self.proxy_g.add(manage)
        pp.add(self.proxy_g)

        self.reg = Adw.PreferencesGroup(title="Registries", description="Sign in to push to and pull private images from "
                                                                        "Docker Hub or another registry. Uses docker's own credential store.")
        add = Gtk.Button(label="Sign in…", valign=Gtk.Align.CENTER)
        add.connect("clicked", lambda *_: self._login())
        self.reg.set_header_suffix(add)
        self.reg_rows = []
        pp.add(self.reg)

        about = Adw.PreferencesGroup(title="About")
        ab = Adw.ActionRow(title=f"Berth {VERSION}", subtitle="A light, native Docker desktop for Linux · MIT licence", activatable=True)
        ab.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        ab.connect("activated", lambda *_: win.get_application().activate_action("about", None))
        about.add(ab)
        diag = Adw.ActionRow(title="Copy diagnostic report", activatable=True,
                             subtitle="Version, system, engine and the latest errors — paste it into an issue or chat")
        diag.add_suffix(Gtk.Image(icon_name="edit-copy-symbolic"))
        diag.connect("activated", lambda *_: self._copy_diag())
        about.add(diag)
        logs = Adw.ActionRow(title="Open the log folder", activatable=True, subtitle=LOG_PATH)
        logs.add_suffix(Gtk.Image(icon_name="folder-open-symbolic"))
        logs.connect("activated", lambda *_: open_uri("file://" + os.path.dirname(LOG_PATH)))
        about.add(logs)
        pp.add(about)
        store.connect("engine", lambda *_: self.get_mapped() and self.on_show())   # refresh only while visible

    def _a11y(self, key, value):
        self.s[key] = value
        theme.apply_accessibility(self.win, self.s)

    def on_size(self, n):
        self.prefs.on_size(n)

    def _copy_diag(self):
        self.get_clipboard().set(diagnostic_report(self.store))
        self.win.toast("Diagnostic report copied")

    # ── engines ──
    def _fill_engines(self):
        bg(discover, done=self._show_engines)        # discover() runs `docker context ls`: never on the UI thread

    def _show_engines(self, found):
        self._filling = True
        cur = self.store.engine.path
        if all(a != cur for _l, a in found):
            found.append(("Current", cur))
        self.endpoints = found
        self.engine_pick.set_model(Gtk.StringList.new([f"{l} — {a}" for l, a in found]))
        self.engine_pick.set_selected(next(i for i, (_l, a) in enumerate(found) if a == cur))
        self._filling = False

    def _pick_engine(self, *_):
        if getattr(self, "_filling", False) or not self.endpoints:
            return
        addr = self.endpoints[self.engine_pick.get_selected()][1]
        if addr != self.store.engine.path:
            self._switch(addr)

    def _switch(self, addr):
        if not addr:
            return
        self.s["engine"] = addr
        self.store.switch(addr)
        self.win.toast(f"Connecting to {addr}")

    # ── registries ──
    def _fill_registries(self):
        for r in self.reg_rows:
            self.reg.remove(r)
        self.reg_rows = []
        regs = registry.logins()
        if not regs:
            r = Adw.ActionRow(title="Not signed in to any registry")
            self.reg.add(r)
            self.reg_rows.append(r)
        for reg in regs:
            r = Adw.ActionRow(title=esc(registry.pretty(reg)), subtitle=esc(reg))
            b = Gtk.Button(label="Sign out", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_, x=reg: bg(registry.logout, x, done=lambda _r: (self.win.toast("Signed out"),
                                                                                            self._fill_registries()),
                                                       error=lambda e: self.win.toast(str(e))))
            r.add_suffix(b)
            self.reg.add(r)
            self.reg_rows.append(r)

    def _login(self):
        d = Adw.AlertDialog(heading="Sign in to a registry",
                            body="For Docker Hub use your username and an access token (hub.docker.com → Account settings → "
                                 "Personal access tokens).")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        reg = Gtk.Entry(placeholder_text="Registry (empty = Docker Hub), e.g. ghcr.io")
        user = Gtk.Entry(placeholder_text="Username")
        pw = Gtk.PasswordEntry(placeholder_text="Password or access token", show_peek_icon=True)
        for w in (reg, user, pw):
            box.append(w)
        d.set_extra_child(box)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Sign in")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def go(*_):
            if not user.get_text().strip() or not pw.get_text():
                return self.win.toast("Username and password are needed")
            self.win.toast("Signing in…")
            bg(registry.login, reg.get_text().strip(), user.get_text().strip(), pw.get_text(),
               done=lambda _r: (self.win.toast("Signed in"), self._fill_registries()),
               error=lambda e: self.win.toast(f"Sign-in failed: {e}"))
        d.connect("response", lambda _d, r: r == "ok" and go())
        d.present(self.win)

    def on_show(self):
        from . import sharing
        p = sharing.proxy(self.store.containers)
        n = len(self.s["routes"] or [])
        self.proxy_row.set_subtitle(("Running" if p.get("State") == "running" else "Stopped") + f" · {n} address"
                                    + ("es" if n != 1 else "") if p else "Not set up — it starts when you add an address")
        self._fill_engines()
        self._fill_registries()
        up = bool(self.store.up)
        self.eng_state.set_subtitle("Running" if up else "Stopped")
        self.eng_btn.set_label("Stop engine" if up else "Start engine")
        self.eng_btn.set_css_classes(["destructive-action"] if up else ["suggested-action"])
        info = self.store.info or {}
        vals = {"Version": info.get("ServerVersion"), "Storage driver": info.get("Driver"),
                "Data folder": info.get("DockerRootDir"), "Socket": self.store.engine.path,
                "CPUs / memory": f"{info.get('NCPU', '?')} / {fmt_bytes(info.get('MemTotal'))}" if info else None,
                "Operating system": info.get("OperatingSystem")}
        for k, r in self.info_rows.items():
            r.set_subtitle(esc(vals.get(k) or "—"))


class DaemonJsonDialog(Adw.Dialog):
    def __init__(self, win):
        super().__init__(title="Engine configuration", content_width=640, content_height=560)
        self.win = win
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.save = Gtk.Button(label="Save and restart engine")
        self.save.add_css_class("suggested-action")
        self.save.connect("clicked", lambda *_: self._save())
        hb.pack_start(cancel)
        hb.pack_end(self.save)
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.msg = Gtk.Label(label=f"{DAEMON_JSON} — saving asks for your password and restarts Docker (containers with a "
                                   "restart policy come back).", wrap=True, xalign=0, margin_start=14, margin_end=14,
                             margin_top=6, css_classes=["dim-label", "caption"])
        box.append(self.msg)
        self.text = Gtk.TextView(monospace=True, left_margin=12, right_margin=12, top_margin=8, bottom_margin=8)
        self.text.add_css_class("card")
        sw = Gtk.ScrolledWindow(child=self.text, vexpand=True, margin_start=12, margin_end=12, margin_bottom=12)
        box.append(sw)
        tv.set_content(box)
        self.set_child(tv)
        try:
            with open(DAEMON_JSON) as f:
                content = f.read()
        except FileNotFoundError:
            content = "{\n}\n"
        except OSError as e:
            content = ""
            self.msg.set_text(f"Can't read {DAEMON_JSON}: {e}")
            self.save.set_sensitive(False)
        self.text.get_buffer().set_text(content)

    def _save(self):
        b = self.text.get_buffer()
        raw = b.get_text(b.get_start_iter(), b.get_end_iter(), False)
        try:
            data = json.loads(raw or "{}")
        except ValueError as e:
            self.msg.set_text(f"Not valid JSON: {e}")
            self.msg.add_css_class("error")
            return
        pretty = json.dumps(data, indent=4) + "\n"
        self.save.set_sensitive(False)

        def work():
            # the JSON goes in on stdin: no temp file, which also works from inside a Flatpak sandbox
            script = (f"umask 022 && cat > {DAEMON_JSON}.berth-new && mv {DAEMON_JSON}.berth-new {DAEMON_JSON} "
                      "&& systemctl restart docker.service")
            r = subprocess.run(host(["pkexec", "sh", "-c", script]), input=pretty, capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(r.stderr.strip() or "cancelled")
            return True

        bg(work, done=lambda _r: (self.win.toast("Saved — Docker Engine restarted"), self.close()),
           error=lambda e: (self.save.set_sensitive(True), self.msg.set_text(f"Not saved: {e}")))
