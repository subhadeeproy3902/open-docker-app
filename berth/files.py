"""File browser + editor for a running container, and for volumes (through a tiny helper container).

Listing and small operations use `exec`; reading and saving files use the Engine's archive API
(tar in / tar out), so edits keep working even on images without a text editor."""
import io
import os
import tarfile
import time

from gi.repository import Adw, Gio, GLib, Gtk

from .util import bg, confirm, esc, fmt_bytes, icon_button, label

# exit 0 at the end: in an empty folder the last test fails, and that must not read as "no shell"
LIST = ('cd -- "$1" 2>/dev/null || exit 3; for f in .* *; do case "$f" in .|..) continue;; esac; '
        '{ [ -e "$f" ] || [ -L "$f" ]; } && stat -c "%F|%s|%Y|%a|%U/%u|%n" -- "$f" 2>/dev/null; done; exit 0')
MAX_EDIT = 2 * 1024 * 1024
HELPER_IMAGE = "busybox:latest"
HELPER_LABEL = "io.github.subhadeeproy3902.berth.helper"


def _join(d, name):
    return "/" + name if d == "/" else d.rstrip("/") + "/" + name


def _parent(p):
    p = p.rstrip("/")
    return p.rsplit("/", 1)[0] or "/"


def _tar_one(name, data, mode=0o644):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.mtime = len(data), mode, int(time.time())
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _first_file(tar_bytes):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as t:
        for m in t.getmembers():
            if m.isfile():
                return m, t.extractfile(m).read()
    return None, b""


