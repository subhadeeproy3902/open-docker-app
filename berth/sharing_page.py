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


EXPIRY = [("Never", None), ("After 15 minutes", 900), ("After 1 hour", 3600), ("After 4 hours", 4 * 3600),
          ("After 1 day", 86400), ("After 1 week", 7 * 86400), ("At a date and time…", "custom")]


def _left(t):
    import time as _t
    s = int(t - _t.time())
    if s <= 0:
        return "expiring now"
    if s < 3600:
        return f"stops in {max(1, s // 60)} min"
    if s < 86400:
        return f"stops in {s // 3600} h {(s % 3600) // 60:02d} min"
    import datetime
    return "stops " + datetime.datetime.fromtimestamp(t).strftime("%a %d %b, %H:%M")


class ExposeDialog(Adw.Dialog):
    def __init__(self, page, cid=None, port=None, route=None):
        super().__init__(title="Change local address" if route else "Add a local address", content_width=520)
        self.page, self.win, self.store, self.editing = page, page.win, page.store, route
        if route:
            c0 = next((c for c in page.store.containers if cname(c) == route["container"]), None)
            cid, port = (c0["Id"] if c0 else None), int(route["port"])
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.ok = Gtk.Button(label="Save" if route else "Add")
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
        self.lan = Adw.SwitchRow(title="Phones on this Wi-Fi too",
                                 subtitle="Also opens it at this computer's Wi-Fi address (with a QR code). Only this "
                                          "address is opened to the network.")
        g.add(self.lan)
        if route:
            self.name.set_text(route["name"])
            self.https.set_active(bool(route.get("https")))
            self.lan.set_active(bool(route.get("lan")))
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
        others = [r for r in self.page.routes() if not (self.editing and r["name"] == self.editing["name"])]
        err = sharing.validate_name(name, others)
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
                 "https": self.https.get_active(), "lan": self.lan.get_active()}
        if self.editing and self.editing.get("lan_port") and route["lan"]:
            route["lan_port"] = self.editing["lan_port"]
        self.ok.set_sensitive(False)
        self.page.add_route(route, done=self.close, fail=lambda: self.ok.set_sensitive(True),
                            replaces=self.editing["name"] if self.editing else None)


