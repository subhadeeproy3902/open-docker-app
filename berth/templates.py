"""Templates gallery (#31): one-click stacks from plain compose files in berth/templates/. A template's first comment
lines describe it (title, description, the address to open, small files to create next to it). Using one copies it
to Berth's data folder and opens the usual "Run a Compose file" dialog, which shows what it creates and checks it."""
import os
import re

from gi.repository import Adw, GLib, Gtk

from . import compose
from .util import esc, follow_content

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
STACKS = os.path.join(GLib.get_user_data_dir(), "berth", "stacks")


def load():
    """[{id, title, description, open, files: {path: text}, path}] sorted by title."""
    out = []
    for name in sorted(os.listdir(HERE)) if os.path.isdir(HERE) else []:
        if not name.endswith((".yaml", ".yml")):
            continue
        meta = {"id": name.rsplit(".", 1)[0], "path": os.path.join(HERE, name), "files": {}, "open": ""}
        with open(meta["path"]) as f:
            for line in f:
                m = re.match(r"#\s*(title|description|open|file ([^:]+)):\s*(.*)", line)
                if not m:
                    if not line.startswith("#"):
                        break
                    continue
                if m.group(2):
                    meta["files"][m.group(2).strip()] = m.group(3)
                else:
                    meta[m.group(1)] = m.group(3).strip()
        meta.setdefault("title", meta["id"])
        meta.setdefault("description", "")
        out.append(meta)
    return sorted(out, key=lambda t: t["title"].lower())


def materialise(t, project):
    """Copy the template into its own folder (so Compose's ./ paths and the project name work) and return the file."""
    folder = os.path.join(STACKS, project)
    os.makedirs(folder, exist_ok=True)
    dst = os.path.join(folder, "compose.yaml")
    if not os.path.exists(dst):
        with open(t["path"]) as src, open(dst, "w") as f:
            f.write(src.read())
    for rel, text in t["files"].items():
        p = os.path.join(folder, rel)
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text + "\n")
    return dst


def free_name(base, store):
    used = {(c.get("Labels") or {}).get("com.docker.compose.project") for c in store.containers}
    name, n = base, 2
    while name in used:
        name, n = f"{base}-{n}", n + 1
    return name


class TemplatesDialog(Adw.Dialog):
    def __init__(self, win, store):
        super().__init__(title="Templates", content_width=620, content_height=640)
        self.win, self.store = win, store
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        pp = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(description="Ready-made stacks with sensible defaults. Each one shows what it "
                                             "creates and is checked before it starts. Ports are open on this "
                                             "computer only.")
        for t in load():
            r = Adw.ActionRow(title=esc(t["title"]), subtitle=esc(t["description"]), activatable=True)
            r.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            r.connect("activated", lambda _r, t=t: self._use(t))
            g.add(r)
        pp.add(g)
        tv.set_content(pp)
        self.set_child(tv)
        follow_content(self, 620)

    def _use(self, t):
        name = free_name(t["id"], self.store)
        try:
            path = materialise(t, name)
        except OSError as e:
            self.win.toast(f"Couldn't prepare the template: {e}")
            return
        self.close()
        d = compose.ComposeOpen(self.win, self.store)
        d.use_file(path, name, t["open"] or None)
        d.present(self.win)
