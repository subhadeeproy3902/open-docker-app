"""Stacks: every Docker Compose project — running, stopped, or taken down but remembered — with its services and
the things you do to a stack: up, down, restart, pull newer images, edit, check the file, logs, open its folder."""
import os

from gi.repository import Adw, Gio, Gtk

from . import compose, compose_check
from .util import follow_content, PageHeader, WidePrefs, bg, cname, esc, flat_page, icon_button, labels, open_uri, short_status


def stacks(containers, remembered):
    """[{project, wd, files, services: [container], running, total}] — from labels, plus remembered ones."""
    out = {}
    for c in containers or []:
        lab = labels(c)
        p = lab.get("com.docker.compose.project")
        if not p:
            continue
        s = out.setdefault(p, {"project": p, "wd": lab.get("com.docker.compose.project.working_dir", ""),
                               "files": [f for f in (lab.get("com.docker.compose.project.config_files") or "")
                                         .split(",") if f], "services": []})
        s["services"].append(c)
    for r in remembered or []:
        if r.get("project") and r["project"] not in out:
            out[r["project"]] = {"project": r["project"], "wd": r.get("wd", ""), "files": list(r.get("files") or []),
                                 "services": []}
    for s in out.values():
        s["services"].sort(key=lambda c: labels(c).get("com.docker.compose.service", cname(c)))
        s["running"] = sum(1 for c in s["services"] if c.get("State") == "running")
        s["total"] = len(s["services"])
    return sorted(out.values(), key=lambda s: (s["total"] == 0, s["project"]))


