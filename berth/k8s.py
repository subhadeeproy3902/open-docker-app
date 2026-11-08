"""Kubernetes view on top of kubectl (any cluster in your kubeconfig), plus local clusters with kind.
Pods, deployments, services and nodes; logs, shell, describe, scale, restart, port-forward, YAML edit/apply."""
import json
import shutil
import subprocess
import time

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .cli import CommandDialog, run_capture
from .util import Row, ago, bg, confirm, esc, factory, host, host_which, icon_button, label, open_uri, ordering, run_in_terminal

KINDS = [("pods", "Pods"), ("deployments", "Deployments"), ("services", "Services"), ("nodes", "Nodes")]


def _age(ts):
    try:
        import calendar
        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 0


def rows_for(kind, items):
    out = []
    for it in items:
        md, sp, st = it.get("metadata") or {}, it.get("spec") or {}, it.get("status") or {}
        d = {"name": md.get("name", ""), "ns": md.get("namespace", ""), "created": _age(md.get("creationTimestamp")), "raw": it}
        if kind == "pods":
            cs = st.get("containerStatuses") or []
            reason = next(((c.get("state") or {}).get("waiting", {}).get("reason") for c in cs
                           if (c.get("state") or {}).get("waiting")), None)
            d.update(status=reason or st.get("phase", ""), ready=f"{sum(1 for c in cs if c.get('ready'))}/{len(cs) or len(sp.get('containers') or [])}",
                     restarts=sum(c.get("restartCount", 0) for c in cs), node=sp.get("nodeName", ""), ip=st.get("podIP", ""))
        elif kind == "deployments":
            want = sp.get("replicas", 0)
            d.update(status="Ready" if st.get("readyReplicas", 0) == want else "Progressing",
                     ready=f"{st.get('readyReplicas', 0)}/{want}", replicas=want, uptodate=st.get("updatedReplicas", 0),
                     image=", ".join(c.get("image", "") for c in ((sp.get("template") or {}).get("spec") or {}).get("containers") or []))
        elif kind == "services":
            ports = ", ".join(f"{p.get('port')}{'→' + str(p.get('targetPort')) if p.get('targetPort') != p.get('port') else ''}/{p.get('protocol', 'TCP')}"
                              + (f" (node {p['nodePort']})" if p.get("nodePort") else "") for p in sp.get("ports") or [])
            d.update(status=sp.get("type", ""), ip=sp.get("clusterIP", ""), ports=ports, port_list=[p.get("port") for p in sp.get("ports") or []])
        elif kind == "nodes":
            cond = {c.get("type"): c.get("status") for c in st.get("conditions") or []}
            roles = [k.split("/", 1)[1] for k in (md.get("labels") or {}) if k.startswith("node-role.kubernetes.io/")]
            d.update(status="Ready" if cond.get("Ready") == "True" else "NotReady", roles=", ".join(roles) or "worker",
                     version=(st.get("nodeInfo") or {}).get("kubeletVersion", ""),
                     ip=next((a.get("address") for a in st.get("addresses") or [] if a.get("type") == "InternalIP"), ""),
                     unschedulable=bool(sp.get("unschedulable")))
        out.append(Row(kind, f"{d['ns']}/{d['name']}", d))
    return out