class FileBrowser(Gtk.Box):
    """cid: a running container. root: where browsing starts. note: optional warning banner text."""

    def __init__(self, win, store, cid, root="/", note=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.store, self.cid, self.root = win, store, cid, root
        self.cwd, self.current, self.dirty = root, None, False
        if note:
            b = Adw.Banner(title=note, revealed=True)
            self.append(b)
        bar = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=8, margin_bottom=6)
        self.up = icon_button("go-up-symbolic", "Parent folder", lambda: self.load(_parent(self.cwd)), css=())
        self.path = Gtk.Entry(text=root, hexpand=True)
        self.path.add_css_class("monospace")
        self.path.connect("activate", lambda e: self.load(e.get_text().strip() or "/"))
        bar.append(self.up)
        bar.append(self.path)
        for icon, tip, fn in (("view-refresh-symbolic", "Reload", lambda: self.load(self.cwd)),
                              ("document-new-symbolic", "New file", self._new_file),
                              ("folder-new-symbolic", "New folder", self._new_folder),
                              ("document-send-symbolic", "Upload files here", self._upload)):
            bar.append(icon_button(icon, tip, fn, css=()))
        self.append(bar)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True, position=420, shrink_start_child=False)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("navigation-sidebar")
        self.list.connect("row-activated", self._activated)
        paned.set_start_child(Gtk.ScrolledWindow(child=self.list))

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        ebar = Gtk.Box(spacing=6, margin_start=10, margin_end=10, margin_top=6, margin_bottom=4)
        self.fname = label("Select a file", css=["heading"], hexpand=True)
        self.save = Gtk.Button(label="Save", sensitive=False, tooltip_text="Save (Ctrl+S)")
        self.save.add_css_class("suggested-action")
        self.save.connect("clicked", lambda *_: self._save())
        self.revert = icon_button("edit-undo-symbolic", "Discard changes", lambda: self._open(self.current), css=())
        self.download = icon_button("folder-download-symbolic", "Download to this computer", self._download, css=())
        self.rename = icon_button("document-edit-symbolic", "Rename / move", self._rename, css=())
        self.delete = icon_button("user-trash-symbolic", "Delete", self._delete, css=())
        for w in (self.fname, self.revert, self.download, self.rename, self.delete, self.save):
            ebar.append(w)
        for w in (self.revert, self.download, self.rename, self.delete):
            w.set_sensitive(False)
        right.append(ebar)
        self.editor = Gtk.TextView(monospace=True, left_margin=10, right_margin=10, top_margin=6, bottom_margin=10,
                                   editable=False)
        self.editor.add_css_class("logview")
        self.ebuf = self.editor.get_buffer()
        self.ebuf.set_text("Pick a file on the left to view or edit it.\nClick a folder to open it.")
        self.ebuf.connect("changed", self._changed)
        right.append(Gtk.ScrolledWindow(child=self.editor, vexpand=True))
        paned.set_end_child(right)
        self.append(paned)
        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>s"),
                                       action=Gtk.CallbackAction.new(lambda *_: (self._save(), True)[1])))
        self.add_controller(keys)
        self.selected = None
        self.load(root)

    # ── listing ──
    def load(self, path):
        self.cwd = path
        self.path.set_text(path)
        self._clear()
        self.list.append(Gtk.Label(label="Loading…", margin_top=12, css_classes=["dim-label"]))

        def done(r):
            code, so, se = r
            self._clear()
            if code == 3:
                self.list.append(Gtk.Label(label="Folder not found", margin_top=12, css_classes=["dim-label"]))
                return
            ents = [ln.split("|", 5) for ln in so.splitlines() if ln.count("|") >= 5]
            if not ents and (code or se.strip()):
                self.list.append(Gtk.Label(label="Can't list files here — this image has no shell or 'stat'.",
                                           wrap=True, margin_top=12, css_classes=["dim-label"]))
                return
            if not ents:
                self.list.append(Gtk.Label(label="Empty folder", margin_top=12, css_classes=["dim-label"]))
            ents.sort(key=lambda e: ("directory" not in e[0], e[5].lower()))
            for kind, size, mtime, perm, owner, name in ents[:5000]:
                user, _s, uid = owner.partition("/")       # a user the image doesn't know shows as its number
                owner = user if user and user != "UNKNOWN" else (f"uid {uid}" if uid else user)
                is_dir, is_link = "directory" in kind, "link" in kind
                row = Adw.ActionRow(title=esc(name), activatable=True,
                                    subtitle=f"{'folder' if is_dir else fmt_bytes(int(size or 0))} · "
                                             f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(int(float(mtime or 0))))} · {perm} {esc(owner)}")
                row.add_prefix(Gtk.Image(icon_name="folder-symbolic" if is_dir else "insert-link-symbolic"
                                         if is_link else "text-x-generic-symbolic"))
                row.entry = (_join(path, name), is_dir, int(size or 0), perm)
                self.list.append(row)
        bg(self.store.engine.exec_run, self.cid, ["sh", "-c", LIST, "sh", path], done=done,
           error=lambda e: (self._clear(), self.list.append(Gtk.Label(label=str(e), wrap=True, margin_top=12))))

    def _clear(self):
        while (ch := self.list.get_first_child()):
            self.list.remove(ch)

    def _activated(self, _lb, row):
        e = getattr(row.get_child(), "entry", None) or getattr(row, "entry", None)
        if not e:
            return
        full, is_dir, size, perm = e
        if is_dir:
            self.load(full)
            return
        self._confirm_leave(lambda: self._open(e))

    # ── viewing / editing ──
    def _confirm_leave(self, then):
        if self.dirty:
            confirm(self.win, "Discard unsaved changes?", f"{esc(self.current[0])} has unsaved edits.", "Discard", then)
        else:
            then()

    def _open(self, e):
        if not e:
            return
        full, _d, size, perm = e
        self.current, self.dirty = e, False
        self.fname.set_text(full)
        for w in (self.download, self.rename, self.delete):
            w.set_sensitive(True)
        self.revert.set_sensitive(False)
        self.save.set_sensitive(False)
        self.editor.set_editable(False)
        if size > MAX_EDIT:
            self._set_text(f"{fmt_bytes(size)} — too big to edit here. Use Download.")
            return
        self._set_text("Loading…")

        def done(tar):
            m, data = _first_file(tar)
            if self.current is not e:
                return
            if m is None:
                self._set_text("Not a regular file.")
                return
            if b"\x00" in data[:8192]:
                self._set_text(f"Binary file ({fmt_bytes(len(data))}) — not shown. Use Download.")
                return
            try:
                text = data.decode()
            except UnicodeDecodeError:
                text = data.decode("latin-1")
            self.current = (full, False, len(data), oct(m.mode)[-3:])
            self._set_text(text)
            self.editor.set_editable(True)
        bg(self.store.engine.get_archive, self.cid, full, done=done, error=lambda er: self._set_text(str(er)))

    def _set_text(self, t):
        self._loading = True
        self.ebuf.set_text(t)
        self._loading = False
        self.dirty = False

    def _changed(self, *_):
        if getattr(self, "_loading", False) or not self.editor.get_editable():
            return
        self.dirty = True
        self.save.set_sensitive(True)
        self.revert.set_sensitive(True)
        self.fname.set_text(self.current[0] + "  •")

    def _save(self):
        if not (self.current and self.dirty):
            return
        full = self.current[0]
        data = self.ebuf.get_text(self.ebuf.get_start_iter(), self.ebuf.get_end_iter(), False).encode()
        mode = int(self.current[3] or "644", 8)
        self.save.set_sensitive(False)

        def done(_r):
            self.dirty = False
            self.fname.set_text(full)
            self.revert.set_sensitive(False)
            self.win.toast(f"Saved {full}")
            self.load(self.cwd)
        bg(self.store.engine.put_archive, self.cid, _parent(full), _tar_one(os.path.basename(full), data, mode),
           done=done, error=lambda e: (self.save.set_sensitive(True), self.win.toast(f"Save failed: {e}")))

    # ── file operations ──
    def _ask_name(self, heading, initial, ok, then):
        d = Adw.AlertDialog(heading=heading)
        e = Gtk.Entry(text=initial)
        e.add_css_class("monospace")
        d.set_extra_child(e)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", ok)
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("ok")
        e.connect("activate", lambda *_: (d.close(), then(e.get_text().strip())))
        d.connect("response", lambda _d, r: r == "ok" and e.get_text().strip() and then(e.get_text().strip()))
        d.present(self.win)

    def _run(self, cmd, ok_msg, reload=True):
        def done(r):
            code, so, se = r
            if code:
                self.win.toast((se or so or f"exit {code}").strip()[:200])
            else:
                self.win.toast(ok_msg)
            if reload:
                self.load(self.cwd)
        bg(self.store.engine.exec_run, self.cid, cmd, done=done, error=lambda e: self.win.toast(str(e)))

    def _new_folder(self):
        self._ask_name("New folder", "new-folder", "Create",
                       lambda n: self._run(["mkdir", "-p", "--", _join(self.cwd, n)], f"Created {n}"))

    def _new_file(self):
        def make(n):
            bg(self.store.engine.put_archive, self.cid, self.cwd, _tar_one(n, b""),
               done=lambda _r: (self.load(self.cwd), self._open((_join(self.cwd, n), False, 0, "644"))),
               error=lambda e: self.win.toast(str(e)))
        self._ask_name("New file", "new-file.txt", "Create", make)

    def _rename(self):
        if not self.current:
            return
        full = self.current[0]
        self._ask_name("Rename or move", full, "Rename",
                       lambda n: self._run(["mv", "--", full, n if n.startswith("/") else _join(_parent(full), n)],
                                           "Renamed"))

    def _delete(self):
        if not self.current:
            return
        full = self.current[0]
        confirm(self.win, "Delete this file?", f"<tt>{esc(full)}</tt>\nIt is removed inside the container right away.",
                "Delete", lambda: (self._run(["rm", "-rf", "--", full], "Deleted"), self._reset_editor()))

    def _reset_editor(self):
        self.current, self.dirty = None, False
        self.editor.set_editable(False)
        self._set_text("")
        self.fname.set_text("Select a file")
        for w in (self.download, self.rename, self.delete, self.save, self.revert):
            w.set_sensitive(False)

    def _download(self):
        if not self.current:
            return
        full = self.current[0]
        dlg = Gtk.FileDialog(title="Save to folder", accept_label="Save here")

        def picked(d, res):
            try:
                folder = d.select_folder_finish(res)
            except GLib.Error:
                return
            dest = folder.get_path()

            def work():
                tar = self.store.engine.get_archive(self.cid, full)
                with tarfile.open(fileobj=io.BytesIO(tar)) as t:
                    t.extractall(dest, filter="data")
                return os.path.join(dest, os.path.basename(full))
            bg(work, done=lambda p: self.win.toast(f"Saved to {p}"), error=lambda e: self.win.toast(str(e)))
        dlg.select_folder(self.win, None, picked)

    def _upload(self):
        dlg = Gtk.FileDialog(title="Upload files", accept_label="Upload")

        def picked(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]

            def work():
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w") as t:
                    for p in paths:
                        t.add(p, arcname=os.path.basename(p))
                self.store.engine.put_archive(self.cid, self.cwd, buf.getvalue())
                return len(paths)
            bg(work, done=lambda n: (self.win.toast(f"Uploaded {n} item{'s' if n > 1 else ''}"), self.load(self.cwd)),
               error=lambda e: self.win.toast(str(e)))
        dlg.open_multiple(self.win, None, picked)


