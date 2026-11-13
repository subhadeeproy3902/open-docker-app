"""Image vulnerability scan with Trivy (open source, run as a container — nothing installed on the host).
The vulnerability database is cached in the volume `berth-trivy-cache` so later scans are quick."""
import json
import os
import time

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .util import Row, bg, confirm, esc, factory, open_uri, ordering

TRIVY = "aquasec/trivy:latest"
CACHE = "berth-trivy-cache"
SEV = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]
SEV_COL = {"CRITICAL": "#c01c28", "HIGH": "#e66100", "MEDIUM": "#c88800", "LOW": "#3584e4", "UNKNOWN": "#77767b"}
HELPER_LABEL = "io.github.subhadeeproy3902.berth.helper"
HISTORY = os.path.join(GLib.get_user_state_dir(), "berth", "scans.json")


def counts(vulns):
    c = {s: 0 for s in SEV}
    for v in vulns:
        c[v["sev"] if v["sev"] in c else "UNKNOWN"] += 1
    return c


def serious(vulns):
    """The critical and high findings, as comparable keys (same CVE in the same package)."""
    return {(v["id"], v["pkg"]) for v in vulns if v["sev"] in ("CRITICAL", "HIGH")}


def _load_history():
    try:
        with open(HISTORY) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def history(ref):
    """Past scans of a tag, oldest first: [{"t": time, "id": image id, "c": {severity: count}}]."""
    return _load_history().get(ref, [])


def _record(ref, image_id, vulns):
    h = _load_history()
    h[ref] = (h.get(ref, []) + [{"t": time.time(), "id": image_id, "c": counts(vulns)}])[-10:]
    try:
        os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
        with open(HISTORY, "w") as f:
            json.dump(h, f)
    except OSError:
        pass


def _daemon_side_socket(sock):
    """Bind mounts are resolved by the daemon. A local Linux unix socket is the same path for it; for remote, VM-based
    (Docker Desktop, Colima…) or docker-in-docker engines the daemon's own socket is /var/run/docker.sock."""
    import sys
    if sys.platform.startswith("linux") and sock.startswith("/") and not sock.startswith("tcp://"):
        return sock
    return "/var/run/docker.sock"


def have_trivy(engine):
    return any(TRIVY in (im.get("RepoTags") or []) for im in engine.images())


def run_scan(engine, ref, sock):
    if not have_trivy(engine):
        st = engine.pull(TRIVY)
        while st.read(2.0) != b"":
            pass
        st.close()
    body = {"Image": TRIVY, "Labels": {HELPER_LABEL: "scan"},
            "Cmd": ["image", "--format", "json", "--quiet", "--scanners", "vuln", "--timeout", "15m", ref],
            "HostConfig": {"Binds": [f"{_daemon_side_socket(sock)}:/var/run/docker.sock", f"{CACHE}:/root/.cache/"]}}
    cid = engine.create_container(None, body)["Id"]
    try:
        engine.action(cid, "start")
        res = engine.wait(cid, timeout=1800)
        out, err = engine.logs_text(cid)
    finally:
        try:
            engine.remove_container(cid)
        except Exception:  # noqa: BLE001
            pass
    if (res or {}).get("StatusCode"):
        raise RuntimeError(err.strip().splitlines()[-1] if err.strip() else f"Trivy exited with {res.get('StatusCode')}")
    data = json.loads(out or "{}")
    vulns = []
    for r in data.get("Results") or []:
        for v in r.get("Vulnerabilities") or []:
            vulns.append({"id": v.get("VulnerabilityID", ""), "pkg": v.get("PkgName", ""), "installed": v.get("InstalledVersion", ""),
                          "fixed": v.get("FixedVersion", ""), "sev": (v.get("Severity") or "UNKNOWN").upper(),
                          "title": v.get("Title") or (v.get("Description") or "")[:140], "url": v.get("PrimaryURL", ""),
                          "target": r.get("Target", ""), "type": r.get("Type", ""),
                          "path": v.get("PkgPath", "")})
    meta = data.get("Metadata") or {}
    _record(ref, meta.get("ImageID", ""), vulns)
    return meta, vulns


