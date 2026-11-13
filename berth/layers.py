"""Files per image layer (#13): what one build step added, changed or deleted, biggest first. The image is saved once
(`docker save`) to a temporary file in Berth's cache folder, read with tarfile, and the file is removed when the
window closes. Build steps are matched to layers through the image config's history (steps that only set metadata
have no layer)."""
import json
import os
import tarfile
import tempfile

from gi.repository import Adw, GLib, Gtk, Pango

from .util import bg, esc, fmt_bytes, label

BIG = 4 * 2**30


def save(engine, ref):
    folder = os.path.join(GLib.get_user_cache_dir(), "berth")
    os.makedirs(folder, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="layers-", suffix=".tar", dir=folder)
    os.close(fd)
    try:
        engine.save_image(ref, path)
    except Exception:
        os.remove(path)
        raise
    return path


def files_of(path, step_index, steps):
    """Files of the layer made by API history entry `step_index` (newest first, `steps` entries in all).
    → (rows [(path, size, kind)], note). kind: added · deleted · folder · link."""
    with tarfile.open(path) as t:
        manifest = json.load(t.extractfile("manifest.json"))[0]
        cfg = json.load(t.extractfile(manifest["Config"]))
        hist = cfg.get("history") or []
        layers = manifest.get("Layers") or []
        nonempty = [i for i, h in enumerate(hist) if not h.get("empty_layer")]
        if len(hist) == steps and len(nonempty) == len(layers):
            ci = len(hist) - 1 - step_index
            if ci not in nonempty:
                return [], "This step only sets metadata (ENV, CMD, LABEL…) — it adds no files."
            layer = layers[nonempty.index(ci)]
        else:                                     # history doesn't line up (squashed / imported images)
            return [], "This image's history doesn't match its layers (it was squashed or imported), so files " \
                       "can't be matched to steps."
        rows = []
        with tarfile.open(fileobj=t.extractfile(layer), mode="r:*") as lt:
            for m in lt:
                name = m.name.lstrip("./")
                base = os.path.basename(name)
                if base.startswith(".wh."):
                    rows.append(("/" + os.path.join(os.path.dirname(name), base[4:]) if base != ".wh..wh..opq"
                                 else "/" + os.path.dirname(name) + "/ (emptied)", 0, "deleted"))
                elif m.isdir():
                    rows.append(("/" + name, 0, "folder"))
                elif m.issym() or m.islnk():
                    rows.append(("/" + name, 0, "link"))
                else:
                    rows.append(("/" + name, m.size, "added"))
        rows.sort(key=lambda r: (-r[1], r[0]))
        return rows, ""


class LayerFiles(Adw.Dialog):
    SHOW = 3000

    def __init__(self, win, store, ref, step_index, steps, step_text, size):
        super().__init__(title="Files in this layer", content_width=820, content_height=640)
        self.win, self.rows, self.path = win, [], None
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=16, margin_end=16, margin_bottom=16)
        box.append(Gtk.Label(label=step_text[:300], xalign=0, wrap=True, css_classes=["monospace", "caption"],
                             wrap_mode=Pango.WrapMode.WORD_CHAR))
        self.summary = label("Reading the image… (docker save)", css=["dim-label", "caption"])
        box.append(self.summary)
        self.search = Gtk.SearchEntry(placeholder_text="Filter by path")
        self.search.connect("search-changed", lambda *_: self._fill())
        box.append(self.search)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        box.append(Gtk.ScrolledWindow(child=self.list, vexpand=True))
        self.spinner = Adw.Spinner(width_request=32, height_request=32, halign=Gtk.Align.CENTER)
        box.append(self.spinner)
        tv.set_content(box)
        self.set_child(tv)
        self.connect("closed", lambda *_: self._cleanup())

        def work():
            self.path = save(store.engine, ref)
            return files_of(self.path, step_index, steps)
        bg(work, done=self._show, error=lambda e: (self.spinner.set_visible(False),
                                                    self.summary.set_text(f"Couldn't read the image: {e}")))

    def _show(self, res):
        self.rows, note = res
        self.spinner.set_visible(False)
        self._cleanup()                       # everything needed is in memory now
        if note:
            self.summary.set_text(note)
            return
        added = [r for r in self.rows if r[2] == "added"]
        deleted = [r for r in self.rows if r[2] == "deleted"]
        self.summary.set_text(f"{len(added):,} files ({fmt_bytes(sum(r[1] for r in added))})"
                              + (f" · {len(deleted):,} deleted" if deleted else "") + " · biggest first")
        self._fill()

    def _fill(self):
        while (c := self.list.get_first_child()) is not None:
            self.list.remove(c)
        q = self.search.get_text().strip().lower()
        shown = [r for r in self.rows if not q or q in r[0].lower()]
        for p, size, kind in shown[:self.SHOW]:
            row = Gtk.Box(spacing=10, margin_start=10, margin_end=10, margin_top=4, margin_bottom=4)
            row.append(Gtk.Label(label=p, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE,
                                 css_classes=["monospace"] + (["dim-label"] if kind in ("folder", "link") else []),
                                 tooltip_text=p))
            if kind == "deleted":
                row.append(Gtk.Label(label="deleted", css_classes=["status-pill", "warn"]))
            elif kind == "added":
                row.append(Gtk.Label(label=fmt_bytes(size), css_classes=["numeric"]))
            self.list.append(row)
        if len(shown) > self.SHOW:
            self.list.append(label(f"… {len(shown) - self.SHOW:,} more — filter to narrow down", css=["dim-label"]))

    def _cleanup(self):
        if self.path and os.path.exists(self.path):
            try:
                os.remove(self.path)
            except OSError:
                pass
        self.path = None


def open_files(win, store, ref, step_index, steps, step_text, image_size):
    def go():
        LayerFiles(win, store, ref, step_index, steps, step_text, image_size).present(win)
    if image_size > BIG:
        from .util import confirm
        confirm(win, "This image is large", f"Berth saves it once ({fmt_bytes(image_size)}) to a temporary file to "
                "read the layer, then deletes it.", "Read it", go, destructive=False)
    else:
        go()