# ── volumes: a throwaway busybox container mounts the volume so it can be browsed ──
def start_volume_helper(engine, volume):
    """Blocking. Returns the helper container id (auto-removed when stopped)."""
    return _helper(engine, [f"{volume}:/volume"])


def stop_volume_helper(engine, cid):
    try:
        engine.req("POST", f"/containers/{cid}/kill", timeout=10)
    except Exception:  # noqa: BLE001 — already gone
        pass


def cleanup_helpers(engine):
    # only volume helpers: a scan container (label value "scan") may belong to a scan still running elsewhere
    for c in engine.containers_filtered({"label": [f"{HELPER_LABEL}=volume"]}) or []:
        try:
            engine.remove_container(c["Id"])
        except Exception:  # noqa: BLE001
            pass


# ── volume export / import / clone (all through the same throwaway helper) ──
def _helper(engine, binds):
    have = any(HELPER_IMAGE in (im.get("RepoTags") or []) for im in engine.images())
    if not have:
        st = engine.pull(HELPER_IMAGE)
        while st.read(2.0) != b"":
            pass
        st.close()
    r = engine.create_container(None, {"Image": HELPER_IMAGE, "Cmd": ["sleep", "86400"], "Labels": {HELPER_LABEL: "volume"},
                                       "HostConfig": {"Binds": binds, "AutoRemove": True}})
    engine.action(r["Id"], "start")
    return r["Id"]


