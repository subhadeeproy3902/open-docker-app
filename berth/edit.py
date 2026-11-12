"""Edit a container.

Docker can change a few things on a running container (name, restart policy, CPU/memory limits).
Everything else (environment, ports, volumes, command, image) needs a new container. Berth does that
safely: stop the old one, rename it aside, create + start the replacement with the same networks,
labels and volumes, and only then delete the old one. Any failure puts the old container back."""
import shlex
import time

from gi.repository import Adw, GLib, Gtk

from .util import bg, cname, esc, labels


def _kv_rows(group, items, placeholder_k, placeholder_v, sep):
    """Editable list of key/value rows inside a PreferencesGroup; returns a getter."""
    rows = []
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

    def add(k="", v=""):
        line = Gtk.Box(spacing=6)
        ek = Gtk.Entry(text=k, placeholder_text=placeholder_k, hexpand=True)
        ev = Gtk.Entry(text=v, placeholder_text=placeholder_v, hexpand=True)
        for e in (ek, ev):
            e.add_css_class("monospace")
        rm = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Remove")
        rm.add_css_class("flat")
        line.append(ek)
        line.append(Gtk.Label(label=sep, css_classes=["dim-label"]))
        line.append(ev)
        line.append(rm)
        entry = (line, ek, ev)
        rows.append(entry)
        rm.connect("clicked", lambda *_: (rows.remove(entry), box.remove(line)))
        box.append(line)
    for k, v in items:
        add(k, v)
    addb = Gtk.Button(label="Add", halign=Gtk.Align.START)
    addb.add_css_class("flat")
    addb.connect("clicked", lambda *_: add())
    wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6)
    wrap.append(box)
    wrap.append(addb)
    group.add(wrap)
    return lambda: [(ek.get_text().strip(), ev.get_text().strip()) for _l, ek, ev in rows if ek.get_text().strip()]


