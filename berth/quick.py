"""One-click everyday changes: rename containers / images / volumes, change a container's ports.
Docker can only rename containers natively; the others are done safely here (see each function)."""
import re

from gi.repository import Adw, Gtk

from .util import bg, cname, label


def _ask(win, heading, body, initial, ok, then, check=None, keep_ok=False):
    d = Adw.AlertDialog(heading=heading, body=body)
    e = Gtk.Entry(text=initial, activates_default=True)
    e.add_css_class("monospace")
    hint = label("", css=["caption", "error"], wrap=True)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.append(e)
    box.append(hint)
    d.set_extra_child(box)
    d.add_response("cancel", "Cancel")
    d.add_response("ok", ok)
    d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("ok")

    def validate(*_):
        t = e.get_text().strip()
        same = t == initial and not keep_ok
        msg = (check(t) if check else None) if t and not same else ("" if same else "Type a name")
        hint.set_text(msg or "")
        d.set_response_enabled("ok", bool(t) and not same and not msg)
    e.connect("changed", validate)
    validate()
    d.connect("response", lambda _d, r: r == "ok" and then(e.get_text().strip()))
    d.present(win)
    e.grab_focus()


NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


# ── containers ──────────────────────────────────────────────────────────────────────────────────────────────────
def rename_container(win, store, cid):
    c = store.by_id(cid)
    old = cname(c) if c else cid[:12]
    taken = {cname(x) for x in store.containers}

    def check(t):
        if not NAME_RE.match(t):
            return "Letters, digits, and . _ - (must start with a letter or digit)"
        return f"“{t}” is already used by another container" if t in taken else None

    def go(new):
        bg(store.engine.rename, cid, new, done=lambda _r: (win.toast(f"Renamed to {new}"), store.refresh("containers")),
           error=lambda e: win.toast(f"Not renamed: {e}"))
    note = ""
    if c and (c.get("Labels") or {}).get("com.docker.compose.project"):
        note = "\n\nIt belongs to a Compose project: the next “compose up” recreates it under its Compose name."
    _ask(win, "Rename container", f"Instant, no restart.{note}", old, "Rename", go, check)