class ShareDialog(Adw.Dialog):
    def __init__(self, page, cid=None, port=None):
        super().__init__(title="Share publicly", content_width=540)
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
        self.expiry = Adw.ComboRow(title="Stop sharing automatically",
                                   model=Gtk.StringList.new([lbl for lbl, _s in EXPIRY]))
        self.expiry.set_selected(2)
        self.expiry.connect("notify::selected", lambda *_: self._expiry_changed())
        h.add(self.expiry)
        self.until = Adw.EntryRow(title="Until (YYYY-MM-DD HH:MM, local time)", visible=False)
        h.add(self.until)
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
        try:
            expires = self._expires()
        except ValueError as e:
            self.ok.set_sensitive(True)
            self.status.set_text(str(e))
            return
        bg(sharing.start_tunnel, self.store.engine, c["Id"], port, token, self.keep.get_active(), progress,
           expires, done=done, error=fail)

    def _expiry_changed(self):
        self.until.set_visible(EXPIRY[self.expiry.get_selected()][1] == "custom")

    def _expires(self):
        import datetime
        import time as _t
        sec = EXPIRY[self.expiry.get_selected()][1]
        if sec is None:
            return None
        if sec == "custom":
            try:
                when = datetime.datetime.strptime(self.until.get_text().strip(), "%Y-%m-%d %H:%M").timestamp()
            except ValueError:
                raise ValueError("Type the end as YYYY-MM-DD HH:MM, e.g. 2026-11-12 18:00")
            if when <= _t.time() + 30:
                raise ValueError("The end time is in the past")
            return int(when)
        return int(_t.time() + sec)


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
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, css_classes=["page-body", "sharing-page"])
        tiles = Gtk.FlowBox(homogeneous=True, min_children_per_line=2, max_children_per_line=4, column_spacing=12,
                            row_spacing=12, selection_mode=Gtk.SelectionMode.NONE)
        self.tiles = {}
        self.tiles_box = tiles
        for key, title in (("local", "Local addresses"), ("public", "Public links"), ("ports", "Ports in use"),
                           ("lan", "Open to whole network")):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["card", "tile"])
            card.append(label(title.upper(), css=["caption", "dim-label", "tile-k"]))
            v = label("0", css=["tile-v", "numeric"])
            sub_ = label("", css=["caption", "dim-label"])
            card.append(v)
            card.append(sub_)
            self.tiles[key] = (card, v, sub_)
            tiles.append(card)
        body.append(tiles)
        cols = Gtk.FlowBox(homogeneous=True, min_children_per_line=1, max_children_per_line=2, column_spacing=18,
                           row_spacing=18, selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START)
        body.append(cols)
        self.pp = cols
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
        self.pp.append(self.g_local)
        self.local_rows = []
        # public links
        self.g_public = Adw.PreferencesGroup(title="Public links",
                                             description="Anyone with the link can open these. Each one stops when "
                                                         "Berth quits, unless you chose to keep it.")
        self.public_pill = label("", css=["status-pill", "muted"], ellipsize=False, valign=Gtk.Align.CENTER)
        self.g_public.set_header_suffix(self.public_pill)   # mirrors the proxy pill of the other column
        self.pp.append(self.g_public)
        self.public_rows = []
        # ports in use: one line per port, full width
        head = Gtk.Box(spacing=12)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        col.append(label("Ports in use", css=["heading"]))
        col.append(label("Every published port. “Whole network” means other devices on your Wi-Fi can reach it — "
                         "fine for a web app, risky for a database.", css=["dim-label", "caption"], wrap=True))
        head.append(col)
        self.port_find = Gtk.SearchEntry(placeholder_text="Filter ports", valign=Gtk.Align.CENTER, width_chars=22)
        self.port_find.connect("search-changed", lambda *_: self._ports_filter.changed(Gtk.FilterChange.DIFFERENT))
        head.append(self.port_find)
        body.append(head)
        self._build_ports_table(body)
        tv.set_content(Gtk.ScrolledWindow(child=body, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True))
        self.append(tv)
        store.connect("containers", lambda *_: self.on_show())
        self.on_show()

    def on_size(self, n):
        from .util import per_size, set_columns
        set_columns(self.tiles_box, per_size(n, 2, 2, 4, 4, 4, 4))
        set_columns(self.pp, per_size(n, 1, 1, 2, 2, 2, 2))

    def _build_ports_table(self, body):
        from gi.repository import Gio, GObject, Pango
        from .util import table_card

        class _P(GObject.Object):
            def __init__(self, d):
                super().__init__()
                self.d = d
        self._P = _P
        self.port_model = Gio.ListStore(item_type=_P)
        q = lambda: self.port_find.get_text().strip().lower()
        self._ports_filter = Gtk.CustomFilter.new(
            lambda it, _d: not q() or q() in f"{it.d['public']} {it.d['container']} {it.d['private']}".lower(), None)
        view = Gtk.ColumnView(model=Gtk.NoSelection(model=Gtk.FilterListModel(model=self.port_model,
                                                                               filter=self._ports_filter)),
                              show_row_separators=True)
        view.add_css_class("berth-table")
        view.add_css_class("data-table")

        def col(title, make, upd, expand=False, width=-1):
            f = Gtk.SignalListItemFactory()
            f.connect("setup", lambda _f, li: li.set_child(make()))
            f.connect("bind", lambda _f, li: upd(li.get_child(), li.get_item().d))
            c = Gtk.ColumnViewColumn(title=title, factory=f, expand=expand, resizable=True)
            if width > 0:
                c.set_fixed_width(width)
            view.append_column(c)

        def lab(css=()):
            return lambda: label("", css=list(css))
        col("Port on this computer", lab(["monospace", "heading-sm"]),
            lambda w, d: w.set_text(f"localhost:{d['public']}"), width=190)
        col("Container", lambda: label("", ellipsize=True), lambda w, d: w.set_text(d["container"]), expand=True)
        col("Inside", lab(["monospace", "dim-label"]), lambda w, d: w.set_text(str(d["private"])), width=90)

        def badge_upd(w, d):
            w.set_text("Whole network ⚠" if d["lan"] else "This computer only")
            w.set_css_classes(["status-pill", "warn" if d["lan"] else "ok"])
            w.set_tooltip_text(f"Bound to {d['ip']}")
        col("Who can reach it", lambda: label("", ellipsize=False, halign=Gtk.Align.START), badge_upd, width=180)

        def acts():
            box = Gtk.Box(spacing=2, halign=Gtk.Align.END)
            box.d = None
            from . import quick
            for icon, tip, fn in (
                    ("adw-external-link-symbolic", "Open in the browser",
                     lambda d: open_uri(f"http://localhost:{d['public']}")),
                    ("document-edit-symbolic", "Change the port, or who can reach it",
                     lambda d: quick.PortsDialog(self.win, self.store, d["id"]).present(self.win)),
                    ("network-server-symbolic", "Give it a name.localhost address",
                     lambda d: ExposeDialog(self, d["id"], d["private"]).present(self.win)),
                    ("network-workgroup-symbolic", "Share publicly (Cloudflare)",
                     lambda d: ShareDialog(self, d["id"], d["private"]).present(self.win))):
                b = icon_button(icon, tip)
                b.connect("clicked", lambda _b, f=fn, bx=box: bx.d and f(bx.d))
                box.append(b)
            return box

        def acts_upd(box, d):
            box.d = d
        col("", acts, acts_upd, width=150)
        card = table_card(view)
        sw = card.get_first_child()
        sw.set_propagate_natural_height(True)               # the page scrolls, not a small box inside it
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        sw.set_vexpand(False)
        card.set_vexpand(False)
        body.append(card)

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
            pid = sharing.start_proxy(self.store.engine, ports, lan=sharing.lan_ports(routes))
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
    def add_route(self, route, done=None, fail=None, replaces=None):
        others = [r for r in self.routes() if r["name"] not in (route["name"], replaces)]
        if not route.get("lan"):
            route.pop("lan_port", None)
        sharing.assign_lan_port(others, route)
        routes = others + [route]
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
            pid = sharing.start_proxy(self.store.engine, ports, lan=sharing.lan_ports(routes))
            return sharing.apply_routes(self.store.engine, routes, pid)
        bg(work, done=lambda _u: (self.store.refresh("containers"), ok()), error=bad)

    def remove_route(self, name):
        routes = [r for r in self.routes() if r["name"] != name]
        self.settings["routes"] = routes
        p = sharing.proxy(self.store.containers)
        if p and p.get("State") == "running":
            ports, eng = self.proxy_ports(), self.store.engine

            def work():       # recreate if a phone port must close, then reload the routes
                pid = sharing.start_proxy(eng, ports, lan=sharing.lan_ports(routes))
                return sharing.apply_routes(eng, routes, pid)
            bg(work, done=lambda _r: self.store.refresh("containers"), error=lambda e: self.win.toast(str(e)))
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
        from .send_page import _qr_picture
        d = Adw.Dialog(title="Scan to open", content_width=360, content_height=440)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=8, margin_bottom=20,
                      margin_start=20, margin_end=20)
        pic = _qr_picture(url, 280)
        pic.set_vexpand(True)
        box.append(pic)
        box.append(label(url, css=["monospace"], xalign=0.5, selectable=True, wrap=True))
        box.append(label("Point your phone's camera at the code. It must be on the same Wi-Fi unless this is a public "
                         "link.", css=["caption", "dim-label"], xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER))
        tv.set_content(box)
        d.set_child(tv)
        d.present(self.win)

    # ── refresh ──
    def on_show(self):
        cs = self.store.containers
        p = sharing.proxy(cs)
        running = bool(p and p.get("State") == "running")
        if not self.get_mapped() and self.local_rows:     # hidden: only keep the routes pointing at the right place
            return self._reapply(running, p)
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
            phone = sharing.lan_url(r) if r.get("lan") and r.get("lan_port") else None
            row = Adw.ActionRow(title=esc(url), subtitle=esc(f"→ {r['container']}:{r['port']}" +
                                                             ("" if alive else " · not running") +
                                                             (f" · phones: {phone}" if phone else "")))
            if phone:
                row.add_suffix(icon_button("phone-symbolic", "Open on your phone (QR code)",
                                           lambda _b=None, u=phone: self._qr(u)))
            row.add_css_class("mono-title")
            ob = icon_button("adw-external-link-symbolic", "Open", lambda _b=None, u=url: open_uri(u))
            ob.set_sensitive(running and alive)
            row.add_suffix(ob)
            row.add_suffix(icon_button("edit-copy-symbolic", "Copy address",
                                       lambda _b=None, u=url: (self.get_clipboard().set(u), self.win.toast("Copied"))))
            row.add_suffix(icon_button("document-edit-symbolic", "Change name, container or port",
                                       lambda _b=None, rr=dict(r): ExposeDialog(self, route=rr).present(self.win)))
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
            exp = sharing.expires_at(c)
            if exp:
                sub += f" · {_left(exp)}"
            if not alive:
                sub += " · stopped"
            row = Adw.ActionRow(title=esc(title), subtitle=esc(sub))
            row.add_css_class("mono-title")
            row.add_prefix(Gtk.Image(icon_name="network-wireless-symbolic", css_classes=["share-live"] if alive else []))
            if url:
                row.add_suffix(icon_button("adw-external-link-symbolic", "Open", lambda _b=None, u=url: open_uri(u)))
                row.add_suffix(icon_button("edit-copy-symbolic", "Copy link",
                                           lambda _b=None, u=url: (self.get_clipboard().set(u), self.win.toast("Copied"))))
                row.add_suffix(icon_button("phone-symbolic", "QR code for your phone", lambda _b=None, u=url: self._qr(u)))
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

        pub = sharing.published(cs)
        self.port_model.splice(0, self.port_model.get_n_items(), [self._P(p) for p in pub])
        nlive = sum(1 for c in tun if c.get("State") == "running")
        self.public_pill.set_text(f"{nlive} live" if nlive else "Nothing public")
        self.public_pill.set_css_classes(["status-pill", "warn" if nlive else "muted"])
        self.public_pill.set_tooltip_text("Links reachable from the internet right now")
        lan = [p for p in pub if p["lan"]]
        nloc = len(self.routes())
        phones = sum(1 for r in self.routes() if r.get("lan"))
        live = sum(1 for c in tun if c.get("State") == "running")
        for key, val, note in (("local", nloc, (f"{phones} open to phones · " if phones else "") +
                                ("proxy running" if running else "proxy off")),
                               ("public", live, "on the internet now" if live else "nothing public"),
                               ("ports", len(pub), f"{len({p['container'] for p in pub})} containers"),
                               ("lan", len(lan), "reachable from your Wi-Fi" if lan else "all private")):
            card, v, s_ = self.tiles[key]
            v.set_text(str(val))
            s_.set_text(note)
            (card.add_css_class if key == "lan" and lan else card.remove_css_class)("tile-warn")
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