class EditDialog(Adw.Dialog):
    def __init__(self, win, store, cid):
        super().__init__(title="Edit container", content_width=680, content_height=760)
        self.win, self.store, self.cid = win, store, cid
        self.tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.apply_btn = Gtk.Button(label="Apply", sensitive=False)
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", lambda *_: self._apply())
        hb.pack_start(cancel)
        hb.pack_end(self.apply_btn)
        self.tv.add_top_bar(hb)
        self.tv.set_content(Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=32, height_request=32))
        self.set_child(self.tv)
        bg(store.engine.inspect, cid, done=self._build, error=lambda e: (win.toast(str(e)), self.close()))

    def _build(self, d):
        self.d = d
        cfg, hc = d.get("Config") or {}, d.get("HostConfig") or {}
        pp = Adw.PreferencesPage()
        compose = labels({"Labels": cfg.get("Labels")}).get("com.docker.compose.project")
        if compose:
            note = Adw.PreferencesGroup()
            row = Adw.ActionRow(title="Part of Compose project " + esc(compose),
                                subtitle="Changes that recreate the container last until the next <tt>docker compose up</tt>, "
                                         "which rebuilds it from the compose file. Edit the compose file to make them permanent.")
            row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
            note.add(row)
            pp.add(note)

        live = Adw.PreferencesGroup(title="Applies instantly", description="No restart needed.")
        self.name = Adw.EntryRow(title="Name", text=d.get("Name", "").lstrip("/"))
        policies = ["no", "unless-stopped", "always", "on-failure"]
        self.restart = Adw.ComboRow(title="Restart policy", model=Gtk.StringList.new(policies))
        cur = (hc.get("RestartPolicy") or {}).get("Name") or "no"
        self.restart.set_selected(policies.index(cur) if cur in policies else 0)
        self.mem = Adw.SpinRow.new_with_range(0, 1024 * 256, 64)
        self.mem.set_title("Memory limit (MB)")
        self.mem.set_subtitle("0 = no limit")
        self.mem.set_value((hc.get("Memory") or 0) / 1024 / 1024)
        self.cpus = Adw.SpinRow.new_with_range(0, 256, 0.25)
        self.cpus.set_digits(2)
        self.cpus.set_title("CPU limit (cores)")
        self.cpus.set_subtitle("0 = no limit")
        self.cpus.set_value((hc.get("NanoCpus") or 0) / 1e9)
        for w in (self.name, self.restart, self.mem, self.cpus):
            live.add(w)
        pp.add(live)

        rec = Adw.PreferencesGroup(title="Recreates the container",
                                   description="Berth stops it, starts a replacement with your changes, and keeps the old one until "
                                               "the new one runs. Volumes and their data are kept.")
        self.image = Adw.EntryRow(title="Image", text=cfg.get("Image", ""))
        self.cmd = Adw.EntryRow(title="Command (empty = image default)",
                                text=shlex.join(cfg.get("Cmd") or []) if cfg.get("Cmd") else "")
        self.workdir = Adw.EntryRow(title="Working directory", text=cfg.get("WorkingDir") or "")
        for w in (self.image, self.cmd, self.workdir):
            rec.add(w)
        pp.add(rec)

        env_g = Adw.PreferencesGroup(title="Environment variables")
        env = [(e.partition("=")[0], e.partition("=")[2]) for e in (cfg.get("Env") or [])]
        self.get_env = _kv_rows(env_g, env, "NAME", "value", "=")
        pp.add(env_g)

        ports_g = Adw.PreferencesGroup(title="Ports", description="Host port → container port (add /udp for UDP).")
        ports = []
        for k, binds in (hc.get("PortBindings") or {}).items():
            for b in binds or [{}]:
                ports.append((b.get("HostPort", ""), k.replace("/tcp", "")))
        self.get_ports = _kv_rows(ports_g, ports, "host port", "container port", "→")
        pp.add(ports_g)

        vol_g = Adw.PreferencesGroup(title="Volumes and folders",
                                     description="Volume name or host folder → path inside the container. Add :ro for read-only.")
        mounts = []
        for m in d.get("Mounts") or []:
            if m.get("Type") in ("volume", "bind"):
                src = m.get("Name") if m.get("Type") == "volume" else m.get("Source")
                mounts.append((src, m.get("Destination", "") + ("" if m.get("RW", True) else ":ro")))
        self.get_mounts = _kv_rows(vol_g, mounts, "volume or /host/path", "/path/in/container", "→")
        pp.add(vol_g)

        self.net_g = Adw.PreferencesGroup(title="Networks", description="Connect or disconnect right away (no restart).")
        pp.add(self.net_g)
        self.net_rows = []
        self._fill_networks(d)

        self.orig = self._values()
        self.tv.set_content(pp)
        self.apply_btn.set_sensitive(True)

    def _fill_networks(self, d):
        for r in self.net_rows:
            self.net_g.remove(r)
        self.net_rows = []
        mine = (d.get("NetworkSettings") or {}).get("Networks") or {}
        for n, ep in mine.items():
            r = Adw.ActionRow(title=esc(n), subtitle=esc(ep.get("IPAddress") or ""))
            if n not in ("host", "none"):
                b = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Disconnect", valign=Gtk.Align.CENTER)
                b.add_css_class("flat")
                b.connect("clicked", lambda *_, net=n: self._net("disconnect", net))
                r.add_suffix(b)
            self.net_g.add(r)
            self.net_rows.append(r)

        def got(nets):
            others = sorted(x["Name"] for x in nets if x["Name"] not in mine and x["Name"] not in ("host", "none"))
            if not others:
                return
            row = Adw.ActionRow(title="Connect to")
            dd = Gtk.DropDown.new_from_strings(others)
            dd.set_valign(Gtk.Align.CENTER)
            go = Gtk.Button(label="Connect", valign=Gtk.Align.CENTER)
            go.connect("clicked", lambda *_: self._net("connect", others[dd.get_selected()]))
            row.add_suffix(dd)
            row.add_suffix(go)
            self.net_g.add(row)
            self.net_rows.append(row)
        bg(self.store.engine.networks, done=got, error=lambda e: None)

    def _net(self, verb, net):
        fn = self.store.engine.connect_network if verb == "connect" else self.store.engine.disconnect_network

        def done(_r):
            self.win.toast(f"{'Connected to' if verb == 'connect' else 'Disconnected from'} {net}")
            self.store.refresh("containers")
            bg(self.store.engine.inspect, self.cid, done=lambda d: (setattr(self, "d", d), self._fill_networks(d)))
        bg(fn, net, self.cid, done=done, error=lambda e: self.win.toast(str(e)))

    def _values(self):
        return {"name": self.name.get_text().strip(), "restart": self.restart.get_selected_item().get_string(),
                "mem": int(self.mem.get_value()), "cpus": round(self.cpus.get_value(), 2),
                "image": self.image.get_text().strip(), "cmd": self.cmd.get_text().strip(),
                "workdir": self.workdir.get_text().strip(), "env": self.get_env(), "ports": self.get_ports(),
                "mounts": self.get_mounts()}

    def _apply(self):
        v = self._values()
        changed = {k for k in v if v[k] != self.orig[k]}
        if not changed:
            self.close()
            return
        recreate = bool(changed & {"image", "cmd", "workdir", "env", "ports", "mounts"})
        if recreate:
            d = Adw.AlertDialog(heading="Recreate the container?",
                                body="It stops briefly while the replacement starts. Its volumes and data are kept; "
                                     "files changed inside the container itself (not in a volume) are lost.")
            d.add_response("cancel", "Cancel")
            d.add_response("ok", "Recreate")
            d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
            d.connect("response", lambda _d, r: r == "ok" and self._run(v, changed, True))
            d.present(self.win)
        else:
            self._run(v, changed, False)

    def _run(self, v, changed, recreate):
        self.apply_btn.set_sensitive(False)
        self.win.toast("Recreating the container…" if recreate else "Applying…")
        bg(_apply_changes, self.store.engine, self.d, v, changed, recreate,
           done=lambda new_id: self._done(new_id, recreate),
           error=lambda e: (self.apply_btn.set_sensitive(True), self.win.toast(f"Not changed: {e}")))

    def _done(self, new_id, recreate):
        self.win.toast("Container recreated with your changes" if recreate else "Changes applied")
        self.store.refresh("containers")
        self.close()
        if recreate and new_id != self.cid:
            GLib.timeout_add(600, lambda: self.win.show_container(new_id) or False)