class K8sPage(Gtk.Box):
    title, icon = "Kubernetes", "network-server-symbolic"
    wants_stats = False

    def __init__(self, win, store, settings):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.store, self.settings = win, store, settings
        self.kind, self.contexts, self.namespaces = "pods", [], []
        self.busy, self.timer, self.query = False, 0, ""
        self.nav = Adw.NavigationView(vexpand=True)
        self.append(self.nav)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.wtitle = Adw.WindowTitle(title="Kubernetes")
        hb.set_title_widget(self.wtitle)
        self.ctx = Gtk.DropDown(model=Gtk.StringList(), tooltip_text="Cluster (kubeconfig context)")
        self.ctx.connect("notify::selected", lambda *_: self._ctx_changed())
        self.ns = Gtk.DropDown(model=Gtk.StringList.new(["All namespaces"]), tooltip_text="Namespace", enable_search=True)
        self.ns.connect("notify::selected", lambda *_: self.refresh())
        hb.pack_start(self.ctx)
        hb.pack_start(self.ns)
        menu = Gio.Menu()
        menu.append("Apply a YAML file…", "k8s.apply")
        menu.append("Create a local cluster (kind)…", "k8s.create")
        menu.append("Delete a local cluster (kind)…", "k8s.delete")
        hb.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="Cluster actions"))
        hb.pack_end(icon_button("view-refresh-symbolic", "Reload", self.refresh, css=()))
        ag = Gio.SimpleActionGroup()
        for n, fn in (("apply", self._apply_file), ("create", self._create_cluster), ("delete", self._delete_cluster)):
            a = Gio.SimpleAction.new(n, None)
            a.connect("activate", lambda *_a, f=fn: f())
            ag.add_action(a)
        for n, fn in (("logs", self._logs), ("shell", self._shell), ("describe", self._describe), ("yaml", self._yaml),
                      ("delete-res", self._delete_res), ("scale", self._scale), ("restart", self._restart),
                      ("forward", self._forward), ("cordon", lambda k: self._cordon(k, True)),
                      ("uncordon", lambda k: self._cordon(k, False))):
            a = Gio.SimpleAction.new(n, GLib.VariantType.new("s"))
            a.connect("activate", lambda _a, p, f=fn: f(p.get_string()))
            ag.add_action(a)
        self.insert_action_group("k8s", ag)
        tv.add_top_bar(hb)
        bar = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=8, margin_bottom=6)
        self.kinds = Adw.ToggleGroup()
        for k, lbl in KINDS:
            self.kinds.add(Adw.Toggle(name=k, label=lbl))
        self.kinds.set_active_name("pods")
        self.kinds.connect("notify::active-name", lambda *_: self._kind_changed())
        bar.append(self.kinds)
        self.search = Gtk.SearchEntry(placeholder_text="Filter", hexpand=True)
        self.search.connect("search-changed", lambda e: (setattr(self, "query", e.get_text().strip().lower()),
                                                         self.filter.changed(Gtk.FilterChange.DIFFERENT)))
        bar.append(self.search)
        self.model = Gio.ListStore(item_type=Row)
        self.filter = Gtk.CustomFilter.new(lambda r, _d: not self.query or self.query in r.data["name"].lower()
                                           or self.query in r.data["ns"].lower(), None)
        self.view = Gtk.ColumnView(show_row_separators=True)
        self.view.add_css_class("data-table")
        self.view.add_css_class("berth-table")
        self.sorted = Gtk.SortListModel(model=Gtk.FilterListModel(model=self.model, filter=self.filter), sorter=self.view.get_sorter())
        self.view.set_model(Gtk.NoSelection(model=self.sorted))
        self.stack = Gtk.Stack(vexpand=True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(bar)
        body.append(Gtk.ScrolledWindow(child=self.view, vexpand=True))
        self.stack.add_named(body, "table")
        self.status = Adw.StatusPage(icon_name="network-server-symbolic")
        self.stack.add_named(self.status, "status")
        tv.set_content(self.stack)
        self.nav.add(Adw.NavigationPage(title="Kubernetes", child=tv, tag="list"))
        self._columns()
        self.connect("map", lambda *_: self._start())
        self.connect("unmap", lambda *_: self._stop_timer())

    # ── setup / polling ──
    def kubectl(self, *args):
        ctx = self._context()
        return ["kubectl"] + (["--context", ctx] if ctx else []) + list(args)

    def _context(self):
        i = self.ctx.get_selected()
        return self.contexts[i] if self.contexts and i < len(self.contexts) else None

    def _ns(self):
        i = self.ns.get_selected()
        return None if i == 0 or i > len(self.namespaces) else self.namespaces[i - 1]

    def on_show(self):
        pass

    def _start(self):
        if not host_which("kubectl"):
            self._show_status("kubectl is not installed", "Install it to use the Kubernetes view:\n"
                              "<tt>sudo pacman -S kubectl</tt> (add <tt>kind</tt> to run a local cluster).")
            return

        def got(out):
            cur = out[1].strip()
            self.contexts = [c for c in out[0].split() if c]
            for w in (self.ctx, self.ns):
                w.set_visible(bool(self.contexts))
            if not self.contexts:
                self._show_status("No Kubernetes cluster configured",
                                  "Create a local one with kind (menu ⋮ → Create a local cluster), or add one to ~/.kube/config.",
                                  create=bool(host_which("kind")))
                return
            self._loading_ctx = True
            self.ctx.set_model(Gtk.StringList.new(self.contexts))
            if cur in self.contexts:
                self.ctx.set_selected(self.contexts.index(cur))
            self._loading_ctx = False
            self._ctx_changed()
        bg(lambda: (run_capture(["kubectl", "config", "get-contexts", "-o", "name"]),
                    subprocess.run(host(["kubectl", "config", "current-context"]), capture_output=True, text=True).stdout),
           done=got, error=lambda e: self._show_status("Can't read kubeconfig", esc(str(e))))

    def _ctx_changed(self):
        if getattr(self, "_loading_ctx", False):
            return

        def got(out):
            self.namespaces = sorted(out.split())
            self._loading_ns = True
            self.ns.set_model(Gtk.StringList.new(["All namespaces"] + self.namespaces))
            self._loading_ns = False
            self.refresh()
            self._stop_timer()
            self.timer = GLib.timeout_add_seconds(5, lambda: (self.refresh(quiet=True), True)[1])
        bg(run_capture, self.kubectl("get", "namespaces", "-o", "jsonpath={.items[*].metadata.name}"), 15, done=got,
           error=lambda e: self._show_status("Can't reach the cluster", esc(str(e)[:400]), create=False))

    def _stop_timer(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0

    def _show_status(self, title, desc, create=False):
        self.status.set_title(title)
        self.status.set_description(desc)
        if create:
            b = Gtk.Button(label="Create a local cluster", halign=Gtk.Align.CENTER)
            b.add_css_class("pill")
            b.add_css_class("suggested-action")
            b.connect("clicked", lambda *_: self._create_cluster())
            self.status.set_child(b)
        else:
            self.status.set_child(None)
        self.stack.set_visible_child_name("status")

    def refresh(self, quiet=False):
        if getattr(self, "_loading_ns", False) or self.busy or not self.contexts:
            return
        self.busy = True
        kind, ns = self.kind, self._ns()
        args = ["get", kind, "-o", "json"] + ([] if kind == "nodes" else (["-n", ns] if ns else ["-A"]))

        def done(out):
            self.busy = False
            if kind != self.kind:
                return
            rows = rows_for(kind, json.loads(out).get("items") or [])
            self._sync(rows)
            self.stack.set_visible_child_name("table")
            self.wtitle.set_subtitle(f"{self._context()} · {len(rows)} {kind}")

        def fail(e):
            self.busy = False
            if not quiet:
                self._show_status("Can't reach the cluster", esc(str(e)[:400]))
        bg(run_capture, self.kubectl(*args), 20, done=done, error=fail)

    def _sync(self, rows):
        old = {self.model.get_item(i).key: self.model.get_item(i) for i in range(self.model.get_n_items())}
        if [r.key for r in rows] == list(old):
            for r in rows:
                o = old[r.key]
                o.data = r.data
                o.emit("changed")
            return
        self.model.splice(0, self.model.get_n_items(), rows)

    def _kind_changed(self):
        self.kind = self.kinds.get_active_name() or "pods"
        self.model.remove_all()
        self._columns()
        self.refresh()

    # ── columns ──
    def _columns(self):
        while self.view.get_columns().get_n_items():
            self.view.remove_column(self.view.get_columns().get_item(0))

        def col(title, fn, width=-1, expand=False, css=(), sort=None, xalign=0.0):
            def setup():
                lb = Gtk.Label(xalign=xalign, ellipsize=Pango.EllipsizeMode.END)
                for c in css:
                    lb.add_css_class(c)
                return lb

            def bind(lb, row, _li):
                from .util import connect_row, disconnect_rows
                disconnect_rows(lb)
                lb.set_text(str(fn(row.data)))
                connect_row(lb, row, "changed", lambda r, w=lb: w.set_text(str(fn(r.data))))
            c = Gtk.ColumnViewColumn(title=title, factory=factory(setup, bind), expand=expand, resizable=True)
            if width > 0:
                c.set_fixed_width(width)
            if sort:
                c.set_sorter(Gtk.CustomSorter.new(lambda a, b, _d: ordering(sort(a.data), sort(b.data)), None))
            self.view.append_column(c)

        def status_col():
            def setup():
                return Gtk.Label(halign=Gtk.Align.START, valign=Gtk.Align.CENTER, css_classes=["status-pill"])

            def bind(lb, row, _li):
                from .util import connect_row, disconnect_rows
                disconnect_rows(lb)

                def upd(r, lb=lb):
                    s = r.data["status"]
                    lb.set_text(s)
                    good = s in ("Running", "Ready", "Succeeded", "Completed", "ClusterIP", "NodePort", "LoadBalancer")
                    lb.set_css_classes(["status-pill", "ok" if good else "warn" if s in ("Pending", "Progressing", "ContainerCreating") else "bad"])
                upd(row)
                connect_row(lb, row, "changed", upd)
            c = Gtk.ColumnViewColumn(title="Status", factory=factory(setup, bind), resizable=True)
            c.set_fixed_width(150)
            c.set_sorter(Gtk.CustomSorter.new(lambda a, b, _d: ordering(a.data["status"], b.data["status"]), None))
            self.view.append_column(c)

        col("Name", lambda d: d["name"], expand=True, css=("heading-sm",), sort=lambda d: d["name"])
        if self.kind != "nodes":
            col("Namespace", lambda d: d["ns"], 140, css=("dim-label",), sort=lambda d: d["ns"])
        status_col()
        if self.kind == "pods":
            col("Ready", lambda d: d["ready"], 70, css=("numeric",))
            col("Restarts", lambda d: d["restarts"], 80, css=("numeric",), sort=lambda d: d["restarts"], xalign=1.0)
            col("Node", lambda d: d["node"], 150, css=("dim-label",))
        elif self.kind == "deployments":
            col("Ready", lambda d: d["ready"], 80, css=("numeric",))
            col("Image", lambda d: d["image"], 220, css=("monospace", "dim-label"))
        elif self.kind == "services":
            col("Cluster IP", lambda d: d["ip"], 130, css=("monospace", "dim-label"))
            col("Ports", lambda d: d["ports"], 200, css=("monospace",))
        elif self.kind == "nodes":
            col("Roles", lambda d: d["roles"], 140)
            col("Version", lambda d: d["version"], 110, css=("monospace", "dim-label"))
            col("IP", lambda d: d["ip"], 130, css=("monospace", "dim-label"))
        col("Age", lambda d: ago(d["created"]).replace(" ago", "") if d["created"] else "", 90, css=("dim-label",),
            sort=lambda d: -d["created"])

        def act_setup():
            mb = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER, halign=Gtk.Align.END)
            mb.add_css_class("flat")
            return mb

        def act_bind(mb, row, _li):
            key = f"{self.kind}|{row.data['ns']}|{row.data['name']}"
            m = Gio.Menu()
            if self.kind in ("pods", "deployments"):
                m.append("Logs", f"k8s.logs::{key}")
            if self.kind == "pods":
                m.append("Open a shell", f"k8s.shell::{key}")
            if self.kind == "deployments":
                m.append("Scale…", f"k8s.scale::{key}")
                m.append("Restart (rollout)", f"k8s.restart::{key}")
            if self.kind == "services":
                m.append("Port-forward…", f"k8s.forward::{key}")
            if self.kind == "nodes":
                m.append("Allow scheduling" if row.data.get("unschedulable") else "Stop scheduling (cordon)",
                         f"k8s.{'uncordon' if row.data.get('unschedulable') else 'cordon'}::{key}")
            m.append("Describe", f"k8s.describe::{key}")
            m.append("Edit YAML…", f"k8s.yaml::{key}")
            if self.kind != "nodes":
                m.append("Delete…", f"k8s.delete-res::{key}")
            mb.set_menu_model(m)
        c = Gtk.ColumnViewColumn(title="", factory=factory(act_setup, act_bind))
        c.set_fixed_width(56)
        self.view.append_column(c)

    # ── actions ──
    @staticmethod
    def _split(key):
        kind, ns, name = key.split("|", 2)
        return kind, ns, name

    def _ref(self, key):
        kind, ns, name = self._split(key)
        return [f"{kind}/{name}"] + (["-n", ns] if ns else [])

    def _logs(self, key):
        kind, ns, name = self._split(key)
        CommandDialog(self.win, f"Logs · {name}", self.kubectl("logs", "-f", "--tail=500", "--all-containers",
                                                                "--timestamps", *self._ref(key))).present(self.win)

    def _shell(self, key):
        _k, ns, name = self._split(key)
        argv = self.kubectl("exec", "-it", "-n", ns, name, "--", "sh", "-c", "command -v bash >/dev/null && exec bash || exec sh")
        from .containers import Vte
        if Vte is not None:
            d = Adw.Dialog(title=f"Shell · {name}", content_width=860, content_height=520)
            tvw = Adw.ToolbarView()
            tvw.add_top_bar(Adw.HeaderBar())
            term = Vte.Terminal(vexpand=True, hexpand=True)
            term.set_cursor_blink_mode(Vte.CursorBlinkMode.OFF)
            term.set_clear_background(False)
            term.spawn_async(Vte.PtyFlags.DEFAULT, None, argv, None, GLib.SpawnFlags.SEARCH_PATH, None, None, -1, None, None, None)
            term.connect("child-exited", lambda *_: d.close())
            tvw.set_content(term)
            d.set_child(tvw)
            d.present(self.win)
            term.grab_focus()
        elif not run_in_terminal(argv):
            self.win.toast("No terminal emulator found (set $TERMINAL)")

    def _describe(self, key):
        _k, _ns, name = self._split(key)
        CommandDialog(self.win, f"Describe · {name}", self.kubectl("describe", *self._ref(key))).present(self.win)

    def _yaml(self, key):
        YamlEditor(self, key).present(self.win)

    def _delete_res(self, key):
        kind, ns, name = self._split(key)
        confirm(self.win, f"Delete {esc(kind[:-1])} {esc(name)}?",
                "A pod owned by a deployment is replaced automatically; deleting a deployment removes its pods.", "Delete",
                lambda: bg(run_capture, self.kubectl("delete", *self._ref(key), "--wait=false"), 60,
                           done=lambda _o: (self.win.toast(f"Deleted {name}"), self.refresh()),
                           error=lambda e: self.win.toast(str(e))))

    def _scale(self, key):
        _k, ns, name = self._split(key)
        cur = next((self.model.get_item(i).data.get("replicas", 1) for i in range(self.model.get_n_items())
                    if self.model.get_item(i).data["name"] == name and self.model.get_item(i).data["ns"] == ns), 1)
        d = Adw.AlertDialog(heading=f"Scale {name}", body="Number of pods to run")
        spin = Gtk.SpinButton.new_with_range(0, 100, 1)
        spin.set_value(cur)
        d.set_extra_child(spin)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Scale")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.connect("response", lambda _d, r: r == "ok" and bg(
            run_capture, self.kubectl("scale", *self._ref(key), f"--replicas={int(spin.get_value())}"), 60,
            done=lambda _o: (self.win.toast(f"Scaled {name} to {int(spin.get_value())}"), self.refresh()),
            error=lambda e: self.win.toast(str(e))))
        d.present(self.win)

    def _restart(self, key):
        _k, _ns, name = self._split(key)
        bg(run_capture, self.kubectl("rollout", "restart", *self._ref(key)), 60,
           done=lambda _o: (self.win.toast(f"Restarting {name}"), self.refresh()), error=lambda e: self.win.toast(str(e)))

    def _forward(self, key):
        _k, ns, name = self._split(key)
        row = next((self.model.get_item(i).data for i in range(self.model.get_n_items())
                    if self.model.get_item(i).data["name"] == name and self.model.get_item(i).data["ns"] == ns), {})
        port = (row.get("port_list") or [80])[0]
        d = Adw.AlertDialog(heading=f"Port-forward {name}", body=f"Reach service port {port} on this computer at localhost:<port>. "
                                                               "It runs until you close the window that opens.")
        spin = Gtk.SpinButton.new_with_range(1024, 65535, 1)
        spin.set_value(8080 if port < 1024 else port)
        d.set_extra_child(spin)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Forward")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def go():
            local = int(spin.get_value())
            CommandDialog(self.win, f"Port-forward · localhost:{local}",
                          self.kubectl("port-forward", *self._ref(key), f"{local}:{port}")).present(self.win)
            GLib.timeout_add(1500, lambda: open_uri(f"http://localhost:{local}") or False)
        d.connect("response", lambda _d, r: r == "ok" and go())
        d.present(self.win)

    def _cordon(self, key, on):
        _k, _ns, name = self._split(key)
        bg(run_capture, self.kubectl("cordon" if on else "uncordon", name), 30,
           done=lambda _o: (self.win.toast(("Cordoned " if on else "Uncordoned ") + name), self.refresh()),
           error=lambda e: self.win.toast(str(e)))

    def _apply_file(self):
        if not self.contexts:
            return self.win.toast("No cluster to apply to")
        from .pages import open_dialog
        open_dialog(self.win, "Apply YAML", lambda p: CommandDialog(self.win, "kubectl apply", self.kubectl("apply", "-f", p),
                                                                    on_done=lambda ok: self.refresh()).present(self.win),
                    ["*.yaml", "*.yml", "*.json"])

    def _create_cluster(self):
        if not host_which("kind"):
            return self.win.toast("Install kind first: sudo pacman -S kind")
        from .pages import ask_text
        ask_text(self.win, "Create a local cluster", "Runs Kubernetes inside a Docker container with kind "
                 "(downloads a ~400 MB node image the first time; uses ~600–800 MB RAM while it exists).",
                 "berth", "Create",
                 lambda n: CommandDialog(self.win, f"Creating cluster {n}", ["kind", "create", "cluster", "--name", n],
                                         on_done=lambda ok: ok and GLib.idle_add(lambda: self._start() or False)).present(self.win))

    def _delete_cluster(self):
        if not host_which("kind"):
            return self.win.toast("kind is not installed")

        def got(out):
            names = out.split()
            if not names:
                return self.win.toast("No kind clusters")
            d = Adw.AlertDialog(heading="Delete a local cluster", body="Removes the cluster's container and everything in it.")
            dd = Gtk.DropDown.new_from_strings(names)
            d.set_extra_child(dd)
            d.add_response("cancel", "Cancel")
            d.add_response("ok", "Delete")
            d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
            d.connect("response", lambda _d, r: r == "ok" and CommandDialog(
                self.win, "Deleting cluster", ["kind", "delete", "cluster", "--name", names[dd.get_selected()]],
                on_done=lambda ok: GLib.idle_add(lambda: self._start() or False)).present(self.win))
            d.present(self.win)
        bg(run_capture, ["kind", "get", "clusters"], 30, done=got, error=lambda e: self.win.toast(str(e)))

    def focus_search(self):
        self.search.grab_focus()