class PortsDialog(Adw.Dialog):
    """Change the published ports of a container (recreates it; volumes and data are kept)."""

    def __init__(self, win, store, cid):
        super().__init__(title="Change ports", content_width=560, content_height=560)
        from .edit import values_from
        self.win, self.store, self.cid = win, store, cid
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.ok = Gtk.Button(label="Apply")
        self.ok.add_css_class("suggested-action")
        self.ok.connect("clicked", lambda *_: self._apply())
        hb.pack_end(self.ok)
        tv.add_top_bar(hb)
        self.page = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup(
            description="Host port → port inside the container. “This computer only” binds to 127.0.0.1, so other "
                        "devices on your network can't reach it — the safe choice for databases.")
        self.page.add(self.group)
        self.warn = label("", css=["caption", "warning"], wrap=True, margin_start=12, margin_end=12)
        wg = Adw.PreferencesGroup()
        wg.add(self.warn)
        self.page.add(wg)
        tv.set_content(self.page)
        self.set_child(tv)
        self.rows = []
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6)
        self.group.add(self.box)
        add = Gtk.Button(label="Add a port", halign=Gtk.Align.START)
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self._row("", "", True))
        self.group.add(add)
        self.exposed = []
        self._own = set()

        def got(d):
            self.d = d
            v = values_from(d)
            self.orig = v["ports"]
            self._own = {h.rpartition(":")[2] for h, _c in v["ports"]}       # its own ports are in use by itself
            self.exposed = sorted(k.split("/")[0] for k in ((d.get("Config") or {}).get("ExposedPorts") or {}))
            self.set_title(f"Ports of {v['name']}")
            for host, ctr in v["ports"]:
                ip, _, port = host.rpartition(":")
                self._row(port, ctr, ip in ("127.0.0.1", "::1"))
            if not v["ports"]:
                for p in self.exposed[:3]:
                    self._row("", p, True)
            self._check()
        bg(store.engine.inspect, cid, done=got, error=lambda e: win.toast(str(e)))

    def _row(self, host, ctr, local):
        line = Gtk.Box(spacing=6)
        eh = Gtk.Entry(text=host, placeholder_text="host port", width_chars=8, input_purpose=Gtk.InputPurpose.DIGITS)
        ec = Gtk.Entry(text=ctr, placeholder_text="container port", width_chars=10)
        for e in (eh, ec):
            e.add_css_class("monospace")
            e.connect("changed", lambda *_: self._check())
        scope = Gtk.DropDown(model=Gtk.StringList.new(["This computer only", "Whole network"]), valign=Gtk.Align.CENTER)
        scope.set_selected(0 if local else 1)
        scope.connect("notify::selected", lambda *_: self._check())
        rm = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Remove", valign=Gtk.Align.CENTER)
        rm.add_css_class("flat")
        line.append(eh)
        line.append(Gtk.Label(label="→", css_classes=["dim-label"]))
        line.append(ec)
        line.append(scope)
        line.append(rm)
        entry = (line, eh, ec, scope)
        self.rows.append(entry)
        rm.connect("clicked", lambda *_: (self.rows.remove(entry), self.box.remove(line), self._check()))
        self.box.append(line)

    def _ports(self):
        out = []
        for _l, eh, ec, scope in self.rows:
            h, c = eh.get_text().strip(), ec.get_text().strip()
            if h or c:
                out.append(((("127.0.0.1:" if scope.get_selected() == 0 else "") + h), c))
        return out

    def _check(self):
        msgs, ok = [], True
        used = {}
        for c in self.store.containers:
            if c["Id"] == self.cid:
                continue
            for p in c.get("Ports") or []:
                if p.get("PublicPort"):
                    used[str(p["PublicPort"])] = cname(c)
        seen = set()
        for host, ctr in self._ports():
            port = host.rpartition(":")[2]
            if not (port.isdigit() and 0 < int(port) < 65536) or not re.match(r"^\d+(/(tcp|udp))?$", ctr):
                msgs.append(f"“{host or '?'} → {ctr or '?'}” isn't a valid pair of ports")
                ok = False
            elif port in used:
                msgs.append(f"Host port {port} is already used by {used[port]}")
                ok = False
            elif port not in self._own and not _port_free(host):
                msgs.append(f"Host port {port} is already used by another program on this computer")
                ok = False
            elif (port, ctr.endswith("/udp")) in seen:
                msgs.append(f"Host port {port} appears twice")
                ok = False
            seen.add((port, ctr.endswith("/udp")))
        self.warn.set_text("\n".join(msgs))
        self.ok.set_sensitive(ok and hasattr(self, "orig") and self._ports() != self.orig)

    def _apply(self):
        from .edit import change_ports
        ports = self._ports()
        self.ok.set_sensitive(False)
        self.win.toast("Recreating the container with the new ports…")

        def done(new_id):
            self.win.toast("Ports changed")
            self.store.refresh("containers")
            self.close()

        def fail(e):
            self.ok.set_sensitive(True)
            self.win.toast(f"Ports not changed (the old container is back): {e}")
        bg(change_ports, self.store.engine, self.cid, ports, done=done, error=fail)


def _port_free(host):
    """Can something bind this host port right now? (finds programs outside Docker that hold it)"""
    import socket
    ip, _, port = host.rpartition(":")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((ip or "0.0.0.0", int(port)))
        s.close()
        return True
    except OSError as e:
        return e.errno == 13            # permission (port < 1024 as a user): can't tell, let Docker decide