def _apply_changes(engine, d, v, changed, recreate):
    cid = d["Id"]
    upd = {}
    if "restart" in changed:
        upd["RestartPolicy"] = {"Name": v["restart"], "MaximumRetryCount": 0}
    if "mem" in changed:
        upd["Memory"] = v["mem"] * 1024 * 1024
        upd["MemorySwap"] = -1 if v["mem"] else 0
    if "cpus" in changed:
        upd["NanoCpus"] = int(v["cpus"] * 1e9)
    if not recreate:
        if upd:
            engine.update(cid, upd)
        if "name" in changed and v["name"]:
            engine.rename(cid, v["name"])
        return cid
    return _recreate(engine, engine.inspect(cid), v)      # fresh: networks may have changed meanwhile


def _recreate(engine, d, v):
    cfg = dict(d.get("Config") or {})
    hc = dict(d.get("HostConfig") or {})
    old_name = d.get("Name", "").lstrip("/")
    new_name = v["name"] or old_name
    was_running = (d.get("State") or {}).get("Running")

    cfg["Image"] = v["image"]
    cfg["Env"] = [f"{k}={val}" for k, val in v["env"]]
    cfg["Cmd"] = shlex.split(v["cmd"]) if v["cmd"] else None
    cfg["WorkingDir"] = v["workdir"]
    exposed, bindings = {}, {}
    for host, ctr in v["ports"]:
        key = ctr if "/" in ctr else ctr + "/tcp"
        exposed[key] = {}
        ip, _, port = host.rpartition(":")              # "127.0.0.1:5432" keeps the address it was bound to
        bindings.setdefault(key, []).append({"HostIp": ip.strip("[]"), "HostPort": port})
    cfg["ExposedPorts"] = {**(cfg.get("ExposedPorts") or {}), **exposed}
    hc["PortBindings"] = bindings
    hc["Binds"] = [f"{src}:{dst}" for src, dst in v["mounts"]]
    hc["Mounts"] = []                      # everything is expressed as Binds now (keeps anonymous volumes too)
    hc["RestartPolicy"] = {"Name": v["restart"], "MaximumRetryCount": 0}
    hc["Memory"] = v["mem"] * 1024 * 1024
    hc["NanoCpus"] = int(v["cpus"] * 1e9)
    for k in ("Hostname",):
        cfg.pop(k, None)                   # let Docker give the new container its own hostname
    nets = (d.get("NetworkSettings") or {}).get("Networks") or {}
    endpoints = {}
    for n, ep in nets.items():
        endpoints[n] = {k: ep.get(k) for k in ("Aliases", "IPAMConfig", "Links", "DriverOpts") if ep.get(k)}
        if endpoints[n].get("Aliases"):
            endpoints[n]["Aliases"] = [a for a in endpoints[n]["Aliases"] if a != d["Id"][:12]]
    body = dict(cfg)
    body["HostConfig"] = hc
    body["NetworkingConfig"] = {"EndpointsConfig": endpoints}

    aside = f"{old_name}-berth-old-{int(time.time())}"
    if was_running:
        engine.action(d["Id"], "stop")
    engine.rename(d["Id"], aside)
    new_id = None
    try:
        new_id = engine.create_container(new_name, body)["Id"]
        if was_running:                    # a stopped container stays stopped after being recreated
            engine.action(new_id, "start")
            time.sleep(1.5)
            st = (engine.inspect(new_id).get("State") or {})
            if not st.get("Running"):
                raise RuntimeError(f"the new container exited (code {st.get('ExitCode')}): check the logs of '{new_name}'")
    except Exception:
        if new_id:
            try:
                engine.remove_container(new_id)
            except Exception:  # noqa: BLE001
                pass
        engine.rename(d["Id"], old_name)
        if was_running:
            engine.action(d["Id"], "start")
        raise
    engine.remove_container(d["Id"])
    return new_id


