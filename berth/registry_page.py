"""Registries page (#34): browse the repositories and tags of Docker Hub, GHCR, Quay, GitLab or a self-hosted
registry (Registry v2 API), see which tags differ from the copy on this computer, pull, copy a tag to another
registry, and delete old tags where the registry allows it. Credentials come from docker's own store."""
import shlex
import threading

from gi.repository import Adw, GLib, Gtk

from . import registry as R
from .cli import CommandDialog
from .pages import TablePage, row_menu
from .util import Row, bg, confirm, esc, fmt_bytes, icon_button


class RegistryPage(TablePage):
    title, icon = "Registries", "network-server-symbolic"
    empty_text = ("Pick a repository", "Choose a registry, then a repository — or type one, e.g. library/nginx.")

    def __init__(self, win, store, settings):
        self.client, self.repo, self.gen = None, "", 0
        super().__init__(win, store, settings)
        self.stack.set_visible_child_name("empty")
        self.search.set_placeholder_text("Search tags")
        self.wtitle.set_subtitle("Repositories and tags on Docker Hub or any registry you can reach")

    # ── header / toolbar ──
    def header_buttons(self):
        add = Gtk.Button(label="Add registry…", tooltip_text="A self-hosted registry, GHCR, Quay, GitLab…")
        add.connect("clicked", lambda *_: self._add_registry())
        return [add]

    def toolbar_widgets(self):
        self.reg_names = Gtk.StringList()
        self.reg_pick = Gtk.DropDown(model=self.reg_names, tooltip_text="Registry")
        self._fill_registries()
        self.reg_pick.connect("notify::selected", lambda *_: self._pick_registry())
        self.repo_names = Gtk.StringList()
        self.repo_pick = Gtk.DropDown(model=self.repo_names, enable_search=True, tooltip_text="Repository",
                                      sensitive=False)
        self.repo_pick.connect("notify::selected", lambda *_: self._pick_repo())
        self.repo_entry = Gtk.Entry(placeholder_text="Namespace or repository", width_chars=24,
                                    tooltip_text="Docker Hub: your user / organisation, or user/repo. Elsewhere: a "
                                                 "repository name, for registries that don't list theirs (GHCR).")
        self.repo_entry.connect("activate", lambda e: self._typed(e.get_text().strip()))
        go = icon_button("go-next-symbolic", "Open", lambda: self._typed(self.repo_entry.get_text().strip()), css=())
        return [self.reg_pick, self.repo_entry, go, self.repo_pick]

    def _registries(self):
        regs = ["Docker Hub"]
        for r in R.logins() + list(self.settings["registries"] or []):
            p = R.pretty(r).removeprefix("https://").removeprefix("http://").rstrip("/")
            if p not in regs:
                regs.append(p)
        return regs

    def _fill_registries(self, select=None):
        regs = self._registries()
        self.reg_names.splice(0, self.reg_names.get_n_items(), regs)
        if select in regs:
            self.reg_pick.set_selected(regs.index(select))

    def _add_registry(self):
        d = Adw.AlertDialog(heading="Add a registry", body="Its address, e.g. localhost:5000, ghcr.io or "
                                                           "registry.example.com. Sign in from Settings → Registries "
                                                           "if it needs a password.")
        e = Gtk.Entry(placeholder_text="host[:port]")
        e.add_css_class("monospace")
        d.set_extra_child(e)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Add")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def go(_d, r):
            v = e.get_text().strip().removeprefix("https://").removeprefix("http://").rstrip("/")
            if r == "ok" and v:
                self.settings["registries"] = sorted(set(list(self.settings["registries"] or []) + [v]))
                self._fill_registries(select=v)
        d.connect("response", go)
        d.present(self.win)

    # ── loading ──
    def _current_registry(self):
        it = self.reg_pick.get_selected_item()
        return it.get_string() if it else "Docker Hub"

    def _pick_registry(self):
        reg = self._current_registry()
        self.client = None
        self.repo_names.splice(0, self.repo_names.get_n_items(), [])
        self.repo_pick.set_sensitive(False)
        self.set_rows([])
        self.stack.set_visible_child_name("empty")
        if R.is_hub(reg):
            self.note.set_text("Docker Hub: type your user or organisation name to list its repositories.")
            self.note.set_visible(True)
            return
        self._list(reg, "")

    def _client(self, reg):
        if self.client is None or self.client.reg != ("docker.io" if R.is_hub(reg) else reg):
            self.client = R.Client("docker.io" if R.is_hub(reg) else reg)
        return self.client

    def _list(self, reg, ns):
        self.note.set_text("Loading repositories…")
        self.note.set_visible(True)

        def done(repos):
            self.repo_names.splice(0, self.repo_names.get_n_items(), repos)
            self.repo_pick.set_sensitive(bool(repos))
            self.note.set_text(f"{len(repos)} repositories" if repos else "No repositories here yet.")
            if len(repos) == 1:
                self._open(repos[0])
        bg(lambda: self._client(reg).repositories(ns), done=done,
           error=lambda e: (self.note.set_text(str(e)), self.note.set_visible(True)))

    def _typed(self, text):
        if not text:
            return
        reg = self._current_registry()
        if "/" in text or not R.is_hub(reg):
            self._open(text if "/" in text or not R.is_hub(reg) else f"library/{text}")
        else:
            self._list(reg, text)

    def _pick_repo(self):
        it = self.repo_pick.get_selected_item()
        if it and self.repo_pick.get_sensitive():
            self._open(it.get_string())

    def _open(self, repo):
        reg = self._current_registry()
        self.repo, self.gen = repo, self.gen + 1
        gen = self.gen
        self.wtitle.set_subtitle(f"{reg} · {repo}")
        self.stack.set_visible_child_name("loading")
        self.note.set_text("Loading tags…")
        self.note.set_visible(True)

        def done(tags):
            if gen != self.gen:
                return
            rows = [Row("tag", t, {"tag": t, "digest": "", "size": 0, "platforms": [], "created": "", "here": "…"})
                    for t in tags]
            self.set_rows(rows)
            self.stack.set_visible_child_name("list" if rows else "empty")
            self.note.set_text(f"{len(tags)} tags" + (" · details for the first 100 load in the background"
                                                       if len(tags) > 100 else ""))
            threading.Thread(target=self._details, args=(gen, reg, repo, rows[:100]), daemon=True).start()

        def fail(e):
            if gen == self.gen:
                self.stack.set_visible_child_name("empty")
                self.note.set_text(f"Couldn't load {repo}: {e}")
        bg(lambda: self._client(reg).tags(repo), done=done, error=fail)

    def _details(self, gen, reg, repo, rows):
        """Digest, size, platforms, created for each tag, one after another (registries rate-limit bursts)."""
        local = {}
        images = self.store.images
        if images is None:                        # the Images page hasn't loaded them yet
            try:
                images = self.store.engine.images()
            except Exception:  # noqa: BLE001
                images = []
        for im in images:
            for t in im.get("RepoTags") or []:
                local[t] = (im.get("RepoDigests") or []) + [im["Id"]]
        for r in rows:
            if gen != self.gen:
                return
            try:
                info = self._client(reg).info(repo, r.key)
            except Exception as e:  # noqa: BLE001
                info = {"error": str(e)}
            ref = R.ref_for(reg, repo, r.key)
            digests = local.get(ref)
            here = ("—" if digests is None else
                    "Same" if any(x and any(d.endswith(x) for d in digests) for x in (info.get("digest"),
                                                                                       info.get("config"))) else "Newer")
            GLib.idle_add(self._update, gen, r, info, here)

    def _update(self, gen, r, info, here):
        if gen == self.gen:
            r.data.update(info)
            r.data["here"] = here
            r.emit("changed")
        return False

    # ── table ──
    def build_columns(self):
        self.column("Tag", lambda r: r.data["tag"], ["monospace", "heading-sm"], expand=True, sort=lambda r: r.data["tag"])
        self.column("Digest", lambda r: r.data["digest"][7:19] if r.data.get("digest") else
                    ("error" if r.data.get("error") else "…"), ["monospace", "dim-label"], width=120,
                    tooltip=lambda r: r.data.get("error") or r.data.get("digest") or None)
        self.column("Size", lambda r: fmt_bytes(r.data["size"]) if r.data["size"] else "", ["numeric"], width=90,
                    sort=lambda r: r.data["size"], xalign=1.0)
        self.column("Platforms", lambda r: ", ".join(r.data["platforms"][:3]) +
                    (f" +{len(r.data['platforms']) - 3}" if len(r.data["platforms"]) > 3 else ""), ["dim-label"],
                    width=200, tooltip=lambda r: "\n".join(r.data["platforms"]) or None)
        self.column("Created", lambda r: r.data["created"][:16], ["dim-label"], width=150, sort=lambda r: r.data["created"])

        def here_setup():
            return Gtk.Label(halign=Gtk.Align.START, valign=Gtk.Align.CENTER, css_classes=["status-pill"])

        def here_upd(w, r):
            h = r.data["here"]
            w.set_text({"Same": "Up to date", "Newer": "Registry differs", "—": "Not pulled"}.get(h, "…"))
            w.set_css_classes(["status-pill", {"Same": "ok", "Newer": "accent"}.get(h, "muted")])
            w.set_tooltip_text({"Same": "This computer has exactly this version",
                                "Newer": "The registry's version differs from yours — pull to update",
                                "—": "Not on this computer"}.get(h))
        self.widget_column("On this computer", here_setup, here_upd, width=140)

        def act_setup():
            box = Gtk.Box(spacing=2, halign=Gtk.Align.END)
            box.row = None
            box.pull = icon_button("folder-download-symbolic", "Pull to this computer")
            box.pull.connect("clicked", lambda *_: box.row and self._pull(box.row))
            box.more = row_menu(box, [("Copy to another registry…", self._copy), ("Delete this tag…", self._delete)])
            box.append(box.pull)
            box.append(box.more)
            return box

        def act_upd(box, r):
            box.row = r
        self.widget_column("", act_setup, act_upd, width=90)

    def match(self, row, q):
        return q in row.data["tag"].lower()

    def _ref(self, r):
        return R.ref_for(self._current_registry(), self.repo, r.data["tag"])

    def _pull(self, r):
        ref = self._ref(r)
        CommandDialog(self.win, f"Pull {ref}", ["docker", "pull", ref],
                      on_done=lambda ok: ok and (self.store.refresh("images"), self._open(self.repo))).present(self.win)

    def _copy(self, r):
        src = self._ref(r)
        d = Adw.AlertDialog(heading=f"Copy {src}", body="To which name? Berth pulls it, tags it and pushes it there "
                                                         "(sign in to the target first if it needs it).")
        e = Gtk.Entry(text=f"localhost:5000/{self.repo.removeprefix('library/')}:{r.data['tag']}")
        e.add_css_class("monospace")
        d.set_extra_child(e)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Copy")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def go(_d, resp):
            dst = e.get_text().strip()
            if resp != "ok" or not dst:
                return
            q = shlex.quote
            CommandDialog(self.win, f"Copy to {dst}",
                          ["sh", "-c", f"docker pull {q(src)} && docker tag {q(src)} {q(dst)} && docker push {q(dst)}"]
                          ).present(self.win)
        d.connect("response", go)
        d.present(self.win)

    def _delete(self, r):
        tag, digest = r.data["tag"], r.data.get("digest")
        if not digest and not R.is_hub(self._current_registry()):
            return self.win.toast("Wait until the tag's digest has loaded")
        others = [x.data["tag"] for x in self._rows() if x is not r and x.data.get("digest") == digest]
        body = (f"<b>{esc(self._ref(r))}</b> is removed from the registry for everyone who uses it."
                + (f"\n\nThe same image is also tagged {esc(', '.join(others[:5]))} — those go too on registries "
                   "that delete by digest." if others and not R.is_hub(self._current_registry()) else ""))

        def go():
            reg = self._current_registry()
            bg(self._client(reg).delete, self.repo, tag, digest,
               done=lambda _r: (self.win.toast(f"Deleted {tag}"), self._open(self.repo)),
               error=lambda e: self.win.toast(f"Couldn't delete: {e}"))
        confirm(self.win, f"Delete tag {tag}?", body, "Delete", go)

    def _rows(self):
        return [self.model.get_item(i) for i in range(self.model.get_n_items())]

    def on_show(self):
        if not self.loaded:
            self.loaded = True
            self._pick_registry()
