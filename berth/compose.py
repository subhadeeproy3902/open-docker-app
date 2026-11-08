"""Docker Compose: start a stack from a file, and up / pull / down / edit an existing stack."""
import os

from gi.repository import Adw, Gio, GLib, Gtk

from .cli import CommandDialog
from .util import confirm, esc, labels


def project_info(containers, project):
    """(working_dir, [config files]) for a compose project, from its containers' labels."""
    for c in containers:
        lab = labels(c)
        if lab.get("com.docker.compose.project") == project:
            wd = lab.get("com.docker.compose.project.working_dir", "")
            files = [f for f in (lab.get("com.docker.compose.project.config_files") or "").split(",") if f]
            return wd, files
    return "", []


def compose_argv(project, wd, files, *verb):
    a = ["docker", "compose", "-p", project]
    if wd:
        a += ["--project-directory", wd]
    for f in files:
        a += ["-f", f]
    return a + list(verb)


def run(win, store, project, verb_label, *verb):
    wd, files = project_info(store.containers, project)
    if not files or not all(os.path.exists(f) for f in files):
        win.toast(f"Compose file for {project} not found ({', '.join(files) or 'no label'})")
        return
    CommandDialog(win, f"{verb_label} · {project}", compose_argv(project, wd, files, *verb), cwd=wd or None,
                  on_done=lambda ok: store.refresh("containers")).present(win)


def up(win, store, project):
    run(win, store, project, "Compose up", "up", "-d", "--remove-orphans")


def pull_up(win, store, project):
    wd, files = project_info(store.containers, project)
    if not files:
        return win.toast("Compose file not found")
    argv = ["sh", "-c", '"$@" pull --ignore-buildable && "$@" up -d', "sh"] + compose_argv(project, wd, files)
    CommandDialog(win, f"Pull and recreate · {project}", argv, cwd=wd or None,
                  on_done=lambda ok: store.refresh("containers")).present(win)


def down(win, store, project):
    confirm(win, f"Take down {esc(project)}?",
            "Stops and <b>removes</b> its containers and networks. Volumes (data) are kept. "
            "Bring it back with Compose up.", "Compose down",
            lambda: run(win, store, project, "Compose down", "down"))


class ComposeEditor(Adw.Dialog):
    """Edit a project's compose file(s) on disk, then optionally apply with `up -d`."""

    def __init__(self, win, store, project):
        super().__init__(title=f"Compose file · {project}", content_width=820, content_height=680)
        self.win, self.store, self.project = win, store, project
        self.wd, self.files = project_info(store.containers, project)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        hb.pack_start(close)
        self.apply = Gtk.Button(label="Save and apply")
        self.apply.add_css_class("suggested-action")
        self.apply.set_tooltip_text("Save, then run docker compose up -d so changed services are recreated")
        self.apply.connect("clicked", lambda *_: self._save(apply=True))
        self.save = Gtk.Button(label="Save")
        self.save.connect("clicked", lambda *_: self._save())
        hb.pack_end(self.apply)
        hb.pack_end(self.save)
        self.pick = Gtk.DropDown.new_from_strings([os.path.basename(f) for f in self.files] or ["(none)"])
        self.pick.connect("notify::selected", lambda *_: self._load())
        hb.set_title_widget(self.pick)
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.path_lbl = Gtk.Label(xalign=0, margin_start=14, margin_top=6, margin_bottom=4, selectable=True,
                                  css_classes=["dim-label", "caption", "monospace"])
        box.append(self.path_lbl)
        self.text = Gtk.TextView(monospace=True, left_margin=12, right_margin=12, top_margin=8, bottom_margin=12)
        self.text.add_css_class("logview")
        box.append(Gtk.ScrolledWindow(child=self.text, vexpand=True))
        tv.set_content(box)
        self.set_child(tv)
        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>s"),
                                       action=Gtk.CallbackAction.new(lambda *_: (self._save(), True)[1])))
        self.add_controller(keys)
        self._load()

    def _file(self):
        i = self.pick.get_selected()
        return self.files[i] if self.files and i < len(self.files) else None

    def _load(self):
        f = self._file()
        self.path_lbl.set_text(f or "No compose file recorded for this project")
        try:
            with open(f) as fh:
                self.text.get_buffer().set_text(fh.read())
            ok = os.access(f, os.W_OK)
        except (OSError, TypeError) as e:
            self.text.get_buffer().set_text(f"Can't read it: {e}")
            ok = False
        self.save.set_sensitive(ok)
        self.apply.set_sensitive(ok)

    def _save(self, apply=False):
        f = self._file()
        if not f:
            return
        b = self.text.get_buffer()
        data = b.get_text(b.get_start_iter(), b.get_end_iter(), False)
        try:
            import shutil
            shutil.copy2(f, f + ".berth-bak")
            with open(f, "w") as fh:
                fh.write(data)
        except OSError as e:
            self.win.toast(f"Not saved: {e}")
            return
        self.win.toast(f"Saved {os.path.basename(f)} (previous version: {os.path.basename(f)}.berth-bak)")
        if apply:
            self.close()
            up(self.win, self.store, self.project)