class YamlEditor(Adw.Dialog):
    """kubectl get -o yaml → edit → kubectl apply -f - (errors shown inline, nothing applied on error)."""

    def __init__(self, page, key):
        kind, ns, name = key.split("|", 2)
        super().__init__(title=f"{kind[:-1].capitalize()} · {name}", content_width=820, content_height=680)
        self.page, self.key = page, key
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        close = Gtk.Button(label="Cancel")
        close.connect("clicked", lambda *_: self.close())
        self.apply = Gtk.Button(label="Apply", sensitive=False)
        self.apply.add_css_class("suggested-action")
        self.apply.connect("clicked", lambda *_: self._apply())
        hb.pack_start(close)
        hb.pack_end(self.apply)
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.msg = Gtk.Label(xalign=0, wrap=True, margin_start=14, margin_end=14, margin_top=6, css_classes=["dim-label", "caption"],
                             label="Changes are applied with kubectl apply. Status fields are ignored by the cluster.")
        box.append(self.msg)
        self.text = Gtk.TextView(monospace=True, left_margin=12, right_margin=12, top_margin=8, bottom_margin=12)
        self.text.add_css_class("logview")
        box.append(Gtk.ScrolledWindow(child=self.text, vexpand=True))
        tv.set_content(box)
        self.set_child(tv)
        bg(run_capture, page.kubectl("get", *page._ref(key), "-o", "yaml"), 30,
           done=lambda y: (self.text.get_buffer().set_text(y), self.apply.set_sensitive(True)),
           error=lambda e: self.msg.set_text(str(e)))

    def _apply(self):
        b = self.text.get_buffer()
        y = b.get_text(b.get_start_iter(), b.get_end_iter(), False)
        self.apply.set_sensitive(False)

        def work():
            r = subprocess.run(host(self.page.kubectl("apply", "-f", "-")), input=y, capture_output=True, text=True, timeout=60)
            if r.returncode:
                raise RuntimeError(r.stderr.strip() or r.stdout.strip())
            return r.stdout.strip()

        def done(out):
            self.page.win.toast(out or "Applied")
            self.page.refresh()
            self.close()

        def fail(e):
            self.apply.set_sensitive(True)
            self.msg.set_text(f"Not applied: {e}")
            self.msg.set_css_classes(["error", "caption"])
        bg(work, done=done, error=fail)