def values_from(d):
    """The editable values of a container exactly as they are (used to recreate it unchanged, e.g. on a new image)."""
    cfg, hc = d.get("Config") or {}, d.get("HostConfig") or {}
    ports = [((f"{b['HostIp']}:" if b.get("HostIp") not in (None, "", "0.0.0.0", "::") else "") + (b.get("HostPort") or ""),
              k.replace("/tcp", "")) for k, binds in (hc.get("PortBindings") or {}).items() for b in binds or [{}]]
    mounts = [((m.get("Name") if m.get("Type") == "volume" else m.get("Source")), m.get("Destination", "") + ("" if m.get("RW", True) else ":ro"))
              for m in d.get("Mounts") or [] if m.get("Type") in ("volume", "bind")]
    return {"name": d.get("Name", "").lstrip("/"), "restart": (hc.get("RestartPolicy") or {}).get("Name") or "no",
            "mem": int((hc.get("Memory") or 0) / 1024 / 1024), "cpus": round((hc.get("NanoCpus") or 0) / 1e9, 2),
            "image": cfg.get("Image", ""), "cmd": shlex.join(cfg["Cmd"]) if cfg.get("Cmd") else "",
            "workdir": cfg.get("WorkingDir") or "", "env": [(e.partition("=")[0], e.partition("=")[2]) for e in cfg.get("Env") or []],
            "ports": ports, "mounts": mounts}


def recreate_same(engine, cid):
    """Recreate a container with identical settings so it picks up the image its tag now points to."""
    return _recreate(engine, engine.inspect(cid), values_from(engine.inspect(cid)))


def change_ports(engine, cid, ports):
    """Recreate a container with new port mappings [(host, container)], everything else unchanged.
    host may be "8080" (all interfaces) or "127.0.0.1:8080" (this computer only)."""
    d = engine.inspect(cid)
    v = values_from(d)
    v["ports"] = ports
    return _recreate(engine, d, v)


def repoint_volume(engine, cid, old, new):
    """Recreate a container so its mounts of volume `old` use volume `new` instead."""
    d = engine.inspect(cid)
    v = values_from(d)
    v["mounts"] = [(new if src == old else src, dst) for src, dst in v["mounts"]]
    return _recreate(engine, d, v)