class StacksPage(Gtk.Box):
    title = "Stacks"
    icon = "view-dual-symbolic"

    def __init__(self, win, store, settings):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.store, self.settings = win, store, settings
        tv = Adw.ToolbarView(vexpand=True)
        self.header = PageHeader("Stacks", "Docker Compose projects — one row per stack")
        run = Gtk.Button(label="Run a Compose file…", valign=Gtk.Align.CENTER,
                         tooltip_text="Start a stack from a compose.yaml (it is checked first)")
        run.add_css_class("suggested-action")
        run.connect("clicked", lambda *_: compose.ComposeOpen(self.win, self.store).present(self.win))
        self.header.add(run)
        flat_page(tv, self.header)
        self.pp = WidePrefs(two_at=("lg", "xl", "2xl", "3xl"))
        self.groups = []
        self.empty = Adw.StatusPage(icon_name=self.icon, title="No stacks yet",
                                    description="Stacks are groups of containers started from a compose.yaml. Run "
                                                "one, and it shows up here with its services.")
        b = Gtk.Button(label="Run a Compose file…", halign=Gtk.Align.CENTER)
        b.add_css_class("pill")
        b.add_css_class("suggested-action")
        b.connect("clicked", lambda *_: compose.ComposeOpen(self.win, self.store).present(self.win))
        self.empty.set_child(b)
        self.stack = Gtk.Stack()
        self.stack.add_named(self.pp, "list")
        self.stack.add_named(self.empty, "empty")
        tv.set_content(self.stack)
        self.append(tv)
        self._sig = None
        store.connect("containers", lambda *_: self.get_mapped() and self.refresh())

    def on_show(self):
        self.refresh()

    def on_size(self, n):
        self.pp.on_size(n)

    def refresh(self):
        for g in self.groups:
            self.pp.remove(g)
        self.groups = []
        lst = stacks(self.store.containers, self.settings["stacks"])
        self.stack.set_visible_child_name("list" if lst else "empty")
        n_up = sum(1 for s in lst if s["running"])
        self.header.set_subtitle(f"{len(lst)} stack{'s' if len(lst) != 1 else ''} · {n_up} running" if lst else
                                 "Docker Compose projects — one row per stack")
        for s in lst:
            g = self._group(s)
            self.pp.add(g)
            self.groups.append(g)

    def _group(self, s):
        p = s["project"]
        state = ("down" if not s["total"] else "running" if s["running"] == s["total"] else
                 "stopped" if not s["running"] else "partly running")
        files = ", ".join(s["files"]) or "compose file unknown"
        g = Adw.PreferencesGroup(title=esc(p), description=esc(f"{state.capitalize()} · {s['running']} of "
                                                              f"{s['total']} services up · {files}")
                                 if s["total"] else esc(f"Down · {files}"))
        box = Gtk.Box(spacing=4)
        acts = [("media-playback-start-symbolic", "Compose up — start everything (creates what is missing)",
                 lambda: compose.up(self.win, self.store, p)),
                ("view-refresh-symbolic", "Restart every service", lambda: compose.restart(self.win, self.store, p)),
                ("folder-download-symbolic", "Pull newer images, then recreate what changed",
                 lambda: compose.pull_up(self.win, self.store, p)),
                ("media-playback-stop-symbolic", "Compose down — stop and remove the containers (data is kept)",
                 lambda: compose.down(self.win, self.store, p))]
        for icon, tip, fn in acts:
            b = icon_button(icon, tip, lambda *_a, f=fn: f())
            box.append(b)
        menu = Gio.Menu()
        menu.append("Check the compose file", f"stk.check::{p}")
        menu.append("Edit the compose file…", f"stk.edit::{p}")
        menu.append("Show its logs", f"stk.logs::{p}")
        menu.append("Open its folder", f"stk.folder::{p}")
        if not s["total"]:
            menu.append("Forget this stack", f"stk.forget::{p}")
        mb = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="More for this stack",
                            valign=Gtk.Align.CENTER)
        mb.add_css_class("flat")
        box.append(mb)
        g.set_header_suffix(box)
        ag = Gio.SimpleActionGroup()
        from gi.repository import GLib
        for name, fn in (("check", self._check), ("edit", self._edit), ("logs", self._logs), ("folder", self._folder),
                         ("forget", self._forget)):
            a = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            a.connect("activate", lambda _a, v, f=fn: f(v.get_string()))
            ag.add_action(a)
        g.insert_action_group("stk", ag)
        if not s["services"]:
            g.add(Adw.ActionRow(title="Not running", subtitle="Compose up starts it again from its file."))
        for c in s["services"]:
            svc = labels(c).get("com.docker.compose.service", cname(c))
            running = c.get("State") == "running"
            r = Adw.ActionRow(title=esc(svc), subtitle=esc(f"{c.get('Image', '')} · {short_status(c.get('Status', ''))}"),
                              activatable=True)
            dot = Gtk.Box(css_classes=["dot"] + (["running"] if running else []), valign=Gtk.Align.CENTER)
            r.add_prefix(dot)
            ports = sorted({pp_.get("PublicPort") for pp_ in c.get("Ports") or [] if pp_.get("PublicPort")})
            if ports:
                r.add_suffix(Gtk.Label(label=", ".join(f":{x}" for x in ports[:3]), css_classes=["dim-label",
                                                                                                "monospace"]))
            r.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            r.set_tooltip_text("Open this container")
            r.connect("activated", lambda _r, cid=c["Id"]: self.win.show_container(cid))
            g.add(r)
        return g

    # ── actions ──
    def _info(self, p):
        return compose.project_info(self.store.containers, p)

    def _check(self, p):
        wd, files = self._info(p)
        if not files:
            self.win.toast("Berth doesn't know this stack's compose file")
            return
        d = Adw.Dialog(title=f"Check · {p}", content_width=560)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        g = Adw.PreferencesGroup(title=esc(", ".join(files)), description="Checking…", margin_start=18,
                                 margin_end=18, margin_top=6, margin_bottom=18)
        # sized to its results (a PreferencesPage would keep the dialog at its first, tiny height)
        tv.set_content(Gtk.ScrolledWindow(child=g, propagate_natural_height=True, max_content_height=560,
                                          hscrollbar_policy=Gtk.PolicyType.NEVER))
        d.set_child(tv)
        follow_content(d, 560)
        d.present(self.win)
        rows = []
        st = self.store
        bg(lambda: compose_check.check(files, *compose.check_inputs(st), p, wd),
           done=lambda res: compose.show_checks(g, rows, res), error=lambda e: g.set_description(esc(str(e))))

    def _edit(self, p):
        compose.ComposeEditor(self.win, self.store, p).present(self.win)

    def _logs(self, p):
        self.win.go("logs")
        self.win.pages["logs"].show_project(p)

    def _folder(self, p):
        wd, files = self._info(p)
        path = wd or (os.path.dirname(files[0]) if files else "")
        if path and os.path.isdir(path):
            open_uri(Gio.File.new_for_path(path).get_uri())
        else:
            self.win.toast("The folder isn't on this computer")

    def _forget(self, p):
        compose.forget(p)
        self.refresh()