# ── images ──────────────────────────────────────────────────────────────────────────────────────────────────────
def rename_image(win, store, ref, image_id):
    """Docker has no image rename: tag the image with the new name, then remove only the old tag (the image and
    anything using it stay)."""
    def check(t):
        if not re.match(r"^[a-z0-9]+([._/-][a-z0-9]+)*(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)*(:[\w][\w.-]{0,127})?$", t):
            return "Use lower-case repository[:tag], e.g. myapp:1.2 or registry.local:5000/team/app:dev"
        return None

    def go(new):
        repo, tag = (new.rsplit(":", 1) if ":" in new.rsplit("/", 1)[-1] else (new, "latest"))

        def work():
            store.engine.tag(image_id, repo, tag)
            if ref != image_id and ref != f"{repo}:{tag}":
                store.engine.remove_image(ref)          # untags only: the new tag keeps the image alive
            return f"{repo}:{tag}"
        bg(work, done=lambda n: (win.toast(f"Renamed to {n}"), store.refresh("images")),
           error=lambda e: win.toast(f"Not renamed: {e}"))
    _ask(win, "Rename image", "Gives the image the new name and removes the old one. Containers that already use it "
         "keep running.", ref if ref != image_id else "", "Rename", go, check)


# ── volumes ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _users(store, volume):
    from .files import HELPER_LABEL
    return [c for c in store.containers for m in (c.get("Mounts") or [])
            if m.get("Type") == "volume" and m.get("Name") == volume and HELPER_LABEL not in (c.get("Labels") or {})]


def rename_volume(win, store, volume, on_done=None):
    """Docker has no volume rename. Berth: stops the containers that use it, copies the data into a new volume,
    recreates those containers on the new volume, starts the ones that were running, and only then deletes the old
    volume. If anything fails before the old volume is deleted, nothing is lost."""
    users = _users(store, volume)
    names = sorted({cname(c) for c in users})
    taken = {v["Name"] for v in (store.volumes or [])}
    compose = any((c.get("Labels") or {}).get("com.docker.compose.project") for c in users)

    def check(t):
        if not NAME_RE.match(t):
            return "Letters, digits, and . _ - (must start with a letter or digit)"
        return f"A volume called “{t}” already exists" if t in taken else None

    body = "The data is copied to a volume with the new name, then the old one is deleted."
    if names:
        body += (f"\n\nUsed by {', '.join(names)}: they stop for a moment, are switched to the new volume and "
                 "started again.")
    if compose:
        body += "\n\nCompose projects use the volume name from the compose file: update it there too."

    def go(new):
        from .edit import repoint_volume
        from .files import clone_volume

        def work():
            eng = store.engine
            running = [c["Id"] for c in users if c.get("State") == "running"]
            for cid in running:
                eng.action(cid, "stop")
            try:
                clone_volume(eng, volume, new)
            except Exception:
                for cid in running:
                    eng.action(cid, "start")
                raise
            new_ids = {}
            for c in {c["Id"]: c for c in users}.values():
                new_ids[c["Id"]] = repoint_volume(eng, c["Id"], volume, new)
            for cid in running:
                eng.action(new_ids.get(cid, cid), "start")
            eng.remove_volume(volume)
            return new
        win.toast(f"Renaming {volume}…")
        bg(work, done=lambda n: (win.toast(f"Volume renamed to {n}"), store.refresh("volumes"),
                                 store.refresh("containers"), on_done and on_done(n)),
           error=lambda e: (win.toast(f"Not renamed: {e}"), store.refresh("volumes"), store.refresh("containers")))
    _ask(win, "Rename volume", body, volume, "Rename", go, check)



def clone_volume_ask(win, store, volume):
    from .files import clone_volume
    taken = {v["Name"] for v in (store.volumes or [])}

    def check(t):
        if not NAME_RE.match(t):
            return "Letters, digits, and . _ - (must start with a letter or digit)"
        return f"A volume called “{t}” already exists" if t in taken else None

    def go(new):
        win.toast(f"Copying {volume}…")
        bg(clone_volume, store.engine, volume, new, done=lambda _r: (win.toast(f"Cloned to {new}"),
                                                                     store.refresh("volumes")),
           error=lambda e: win.toast(f"Not cloned: {e}"))
    _ask(win, "Clone volume", "Copies every file into a new volume. The original stays as it is.", f"{volume}-copy",
         "Clone", go, check, keep_ok=True)