def export_volume(engine, volume, dest):
    """Write the volume's contents to dest (.tar.gz). Returns bytes written."""
    import gzip
    import shutil
    import tempfile
    h = _helper(engine, [f"{volume}:/volume:ro"])
    fd, tmp = tempfile.mkstemp(prefix="berth-vol-", suffix=".tar")
    os.close(fd)
    try:
        engine.stream_to_file(f"/containers/{h}/archive", {"path": "/volume/."}, tmp)
        if dest.endswith((".gz", ".tgz")):
            with open(tmp, "rb") as src, gzip.open(dest, "wb", compresslevel=6) as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
        else:
            shutil.move(tmp, dest)
    finally:
        stop_volume_helper(engine, h)
        if os.path.exists(tmp):
            os.unlink(tmp)
    return os.path.getsize(dest)


def import_volume(engine, volume, src):
    """Unpack a .tar / .tar.gz into the volume (existing files with the same names are replaced)."""
    import gzip
    import shutil
    import tempfile
    tmp = None
    if src.endswith((".gz", ".tgz")):
        fd, tmp = tempfile.mkstemp(prefix="berth-vol-", suffix=".tar")
        os.close(fd)
        with gzip.open(src, "rb") as i, open(tmp, "wb") as o:
            shutil.copyfileobj(i, o, 1024 * 1024)
    h = _helper(engine, [f"{volume}:/volume"])
    try:
        engine.put_archive_file(h, "/volume", tmp or src)
    finally:
        stop_volume_helper(engine, h)
        if tmp:
            os.unlink(tmp)


def clone_volume(engine, volume, new_name):
    engine.create_volume(new_name)
    h = _helper(engine, [f"{volume}:/from:ro", f"{new_name}:/to"])
    try:
        code, _o, err = engine.exec_run(h, ["sh", "-c", "cp -a /from/. /to/"], timeout=3600)
        if code:
            raise RuntimeError(err.strip() or f"copy failed ({code})")
    finally:
        stop_volume_helper(engine, h)
