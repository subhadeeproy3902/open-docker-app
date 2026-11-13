"""Environment and secrets of a stack (#23): every variable of every service, where it comes from (compose file,
.env interpolation, env_file, image default, set at creation), secrets masked, differences between services, and
editing the stack's .env / env_file files with validation. Applying recreates only the services whose configuration
changed (`docker compose up -d --no-deps <those>`)."""
import json
import os
import re
import subprocess

from gi.repository import Adw, Gtk, Pango

from . import compose
from .cli import CommandDialog
from .util import bg, esc, follow_content, host, labels

SECRET = re.compile(r"(KEY|SECRET|PASSWORD|PASSWD|PASS$|TOKEN|PRIVATE|CREDENTIAL|AUTH)", re.I)
LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=(.*)$")


def is_secret(name):
    return bool(SECRET.search(name))


def _config(project, wd, files, raw=False):
    argv = compose.compose_argv(project, wd, files, "config", "--format", "json",
                                *(["--no-interpolate", "--no-normalize"] if raw else []))
    r = subprocess.run(host(argv), capture_output=True, text=True, timeout=60, cwd=wd or None)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip()
                           else "docker compose config failed")
    return json.loads(r.stdout)


def parse_env_file(text):
    """{name: value} and [(line number, problem)] — the same rules Compose uses for .env files."""
    out, problems = {}, []
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = LINE.match(line)
        if not m:
            problems.append((n, "not NAME=value"))
            continue
        v = m.group(2).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        elif v.count('"') % 2 or v.count("'") % 2 and not v.startswith("#"):
            problems.append((n, "unbalanced quotes"))
        if m.group(1) in out:
            problems.append((n, f"{m.group(1)} is set twice — the last one wins"))
        out[m.group(1)] = v
    return out, problems


def collect(engine, containers, project):
    """Everything the dialog shows, gathered on a worker thread."""
    wd, files = compose.project_info(containers, project)
    raw = _config(project, wd, files, raw=True) if files else {"services": {}}
    services = {}
    env_files = set()
    if wd and os.path.exists(os.path.join(wd, ".env")):
        env_files.add(os.path.join(wd, ".env"))
    for svc, d in (raw.get("services") or {}).items():
        files_ = [e["path"] if isinstance(e, dict) else os.path.join(wd or "", e) for e in d.get("env_file") or []]
        env_files.update(files_)
        services[svc] = {"raw": d.get("environment") or {}, "env_files": files_}
    file_vals = {}
    for f in env_files:
        try:
            with open(f) as fh:
                file_vals[f] = parse_env_file(fh.read())[0]
        except OSError:
            file_vals[f] = {}
    rows = {}                                       # svc -> [(name, value, source)]
    for c in containers:
        lab = labels(c)
        if lab.get("com.docker.compose.project") != project:
            continue
        svc = lab.get("com.docker.compose.service", "?")
        try:
            ins = engine.inspect(c["Id"])
            img = engine.image_inspect(ins.get("Image", ""))
        except Exception:  # noqa: BLE001
            continue
        img_env = dict(e.split("=", 1) for e in (img.get("Config") or {}).get("Env") or [] if "=" in e)
        info = services.get(svc, {"raw": {}, "env_files": []})
        lst = []
        for e in (ins.get("Config") or {}).get("Env") or []:
            name, _, val = e.partition("=")
            rawv = info["raw"].get(name)
            if rawv is not None:
                src = "compose file" + (" (via ${…})" if isinstance(rawv, str) and "${" in rawv else "")
            elif any(name in file_vals.get(f, {}) for f in info["env_files"]):
                src = "env_file " + os.path.basename(next(f for f in info["env_files"] if name in file_vals.get(f, {})))
            elif img_env.get(name) == val:
                src = "image default"
            else:
                src = "set when created"
            lst.append((name, val, src))
        rows[svc] = sorted(lst)
    # the same name with different values in different services (image defaults like PATH don't count)
    seen = {}
    for svc, lst in rows.items():
        for name, val, src in lst:
            if src != "image default":
                seen.setdefault(name, {})[svc] = val
    conflicts = {n: v for n, v in seen.items() if len(set(v.values())) > 1}
    return {"wd": wd, "files": files, "rows": rows, "env_files": sorted(env_files), "conflicts": conflicts}


def changed_services(project, wd, files, before):
    after = _config(project, wd, files)
    out = []
    for svc, d in (after.get("services") or {}).items():
        if json.dumps(d, sort_keys=True) != json.dumps((before.get("services") or {}).get(svc), sort_keys=True):
            out.append(svc)
    return out


class SecretRow(Adw.ActionRow):
    def __init__(self, name, value, source):
        super().__init__(title=esc(name), subtitle=esc(source), subtitle_selectable=False)
        self.value, self.shown = value, not is_secret(name)
        self.val = Gtk.Label(xalign=1, ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=40, selectable=True,
                             css_classes=["monospace"])
        self.add_suffix(self.val)
        if is_secret(name):
            self.eye = Gtk.Button(icon_name="view-reveal-symbolic", tooltip_text="Show the value", valign=Gtk.Align.CENTER)
            self.eye.add_css_class("flat")
            self.eye.connect("clicked", lambda *_: self._toggle())
            self.add_suffix(self.eye)
        self._show()

    def _toggle(self):
        self.shown = not self.shown
        self.eye.set_icon_name("view-conceal-symbolic" if self.shown else "view-reveal-symbolic")
        self.eye.set_tooltip_text("Hide the value" if self.shown else "Show the value")
        self._show()

    def _show(self):
        self.val.set_text(self.value if self.shown else "••••••••")