class ComposeOpen(Adw.Dialog):
    """Start a stack from any compose file on disk."""

    def __init__(self, win, store):
        super().__init__(title="Run a Compose file", content_width=560)
        self.win, self.store, self.file = win, store, None
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.go = Gtk.Button(label="Compose up", sensitive=False)
        self.go.add_css_class("suggested-action")
        self.go.connect("clicked", lambda *_: self._go())
        hb.pack_start(cancel)
        hb.pack_end(self.go)
        tv.add_top_bar(hb)
        pp = Adw.PreferencesPage()
        g = Adw.PreferencesGroup()
        self.file_row = Adw.ActionRow(title="Compose file", subtitle="Choose a compose.yaml / docker-compose.yml", activatable=True)
        self.file_row.add_suffix(Gtk.Image(icon_name="document-open-symbolic"))
        self.file_row.connect("activated", lambda *_: self._pick())
        self.name = Adw.EntryRow(title="Project name")
        self.build = Adw.SwitchRow(title="Build images first", subtitle="--build")
        self.pull = Adw.SwitchRow(title="Pull newer images", subtitle="--pull always")
        self.profiles = Adw.EntryRow(title="Profiles (comma separated, optional)")
        for w in (self.file_row, self.name, self.build, self.pull, self.profiles):
            g.add(w)
        pp.add(g)
        tv.set_content(pp)
        self.set_child(tv)

    def _pick(self):
        flt = Gtk.FileFilter()
        flt.set_name("Compose files")
        for pat in ("*.yml", "*.yaml"):
            flt.add_pattern(pat)
        store = Gio.ListStore(item_type=Gtk.FileFilter)
        store.append(flt)
        d = Gtk.FileDialog(title="Choose a compose file", filters=store)

        def done(dlg, res):
            try:
                f = dlg.open_finish(res)
            except GLib.Error:
                return
            self.file = f.get_path()
            self.file_row.set_subtitle(esc(self.file))
            if not self.name.get_text():
                self.name.set_text(os.path.basename(os.path.dirname(self.file)).lower().replace(" ", "-"))
            self.go.set_sensitive(True)
        d.open(self.win, None, done)

    def _go(self):
        name = self.name.get_text().strip() or "stack"
        argv = ["docker", "compose", "-p", name, "-f", self.file]
        for p in [x.strip() for x in self.profiles.get_text().split(",") if x.strip()]:
            argv += ["--profile", p]
        argv += ["up", "-d"]
        if self.build.get_active():
            argv.append("--build")
        if self.pull.get_active():
            argv += ["--pull", "always"]
        self.close()
        CommandDialog(self.win, f"Compose up · {name}", argv, cwd=os.path.dirname(self.file),
                      on_done=lambda ok: self.store.refresh("containers")).present(self.win)
