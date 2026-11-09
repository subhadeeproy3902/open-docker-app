"""Ports & sharing page: local addresses (name.localhost), public links (Cloudflare Tunnel), and every port in use."""
import re
import subprocess

from gi.repository import Adw, Gdk, GLib, Gtk

from . import sharing
from .util import PageHeader, bg, cname, esc, flat_page, host, host_which, icon_button, label, open_uri


def _running(store):
    return [c for c in store.containers if c.get("State") == "running"
            and not (c.get("Labels") or {}).get(sharing.ROLE)]


def _default_name(c):
    svc = (c.get("Labels") or {}).get("com.docker.compose.service") or cname(c)
    n = re.sub(r"[^a-z0-9-]+", "-", svc.lower()).strip("-")
    return n[:63] or "app"


class _TargetRows:
    """A container picker + a port picker that follows it (ports come from the engine in the background)."""

    def __init__(self, store, group, cid=None, port=None):
        self.store = store
        self.items = sorted(_running(store), key=cname)
        self.ctr = Adw.ComboRow(title="Container", model=Gtk.StringList.new([cname(c) for c in self.items] or
                                                                             ["No running containers"]))
        self.port = Adw.ComboRow(title="Port inside the container", model=Gtk.StringList.new(["…"]))
        self.custom = Adw.EntryRow(title="Or type a port", input_purpose=Gtk.InputPurpose.DIGITS)
        for w in (self.ctr, self.port, self.custom):
            group.add(w)
        self.ports, self._want = [], port
        if cid:
            for i, c in enumerate(self.items):
                if c["Id"] == cid or c["Id"].startswith(cid):
                    self.ctr.set_selected(i)
        self.ctr.connect("notify::selected", lambda *_: self._load())
        self._load()

    def container(self):
        i = self.ctr.get_selected()
        return self.items[i] if 0 <= i < len(self.items) else None

    def chosen_port(self):
        t = self.custom.get_text().strip()
        if t:
            return int(t) if t.isdigit() and 0 < int(t) < 65536 else None
        i = self.port.get_selected()
        return self.ports[i] if 0 <= i < len(self.ports) else None

    def _load(self):
        c = self.container()
        if not c:
            return

        def done(ports):
            self.ports = ports
            self.port.set_model(Gtk.StringList.new([str(p) for p in ports] or ["none declared — type one below"]))
            if self._want in ports:
                self.port.set_selected(ports.index(self._want))
            self._want = None
        bg(sharing.container_ports, self.store.engine, c["Id"], done=done, error=lambda e: done([]))


class ExposeDialog(Adw.Dialog):
    def __init__(self, page, cid=None, port=None):
        super().__init__(title="Add a local address", content_width=520, content_height=560)
        self.page, self.win, self.store = page, page.win, page.store
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.ok = Gtk.Button(label="Add")
        self.ok.add_css_class("suggested-action")
        self.ok.connect("clicked", lambda *_: self._add())
        hb.pack_end(self.ok)
        tv.add_top_bar(hb)
        pp = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(description="Open a container at a name like shop.localhost instead of remembering "
                                             "ports. Only this computer can use it. Berth runs one small proxy "
                                             "container (Caddy) for all addresses.")
        self.t = _TargetRows(self.store, g, cid, port)
        self.name = Adw.EntryRow(title="Name")
        c = self.t.container()
        self.name.set_text(_default_name(c) if c else "")
        self.t.ctr.connect("notify::selected", lambda *_: self.t.container() and
                           self.name.set_text(_default_name(self.t.container())))
        self.name.connect("changed", lambda *_: self._preview())
        g.add(self.name)
        self.https = Adw.SwitchRow(title="HTTPS", subtitle="Uses Berth's local certificate; trust it once to avoid "
                                                           "browser warnings")
        self.https.connect("notify::active", lambda *_: self._preview())
        g.add(self.https)
        pp.add(g)
        self.prev = Adw.PreferencesGroup()
        self.url = Adw.ActionRow(title="", subtitle="The address", css_classes=["property"])
        self.prev.add(self.url)
        pp.add(self.prev)
        tv.set_content(pp)
        self.set_child(tv)
        self._preview()

    def _preview(self):
        name = self.name.get_text().strip().lower()
        err = sharing.validate_name(name, self.page.routes())
        r = {"name": name or "name", "https": self.https.get_active()}
        self.url.set_title(esc(sharing.url_for(r, self.page.proxy_ports())))
        self.url.set_subtitle(esc(err) if err and name else "The address")
        self.ok.set_sensitive(not err)

    def _add(self):
        c, port = self.t.container(), self.t.chosen_port()
        if not c or not port:
            self.win.toast("Pick a container and a port")
            return
        route = {"name": self.name.get_text().strip().lower(), "container": cname(c), "port": port,
                 "https": self.https.get_active()}
        self.ok.set_sensitive(False)
        self.page.add_route(route, done=self.close, fail=lambda: self.ok.set_sensitive(True))