class EnvDialog(Adw.Dialog):
    def __init__(self, win, store, project):
        super().__init__(title=f"Environment · {project}", content_width=760, content_height=700)
        self.win, self.store, self.project = win, store, project
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.pp = Adw.PreferencesPage()
        self.loading = Adw.PreferencesGroup(description="Reading the compose file and the containers…")
        self.pp.add(self.loading)
        tv.set_content(self.pp)
        self.set_child(tv)
        follow_content(self, 760)
        bg(collect, store.engine, list(store.containers), project, done=self._show,
           error=lambda e: self.loading.set_description(esc(f"Couldn't read the stack: {e}")))

    def _show(self, d):
        self.d = d
        self.pp.remove(self.loading)
        if d["conflicts"]:
            g = Adw.PreferencesGroup(title="Differs between services",
                                     description="The same variable has different values — often a mistake.")
            for name, per in sorted(d["conflicts"].items()):
                vals = ", ".join(f"{s}: {'••••' if is_secret(name) else v}" for s, v in sorted(per.items()))
                r = Adw.ActionRow(title=esc(name), subtitle=esc(vals))
                r.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
                g.add(r)
            self.pp.add(g)
        for svc, lst in sorted(d["rows"].items()):
            g = Adw.PreferencesGroup(title=esc(svc), description=f"{len(lst)} variables · secrets are hidden until "
                                                                 "you show them")
            for name, val, src in lst:
                g.add(SecretRow(name, val, src))
            self.pp.add(g)
        g = Adw.PreferencesGroup(title="Files", description="Edit the stack's .env and env_file files. Applying "
                                                            "recreates only the services whose settings change.")
        if not d["env_files"] and d["wd"]:
            r = Adw.ActionRow(title="No .env file", subtitle="Create one next to the compose file", activatable=True)
            r.add_suffix(Gtk.Image(icon_name="document-new-symbolic"))
            r.connect("activated", lambda *_: EnvFileDialog(self, os.path.join(d["wd"], ".env")).present(self.win))
            g.add(r)
        for f in d["env_files"]:
            r = Adw.ActionRow(title=esc(os.path.basename(f)), subtitle=esc(f), activatable=True)
            r.add_suffix(Gtk.Image(icon_name="document-edit-symbolic"))
            r.connect("activated", lambda _r, f=f: EnvFileDialog(self, f).present(self.win))
            g.add(r)
        if not d["files"]:
            g.set_description("Berth doesn't know this stack's compose file, so only what the containers run with "
                              "is shown.")
        self.pp.add(g)


class EnvFileDialog(Adw.Dialog):
    """Edit one env file: checked as you type, saved, then the services it changes are recreated (after asking)."""

    def __init__(self, parent, path):
        super().__init__(title=os.path.basename(path), content_width=640, content_height=520)
        self.parent, self.path = parent, path
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        hb.pack_start(cancel)
        self.save = Gtk.Button(label="Save and apply")
        self.save.add_css_class("suggested-action")
        self.save.connect("clicked", lambda *_: self._save())
        hb.pack_end(self.save)
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=16, margin_end=16, margin_bottom=16)
        self.view = Gtk.TextView(monospace=True, top_margin=10, bottom_margin=10, left_margin=12, right_margin=12)
        self.view.add_css_class("logview")
        try:
            with open(path) as f:
                self.view.get_buffer().set_text(f.read())
        except OSError:
            self.view.get_buffer().set_text("# NAME=value, one per line\n")
        self.view.get_buffer().connect("changed", lambda *_: self._validate())
        card = Gtk.Box(css_classes=["card"], overflow=Gtk.Overflow.HIDDEN)
        card.append(Gtk.ScrolledWindow(child=self.view, vexpand=True, hexpand=True, min_content_height=300))
        box.append(card)
        self.status = Gtk.Label(xalign=0, wrap=True, css_classes=["caption", "dim-label"])
        box.append(self.status)
        tv.set_content(box)
        self.set_child(tv)
        self._validate()

    def _text(self):
        b = self.view.get_buffer()
        return b.get_text(b.get_start_iter(), b.get_end_iter(), False)

    def _validate(self):
        vals, problems = parse_env_file(self._text())
        bad = [p for p in problems if "twice" not in p[1]]
        self.save.set_sensitive(not bad)
        self.status.set_text("; ".join(f"line {n}: {m}" for n, m in problems[:4]) if problems else
                             f"{len(vals)} variables · looks good")
        self.status.set_css_classes(["caption", "warn-text" if problems else "dim-label"])

    def _save(self):
        d, p = self.parent.d, self.parent.project
        old = None
        try:
            with open(self.path) as f:
                old = f.read()
        except OSError:
            pass
        text = self._text()

        def work():
            before = _config(p, d["wd"], d["files"]) if d["files"] else {}
            with open(self.path, "w") as f:
                f.write(text if text.endswith("\n") else text + "\n")
            try:
                return changed_services(p, d["wd"], d["files"], before) if d["files"] else []
            except Exception:
                if old is not None:                     # the file broke the stack: put it back
                    with open(self.path, "w") as f:
                        f.write(old)
                raise

        def done(svcs):
            self.close()
            win = self.parent.win
            if not svcs:
                win.toast("Saved — no service's settings changed")
                return
            argv = compose.compose_argv(p, d["wd"], d["files"], "up", "-d", "--no-deps", *svcs)
            CommandDialog(win, f"Recreate {', '.join(svcs)}", argv, cwd=d["wd"] or None,
                          on_done=lambda ok: win.store.refresh("containers")).present(win)
        bg(work, done=done, error=lambda e: self.status.set_text(f"Not saved: {e}"))