class ScanDialog(Adw.Dialog):
    def __init__(self, win, store, ref):
        super().__init__(title=f"Vulnerabilities · {ref}", content_width=980, content_height=680)
        self.win, self.store, self.ref = win, store, ref
        self.tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.rescan = Gtk.Button(label="Scan again", sensitive=False)
        self.rescan.connect("clicked", lambda *_: self._start())
        hb.pack_end(self.rescan)
        self.tv.add_top_bar(hb)
        self.set_child(self.tv)
        if have_trivy(store.engine):
            self._start()
        else:
            sp = Adw.StatusPage(icon_name="security-high-symbolic", title="Scan with Trivy?",
                                description="Berth scans images with <b>Trivy</b>, an open-source scanner that runs as a container. "
                                            "The first scan downloads it (about 270 MB) and its vulnerability database "
                                            f"(about 80 MB, refreshed daily, kept in the volume <tt>{CACHE}</tt>).")
            b = Gtk.Button(label="Download and scan", halign=Gtk.Align.CENTER)
            b.add_css_class("pill")
            b.add_css_class("suggested-action")
            b.connect("clicked", lambda *_: self._start())
            sp.set_child(b)
            self.tv.set_content(sp)

    def _start(self):
        self.rescan.set_sensitive(False)
        sp = Adw.StatusPage(title="Scanning…", description=f"{esc(self.ref)}\nThe first scan also downloads the database; "
                                                           "this can take a minute or two.")
        sp.set_child(Adw.Spinner(width_request=32, height_request=32, halign=Gtk.Align.CENTER))
        self.tv.set_content(sp)
        bg(run_scan, self.store.engine, self.ref, self.store.engine.path, done=self._show, error=self._fail)

    def _fail(self, e):
        self.rescan.set_sensitive(True)
        self.tv.set_content(Adw.StatusPage(icon_name="dialog-error-symbolic", title="Scan failed", description=esc(str(e))))

    def _show(self, res):
        meta, vulns = res
        if "images" in getattr(self.win, "pages", {}):
            self.win.pages["images"]._fill()          # the Security column shows the new result
        self.rescan.set_sensitive(True)
        self.vulns = vulns
        self.filter = Gtk.CustomFilter.new(self._keep, None)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        counts = {s: 0 for s in SEV}
        for v in vulns:
            counts[v["sev"] if v["sev"] in counts else "UNKNOWN"] += 1
        top = Gtk.Box(spacing=8, margin_start=14, margin_end=14, margin_top=8)
        self.filters = {}
        for s in SEV:
            b = Gtk.ToggleButton(active=s != "UNKNOWN" or counts[s] > 0)
            b.add_css_class("level-chip")
            lb = Gtk.Label()
            lb.set_markup(f"<span foreground='{SEV_COL[s]}'>●</span> {s.capitalize()}  <b>{counts[s]}</b>")
            b.set_child(lb)
            b.connect("toggled", lambda *_: self.filter.changed(Gtk.FilterChange.DIFFERENT))
            self.filters[s] = b
            top.append(b)
        self.search = Gtk.SearchEntry(placeholder_text="Search CVE, package…", hexpand=True)
        self.search.connect("search-changed", lambda *_: self.filter.changed(Gtk.FilterChange.DIFFERENT))
        top.append(self.search)
        box.append(top)
        os_info = (meta.get("OS") or {})
        summary = f"{len(vulns)} vulnerabilities" + (f" · {os_info.get('Family', '')} {os_info.get('Name', '')}" if os_info else "")
        fixable = sum(1 for v in vulns if v["fixed"])
        past = history(self.ref)
        if len(past) > 1:
            p = past[-2]
            summary += (f" · previous scan {time.strftime('%d %b %H:%M', time.localtime(p['t']))}: "
                        f"{p['c'].get('CRITICAL', 0)} critical, {p['c'].get('HIGH', 0)} high"
                        + (" (a different version)" if p["id"] and p["id"] != meta.get("ImageID") else ""))
        box.append(Gtk.Label(label=f"{summary} · {fixable} have a fix available", xalign=0, margin_start=16,
                             css_classes=["dim-label", "caption"]))
        if not vulns:
            box.append(Adw.StatusPage(icon_name="security-high-symbolic", title="No known vulnerabilities",
                                      description="Trivy found nothing in this image.", vexpand=True))
            self.tv.set_content(box)
            return
        model = Gio.ListStore(item_type=Row)
        for i, v in enumerate(sorted(vulns, key=lambda v: (SEV.index(v["sev"]) if v["sev"] in SEV else 9, v["pkg"]))):
            model.append(Row("vuln", str(i), v))
        view = Gtk.ColumnView(show_row_separators=True, single_click_activate=True)
        view.add_css_class("data-table")
        sm = Gtk.SortListModel(model=Gtk.FilterListModel(model=model, filter=self.filter), sorter=view.get_sorter())
        view.set_model(Gtk.NoSelection(model=sm))
        view.connect("activate", lambda _v, pos: sm.get_item(pos).data["url"] and open_uri(sm.get_item(pos).data["url"]))

        def col(title, key, width=-1, expand=False, css=(), sev=False):
            def setup():
                lb = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
                for c in css:
                    lb.add_css_class(c)
                return lb

            def bind(lb, row, _li):
                val = row.data[key]
                if sev:
                    lb.set_markup(f"<span foreground='{SEV_COL.get(val, '#777')}' weight='bold'>{esc(val.capitalize())}</span>")
                else:
                    lb.set_text(val or ("—" if key == "fixed" else ""))
                lb.set_tooltip_text(row.data["title"] if key in ("id", "title") else None)
            c = Gtk.ColumnViewColumn(title=title, factory=factory(setup, bind), expand=expand, resizable=True)
            if width > 0:
                c.set_fixed_width(width)
            c.set_sorter(Gtk.CustomSorter.new(lambda a, b, _d, k=key: ordering(
                SEV.index(a.data[k]) if sev and a.data[k] in SEV else a.data[k],
                SEV.index(b.data[k]) if sev and b.data[k] in SEV else b.data[k]), None))
            view.append_column(c)
        col("Severity", "sev", 90, sev=True)
        col("Vulnerability", "id", 160, css=("monospace",))
        col("Package", "pkg", 160)
        col("Installed", "installed", 130, css=("monospace", "dim-label"))
        col("Fixed in", "fixed", 130, css=("monospace",))
        col("Title", "title", expand=True)
        box.append(Gtk.ScrolledWindow(child=view, vexpand=True))
        box.append(Gtk.Label(label="Click a row to open its advisory.", xalign=0, margin_start=16, margin_bottom=8,
                             css_classes=["dim-label", "caption"]))
        self.tv.set_content(box)

    def _keep(self, row, _d):
        v = row.data
        if not self.filters.get(v["sev"], self.filters["UNKNOWN"]).get_active():
            return False
        q = self.search.get_text().strip().lower()
        return not q or q in v["id"].lower() or q in v["pkg"].lower() or q in v["title"].lower()