class ShareDialog(Adw.Dialog):
    def __init__(self, page, cid=None, port=None):
        super().__init__(title="Share publicly", content_width=540, content_height=640)
        self.page, self.win, self.store = page, page.win, page.store
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.ok = Gtk.Button(label="Share")
        self.ok.add_css_class("destructive-action")
        self.ok.connect("clicked", lambda *_: self._share())
        hb.pack_end(self.ok)
        tv.add_top_bar(hb)
        pp = Adw.PreferencesPage()
        warn = Adw.PreferencesGroup()
        warn.add(label("⚠  Anyone with the link can open this port from the internet. Share only what you would "
                       "show a stranger: a demo, a webhook endpoint — not a database or an admin page.",
                       css=["share-warning"], wrap=True))
        pp.add(warn)
        g = Adw.PreferencesGroup(title="What to share")
        self.t = _TargetRows(self.store, g, cid, port)
        pp.add(g)
        h = Adw.PreferencesGroup(title="How", description="Runs Cloudflare's cloudflared in a container next to it. "
                                                           "Nothing is installed on this computer.")
        self.mode = Adw.ComboRow(title="Tunnel", model=Gtk.StringList.new(
            ["Quick link (no account)", "My Cloudflare tunnel (token)"]))
        self.mode.set_subtitle("A random https://….trycloudflare.com address, new each time")
        self.mode.connect("notify::selected", lambda *_: self.mode.set_subtitle(
            "A random https://….trycloudflare.com address, new each time" if self.mode.get_selected() == 0 else
            "Uses the hostname you set up in the Cloudflare dashboard"))
        self.mode.connect("notify::selected", lambda *_: self.token.set_visible(self.mode.get_selected() == 1))
        h.add(self.mode)
        self.token = Adw.PasswordEntryRow(title="Tunnel token (Zero Trust → Networks → Tunnels)", visible=False)
        h.add(self.token)
        self.keep = Adw.SwitchRow(title="Keep sharing after Berth closes",
                                  subtitle="Off: the link stops when you quit Berth")
        h.add(self.keep)
        pp.add(h)
        self.status = label("", css=["dim-label"], wrap=True)
        sg = Adw.PreferencesGroup()
        sg.add(self.status)
        pp.add(sg)
        tv.set_content(pp)
        self.set_child(tv)

    def _share(self):
        c, port = self.t.container(), self.t.chosen_port()
        if not c or not port:
            self.win.toast("Pick a container and a port")
            return
        token = self.token.get_text().strip() if self.mode.get_selected() == 1 else None
        if self.mode.get_selected() == 1 and not token:
            self.win.toast("Paste the tunnel token, or pick the quick link")
            return
        self.ok.set_sensitive(False)
        self.status.set_text("Starting…")

        def progress(t):
            GLib.idle_add(lambda: self.status.set_text(t) and False)

        def done(r):
            _cid, url = r
            self.store.refresh("containers")
            self.close()
            self.page.shared(url, cname(c), port)

        def fail(e):
            self.ok.set_sensitive(True)
            self.status.set_text(f"Couldn't share: {e}")
        bg(sharing.start_tunnel, self.store.engine, c["Id"], port, token, self.keep.get_active(), progress,
           done=done, error=fail)


class SharingPage(Gtk.Box):
    title = "Ports & sharing"
    icon = "network-workgroup-symbolic"

    def __init__(self, win, store, settings):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.store, self.settings = win, store, settings
        self.urls, self._sig, self._applying = {}, None, False
        tv = Adw.ToolbarView(vexpand=True)
        self.header = PageHeader("Ports & sharing", "Friendly local addresses, public links, and every port in use")
        add = Gtk.Button(label="Add local address…")
        add.connect("clicked", lambda *_: ExposeDialog(self).present(self.win))
        share = Gtk.Button(label="Share publicly…")
        share.connect("clicked", lambda *_: ShareDialog(self).present(self.win))
        self.header.add(add)
        self.header.add(share)
        flat_page(tv, self.header)
        self.pp = Adw.PreferencesPage(css_classes=["sharing-page"])
        # local addresses
        self.g_local = Adw.PreferencesGroup(title="Local addresses",
                                            description="name.localhost → a container port. Only this computer can "
                                                        "open them.")
        hdr = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.proxy_pill = label("", css=["status-pill", "muted"], ellipsize=False, valign=Gtk.Align.CENTER)
        hdr.append(self.proxy_pill)
        self.proxy_menu = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text="Proxy", valign=Gtk.Align.CENTER)
        self.proxy_menu.add_css_class("flat")
        self.proxy_menu.set_popover(self._proxy_popover())
        hdr.append(self.proxy_menu)
        self.g_local.set_header_suffix(hdr)
        self.pp.add(self.g_local)
        self.local_rows = []
        # public links
        self.g_public = Adw.PreferencesGroup(title="Public links",
                                             description="Anyone with the link can open these. Each one stops when "
                                                         "Berth quits, unless you chose to keep it.")
        self.pp.add(self.g_public)
        self.public_rows = []
        # ports in use
        self.g_ports = Adw.PreferencesGroup(title="Ports in use",
                                            description="Published ports of your containers. ⚠ marks ports open to "
                                                        "your whole network, not just this computer.")
        self.pp.add(self.g_ports)
        self.port_rows = []
        tv.set_content(self.pp)
        self.append(tv)
        store.connect("containers", lambda *_: self.on_show())
        self.on_show()

    # ── settings ──
    def routes(self):
        return [dict(r) for r in (self.settings["routes"] or [])]

    def proxy_ports(self):
        p = self.settings["proxy_ports"] or [80, 443]
        return int(p[0]), int(p[1])

    # ── proxy menu ──
    def _proxy_popover(self):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=10, margin_bottom=10,
                      margin_start=10, margin_end=10, width_request=300)
        box.append(label("Proxy ports on 127.0.0.1", css=["heading"]))
        row = Gtk.Box(spacing=6)
        self.p_http = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.p_https = Gtk.SpinButton.new_with_range(1, 65535, 1)
        hp, sp = self.proxy_ports()
        self.p_http.set_value(hp)
        self.p_https.set_value(sp)
        row.append(label("HTTP", css=["dim-label"], valign=Gtk.Align.CENTER))
        row.append(self.p_http)
        row.append(label("HTTPS", css=["dim-label"], valign=Gtk.Align.CENTER))
        row.append(self.p_https)
        box.append(row)
        apply = Gtk.Button(label="Use these ports")
        apply.connect("clicked", lambda *_: (pop.popdown(), self._set_ports()))
        box.append(apply)
        box.append(Gtk.Separator())
        for text, fn in (("Start proxy", self._start), ("Stop proxy", self._stop),
                         ("Remove proxy (addresses are kept)", self._remove),
                         ("Save HTTPS certificate…", self._cert)):
            b = Gtk.Button(label=text)
            b.add_css_class("flat")
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, f=fn: (pop.popdown(), f()))
            box.append(b)
        pop.set_child(box)
        return pop

    def _set_ports(self):
        ports = [int(self.p_http.get_value()), int(self.p_https.get_value())]
        if ports[0] == ports[1]:
            self.win.toast("HTTP and HTTPS need different ports")
            return
        self.settings["proxy_ports"] = ports
        self._start()

    def _start(self, then=None):
        routes, ports = self.routes(), self.proxy_ports()

        def work():
            pid = sharing.start_proxy(self.store.engine, ports)
            sharing.apply_routes(self.store.engine, routes, pid)
            return pid
        bg(work, done=lambda _p: (self.store.refresh("containers"), then and then()),
           error=lambda e: self.win.toast(f"Proxy: {e}"))

    def _stop(self):
        bg(sharing.stop_proxy, self.store.engine, done=lambda _r: self.store.refresh("containers"),
           error=lambda e: self.win.toast(str(e)))

    def _remove(self):
        bg(sharing.stop_proxy, self.store.engine, True, done=lambda _r: self.store.refresh("containers"),
           error=lambda e: self.win.toast(str(e)))

    def _cert(self):
        p = sharing.proxy(self.store.containers)
        if not p:
            self.win.toast("Start the proxy first")
            return
        from .pages import save_dialog

        def got(pem):
            def write(path):
                with open(path, "wb") as f:
                    f.write(pem)
                d = Adw.AlertDialog(heading="Certificate saved",
                                    body=f"{path}\n\nTo stop HTTPS warnings, trust it once:\n"
                                         "• Firefox: Settings → Privacy & Security → Certificates → View "
                                         "Certificates → Authorities → Import\n"
                                         "• Chrome / system on Arch: sudo trust anchor <file>\n"
                                         "• Debian / Ubuntu: sudo cp <file> /usr/local/share/ca-certificates/"
                                         "berth.crt && sudo update-ca-certificates\n"
                                         "• macOS: open it, then Keychain Access → Always Trust")
                d.add_response("ok", "Done")
                d.present(self.win)
            save_dialog(self.win, "Save certificate", "berth-local-ca.crt", write)
        bg(sharing.root_certificate, self.store.engine, p["Id"], done=got, error=lambda e: self.win.toast(str(e)))

    # ── actions ──
    def add_route(self, route, done=None, fail=None):
        routes = [r for r in self.routes() if r["name"] != route["name"]] + [route]
        self.settings["routes"] = routes
        url = sharing.url_for(route, self.proxy_ports())

        def ok():
            done and done()
            t = Adw.Toast(title=f"{url} → {route['container']}:{route['port']}", button_label="Open", timeout=6)
            t.connect("button-clicked", lambda *_: open_uri(url))
            self.win.toasts.add_toast(t)

        def bad(e):
            self.win.toast(f"Proxy: {e}")
            fail and fail()
        ports = self.proxy_ports()

        def work():
            pid = sharing.start_proxy(self.store.engine, ports)
            return sharing.apply_routes(self.store.engine, routes, pid)
        bg(work, done=lambda _u: (self.store.refresh("containers"), ok()), error=bad)

    def remove_route(self, name):
        routes = [r for r in self.routes() if r["name"] != name]
        self.settings["routes"] = routes
        p = sharing.proxy(self.store.containers)
        if p and p.get("State") == "running":
            bg(sharing.apply_routes, self.store.engine, routes, p["Id"], error=lambda e: self.win.toast(str(e)))
        self.on_show()

    def shared(self, url, target, port):
        if url:
            self.urls[target, str(port)] = url
            self.get_clipboard().set(url)
            t = Adw.Toast(title=f"{url} — copied", button_label="Open", timeout=10)
            t.connect("button-clicked", lambda *_: open_uri(url))
        else:
            t = Adw.Toast(title=f"{target}:{port} is shared through your Cloudflare tunnel", timeout=8)
        self.win.toasts.add_toast(t)
        self.win.go("sharing")

    def _stop_tunnel(self, c):
        def go():
            bg(sharing.stop_tunnel, self.store.engine, c["Id"], done=lambda _r: self.store.refresh("containers"),
               error=lambda e: self.win.toast(str(e)))
        go()

    def _qr(self, url):
        try:
            png = subprocess.run(host(["qrencode", "-o", "-", "-s", "8", "-m", "2", url]), capture_output=True,
                                 timeout=5).stdout          # host(): through flatpak-spawn inside the Flatpak
        except (OSError, subprocess.SubprocessError):
            return
        tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
        d = Adw.Dialog(title="Scan to open", content_width=360, content_height=420)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12, margin_bottom=18,
                      margin_start=18, margin_end=18)
        box.append(Gtk.Picture(paintable=tex, can_shrink=True, vexpand=True))
        box.append(label(url, css=["caption", "monospace"], xalign=0.5, selectable=True))
        tv.set_content(box)
        d.set_child(tv)
        d.present(self.win)

    # ── refresh ──
    def on_show(self):
        cs = self.store.containers
        p = sharing.proxy(cs)
        running = bool(p and p.get("State") == "running")
        self.proxy_pill.set_text("Proxy running" if running else "Proxy stopped" if p else "Proxy not set up")
        self.proxy_pill.set_css_classes(["status-pill", "ok" if running else "muted"])
        ports = self.proxy_ports()

        for r in self.local_rows:
            self.g_local.remove(r)
        self.local_rows = []
        byname = {c2.get("Names", ["/"])[0].lstrip("/"): c2 for c2 in cs}
        for r in self.routes():
            url = sharing.url_for(r, ports)
            tgt = byname.get(r["container"])
            alive = tgt is not None and tgt.get("State") == "running"
            row = Adw.ActionRow(title=esc(url), subtitle=esc(f"→ {r['container']}:{r['port']}" +
                                                             ("" if alive else " · not running")))
            row.add_css_class("mono-title")
            ob = icon_button("adw-external-link-symbolic", "Open", lambda _b=None, u=url: open_uri(u))
            ob.set_sensitive(running and alive)
            row.add_suffix(ob)
            row.add_suffix(icon_button("edit-copy-symbolic", "Copy address",
                                       lambda _b=None, u=url: (self.get_clipboard().set(u), self.win.toast("Copied"))))
            row.add_suffix(icon_button("user-trash-symbolic", "Remove address",
                                       lambda _b=None, n=r["name"]: self.remove_route(n)))
            self.g_local.add(row)
            self.local_rows.append(row)
        if not self.local_rows:
            row = Adw.ActionRow(title="No local addresses yet",
                                subtitle="Give a container a name like shop.localhost — no ports to remember")
            b = Gtk.Button(label="Add…", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_: ExposeDialog(self).present(self.win))
            row.add_suffix(b)
            self.g_local.add(row)
            self.local_rows.append(row)

        for r in self.public_rows:
            self.g_public.remove(r)
        self.public_rows = []
        tun = sharing.tunnels(cs)
        for c in tun:
            lab = c.get("Labels") or {}
            key = (lab.get(sharing.TARGET), lab.get(sharing.PORT))
            url = self.urls.get(key)
            alive = c.get("State") == "running"
            token = lab.get(sharing.MODE) == "token"
            title = url or ("Your Cloudflare tunnel" if token else "Starting…" if alive else "Stopped")
            sub = f"→ {key[0]}:{key[1]} · " + ("Cloudflare tunnel (token)" if token else "Cloudflare quick tunnel")
            if lab.get(sharing.KEEP) == "1":
                sub += " · keeps running"
            if not alive:
                sub += " · stopped"
            row = Adw.ActionRow(title=esc(title), subtitle=esc(sub))
            row.add_css_class("mono-title")
            row.add_prefix(Gtk.Image(icon_name="network-wireless-symbolic", css_classes=["share-live"] if alive else []))
            if url:
                row.add_suffix(icon_button("adw-external-link-symbolic", "Open", lambda _b=None, u=url: open_uri(u)))
                row.add_suffix(icon_button("edit-copy-symbolic", "Copy link",
                                           lambda _b=None, u=url: (self.get_clipboard().set(u), self.win.toast("Copied"))))
                if host_which("qrencode"):
                    row.add_suffix(icon_button("view-grid-symbolic", "QR code", lambda _b=None, u=url: self._qr(u)))
            stop = Gtk.Button(label="Stop sharing", valign=Gtk.Align.CENTER)
            stop.add_css_class("destructive-action")
            stop.connect("clicked", lambda _b, cc=c: self._stop_tunnel(cc))
            row.add_suffix(stop)
            self.g_public.add(row)
            self.public_rows.append(row)
            if alive and not url and not token:
                bg(sharing.tunnel_url, self.store.engine, c["Id"],
                   done=lambda u, k=key: u and self.urls.get(k) != u and (self.urls.__setitem__(k, u), self.on_show()),
                   error=lambda e: None)
        if not tun:
            row = Adw.ActionRow(title="Nothing is shared",
                                subtitle="Share a demo or a webhook endpoint with a public https link")
            b = Gtk.Button(label="Share…", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_: ShareDialog(self).present(self.win))
            row.add_suffix(b)
            self.g_public.add(row)
            self.public_rows.append(row)

        for r in self.port_rows:
            self.g_ports.remove(r)
        self.port_rows = []
        for p in sharing.published(cs):
            shown_ip = "0.0.0.0" if p["lan"] else p["ip"]
            row = Adw.ActionRow(title=esc(f"localhost:{p['public']}  →  {p['container']}:{p['private']}"),
                                subtitle=esc(f"⚠ open to your whole network ({shown_ip})" if p["lan"]
                                             else f"this computer only ({shown_ip})"))
            row.add_css_class("mono-title")
            row.add_suffix(icon_button("adw-external-link-symbolic", f"Open localhost:{p['public']}",
                                       lambda _b=None, n=p["public"]: open_uri(f"http://localhost:{n}")))
            a = Gtk.Button(label="Address", valign=Gtk.Align.CENTER, tooltip_text="Give it a name.localhost address")
            a.add_css_class("flat")
            a.connect("clicked", lambda _b, q=p: ExposeDialog(self, q["id"], q["private"]).present(self.win))
            row.add_suffix(a)
            s = Gtk.Button(label="Share", valign=Gtk.Align.CENTER, tooltip_text="Share publicly")
            s.add_css_class("flat")
            s.connect("clicked", lambda _b, q=p: ShareDialog(self, q["id"], q["private"]).present(self.win))
            row.add_suffix(s)
            self.g_ports.add(row)
            self.port_rows.append(row)
        if not self.port_rows:
            row = Adw.ActionRow(title="No published ports")
            self.g_ports.add(row)
            self.port_rows.append(row)
        n = sum(1 for c in tun if c.get("State") == "running")
        self.header.set_subtitle(f"{len(self.routes())} local address{'es' if len(self.routes()) != 1 else ''} · "
                                 f"{n} public link{'s' if n != 1 else ''} · {len(sharing.published(cs))} ports in use")
        self._reapply(running, p)

    def _reapply(self, running, p):
        """Targets on the default bridge get a new IP when they restart: re-point the proxy when that happens."""
        routes = self.routes()
        if not (running and routes) or self._applying:
            return
        self._applying = True
        eng = self.store.engine

        def work():
            sig = sharing.signature(eng, routes)
            if sig != self._sig:
                sharing.apply_routes(eng, routes, p["Id"])
            return sig

        def done(sig):
            self._applying = False
            self._sig = sig

        def fail(_e):
            self._applying = False
        bg(work, done=done, error=fail)
