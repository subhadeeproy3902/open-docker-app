"""Local AI (#24): open-weight models through an Ollama container — pick a model by size and memory, download it with
live progress, chat with it, and plug its OpenAI-compatible address into other apps. Runs on the CPU unless the user
switches on the NVIDIA GPU (only offered when the engine has the nvidia runtime). Berth stops the container after a
while without use (Settings → Local AI), so it doesn't hold memory."""
import json
import os
import time

from gi.repository import Adw, GLib, Gtk, Pango

from .cli import CommandDialog
from .util import PageHeader, bg, esc, flat_page, fmt_bytes, label
from .util import set_busy  # noqa: E402

NAME, IMAGE, VOLUME, PORT = "berth-ollama", "ollama/ollama:latest", "berth-ollama-models", 11434
ROLE = "io.github.subhadeeproy3902.berth.role"
LAST_USE = os.path.join(GLib.get_user_state_dir(), "berth", "ai-last-use")
# (model, what it is, download size, memory it needs to run) — small ones first
CATALOG = [("qwen2.5:0.5b", "Qwen 2.5 0.5B — tiny, quick answers", 0.40e9, 1.0e9),
           ("smollm2:1.7b", "SmolLM2 1.7B — small general model", 1.8e9, 2.5e9),
           ("llama3.2:1b", "Llama 3.2 1B — small general model", 1.3e9, 2.0e9),
           ("gemma3:1b", "Gemma 3 1B — small general model", 0.8e9, 1.5e9),
           ("qwen2.5:1.5b", "Qwen 2.5 1.5B — good small all-rounder", 1.0e9, 2.0e9),
           ("qwen2.5-coder:1.5b", "Qwen 2.5 Coder 1.5B — code", 1.0e9, 2.0e9),
           ("llama3.2:3b", "Llama 3.2 3B — better answers, still laptop-sized", 2.0e9, 4.0e9),
           ("phi4-mini", "Phi-4 mini 3.8B — reasoning", 2.5e9, 5.0e9),
           ("mistral:7b", "Mistral 7B — needs a strong laptop", 4.1e9, 8.0e9),
           ("llama3.1:8b", "Llama 3.1 8B — needs a strong laptop or a GPU", 4.9e9, 9.0e9)]


def touch():
    try:
        os.makedirs(os.path.dirname(LAST_USE), exist_ok=True)
        with open(LAST_USE, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def last_use():
    try:
        with open(LAST_USE) as f:
            return float(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def find(engine):
    return next((c for c in engine.containers() if f"/{NAME}" in (c.get("Names") or [])), None)


def has_nvidia(info):
    return "nvidia" in (info.get("Runtimes") or {})


def free_memory(info, local):
    """Memory a model can use: available RAM on this computer (local engines), else the engine's total."""
    if local:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
        except OSError:
            pass
    return info.get("MemTotal") or 0


def start(engine, gpu=False, progress=None):
    """Create (first time) and start the Ollama container: port 11434 on this computer only, models in a volume."""
    from .sharing import ensure_image
    c = find(engine)
    if c is None:
        ensure_image(engine, IMAGE, progress)
        hc = {"PortBindings": {f"{PORT}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(PORT)}]},
              "Binds": [f"{VOLUME}:/root/.ollama"], "RestartPolicy": {"Name": "no"}}
        if gpu:
            hc["DeviceRequests"] = [{"Driver": "nvidia", "Count": -1, "Capabilities": [["gpu"]]}]
        cid = engine.create_container(NAME, {"Image": IMAGE, "ExposedPorts": {f"{PORT}/tcp": {}},
                                             "Env": ["OLLAMA_KEEP_ALIVE=5m"], "Labels": {ROLE: "ollama"},
                                             "HostConfig": hc})["Id"]
    else:
        cid = c["Id"]
    if not c or c.get("State") != "running":
        engine.action(cid, "start")
    touch()
    return cid


def stop(engine):
    c = find(engine)
    if c and c.get("State") == "running":
        engine.action(c["Id"], "stop")


_CLIENTS = {}                 # Ollama container id → its HTTP client (one helper at most, reused by every request)


def _http(engine):
    from .db import Backend, Http
    c = find(engine)
    if not c or c.get("State") != "running":
        raise RuntimeError("Ollama isn't running — press Start")
    h = _CLIENTS.get(c["Id"])
    if h is None:
        h = _CLIENTS[c["Id"]] = Http(Backend(engine, engine.inspect(c["Id"])), PORT, scheme="http")
    return h


def installed(engine):
    """[(name, size)] of the downloaded models."""
    status, body = _http(engine).request("GET", "/api/tags", timeout=10)
    if status != 200:
        raise RuntimeError(f"Ollama answered {status}")
    return [(m["name"], m.get("size", 0)) for m in json.loads(body).get("models") or []]


def chat(engine, model, messages):
    touch()
    status, body = _http(engine).request("POST", "/api/chat", {"model": model, "messages": messages, "stream": False},
                                         timeout=600)
    touch()
    if status != 200:
        try:
            raise RuntimeError(json.loads(body).get("error") or f"Ollama answered {status}")
        except ValueError:
            raise RuntimeError(f"Ollama answered {status}") from None
    d = json.loads(body)
    secs = (d.get("eval_duration") or 0) / 1e9
    rate = f"{d.get('eval_count', 0) / secs:.1f} tokens/s" if secs else ""
    return (d.get("message") or {}).get("content", ""), rate


def delete(engine, model):
    status, body = _http(engine).request("DELETE", "/api/delete", {"model": model}, timeout=60)
    if status != 200:
        raise RuntimeError(body[:200] or f"Ollama answered {status}")


class AiPage(Gtk.Box):
    title, icon = "Local AI", "system-run-symbolic"
    wants_stats = False

    def __init__(self, win, store, settings):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.store, self.settings = win, store, settings
        self.messages, self.model = [], None
        tv = Adw.ToolbarView(vexpand=True)
        self.header = PageHeader("Local AI", "Open models on this computer with Ollama — no account, nothing leaves "
                                             "your machine")
        self.btn = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
        self.btn.add_css_class("suggested-action")
        self.btn.connect("clicked", lambda *_: self._toggle())
        self.header.add(self.btn)
        flat_page(tv, self.header)
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=440, shrink_start_child=False,
                          css_classes=["page-body"])
        # left: status, endpoint, models
        self.pp = Adw.PreferencesPage()
        self.state = Adw.PreferencesGroup(title="Ollama")
        self.state_row = Adw.ActionRow(title="Status", subtitle="Checking…")
        self.state.add(self.state_row)
        self.endpoint = Adw.ActionRow(title="OpenAI-compatible address", subtitle=f"http://localhost:{PORT}/v1",
                                      subtitle_selectable=True)
        cp = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy", valign=Gtk.Align.CENTER)
        cp.add_css_class("flat")
        cp.connect("clicked", lambda *_: (self.get_clipboard().set(f"http://localhost:{PORT}/v1"),
                                          self.win.toast("Copied")))
        self.endpoint.add_suffix(cp)
        self.state.add(self.endpoint)
        self.gpu = Adw.SwitchRow(title="Use the NVIDIA GPU", subtitle="Faster, but keeps the GPU awake. Applies when "
                                 "Ollama is created (delete the container to change it).", visible=False)
        self.state.add(self.gpu)
        self.pp.add(self.state)
        self.mine = Adw.PreferencesGroup(title="Downloaded models")
        self.mine_rows = []
        self.pp.add(self.mine)
        self.cat = Adw.PreferencesGroup(title="Get a model", description="Small models run fine on a laptop CPU. The "
                                        "memory column is what a model needs while it answers.")
        self.cat_rows = []
        self.pp.add(self.cat)
        paned.set_start_child(self.pp)
        # right: chat
        chatbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12)
        top = Gtk.Box(spacing=8)
        self.model_names = Gtk.StringList()
        self.model_pick = Gtk.DropDown(model=self.model_names, tooltip_text="Model to chat with")
        top.append(label("Chat with", css=["heading"]))
        top.append(self.model_pick)
        clear = Gtk.Button(label="New chat")
        clear.connect("clicked", lambda *_: self._clear())
        top.append(Gtk.Box(hexpand=True))
        top.append(clear)
        chatbox.append(top)
        self.log = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=12, margin_end=12,
                           margin_top=12, margin_bottom=12)
        self.log_sw = Gtk.ScrolledWindow(child=self.log, vexpand=True)
        card = Gtk.Box(css_classes=["card"], overflow=Gtk.Overflow.HIDDEN)
        card.append(self.log_sw)
        self.log_sw.set_hexpand(True)
        chatbox.append(card)
        row = Gtk.Box(spacing=6)
        self.entry = Gtk.Entry(placeholder_text="Ask something…", hexpand=True)
        self.entry.connect("activate", lambda *_: self._send())
        self.send = Gtk.Button(label="Send")
        self.send.add_css_class("suggested-action")
        self.send.connect("clicked", lambda *_: self._send())
        row.append(self.entry)
        row.append(self.send)
        chatbox.append(row)
        self.rate = label("", css=["caption", "dim-label"])
        chatbox.append(self.rate)
        paned.set_end_child(chatbox)
        tv.set_content(paned)
        self.append(tv)
        self._clear()

    def on_show(self):
        self.refresh()

    # ── state ──
    def refresh(self):
        eng, info = self.store.engine, dict(self.store.info or {})
        self.gpu.set_visible(has_nvidia(info) and find_cached(self) is None)

        def work():
            c = find(eng)
            models = installed(eng) if c and c.get("State") == "running" else []
            return c, models
        def fail(e):
            self._show((find_cached(self), []))
            self.state_row.set_subtitle(f"Couldn't reach Ollama: {e}")
        bg(work, done=self._show, error=fail)

    def _show(self, res):
        c, models = res
        self._c = c
        running = bool(c and c.get("State") == "running")
        self.state_row.set_subtitle("Running — port 11434 on this computer only" if running else
                                    "Stopped" if c else "Not set up yet — Start downloads Ollama (about 1.5 GB)")
        self.btn.set_label("Stop" if running else "Start")
        self.btn.set_css_classes(["destructive-action" if running else "suggested-action"])
        for r in self.mine_rows:
            self.mine.remove(r)
        self.mine_rows = []
        for name, size in models:
            r = Adw.ActionRow(title=esc(name), subtitle=fmt_bytes(size))
            rm = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete this model", valign=Gtk.Align.CENTER)
            rm.add_css_class("flat")
            rm.connect("clicked", lambda *_a, n=name: self._delete(n))
            r.add_suffix(rm)
            self.mine.add(r)
            self.mine_rows.append(r)
        if not models:
            r = Adw.ActionRow(title="None yet", subtitle="Get one below" if running else "Start Ollama first")
            self.mine.add(r)
            self.mine_rows.append(r)
        names = [n for n, _s in models]
        cur = self.model_pick.get_selected_item().get_string() if self.model_pick.get_selected_item() else None
        self.model_names.splice(0, self.model_names.get_n_items(), names)
        if cur in names:
            self.model_pick.set_selected(names.index(cur))
        self.send.set_sensitive(running and bool(names))
        self._catalog(running, set(names))

    def _catalog(self, running, have):
        for r in self.cat_rows:
            self.cat.remove(r)
        self.cat_rows = []
        info = dict(self.store.info or {})
        local = self.store.engine.path.startswith(("/", "unix://"))
        free = free_memory(info, local)
        for name, what, dl, ram in CATALOG:
            if name in have or (":" not in name and f"{name}:latest" in have):
                continue
            tight = free and ram > free
            r = Adw.ActionRow(title=esc(name), subtitle=esc(f"{what} · download {fmt_bytes(dl)} · needs about "
                                                             f"{fmt_bytes(ram)} of memory"
                                                             + (" — more than is free now" if tight else "")))
            if tight:
                r.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", tooltip_text="Not enough free memory"))
            b = Gtk.Button(label="Get", valign=Gtk.Align.CENTER, sensitive=running)
            b.connect("clicked", lambda *_a, n=name, t=tight: self._pull(n, t))
            r.add_suffix(b)
            self.cat.add(r)
            self.cat_rows.append(r)

    def _toggle(self):
        running = bool(getattr(self, "_c", None) and self._c.get("State") == "running")
        set_busy(self.btn, True)
        self.state_row.set_subtitle("Stopping…" if running else "Starting… (the first time downloads Ollama)")
        say = lambda t: GLib.idle_add(lambda: self.state_row.set_subtitle(t) and False)
        fn = (lambda: stop(self.store.engine)) if running else \
            (lambda: start(self.store.engine, self.gpu.get_active(), say))

        def done(_r):
            set_busy(self.btn, False)
            GLib.timeout_add(1500, lambda: self.refresh() and False)
        bg(fn, done=done, error=lambda e: (set_busy(self.btn, False), self.win.toast(f"Ollama: {e}")))

    def _pull(self, name, tight):
        def go():
            touch()
            CommandDialog(self.win, f"Download {name}", ["docker", "exec", NAME, "ollama", "pull", name],
                          on_done=lambda ok: (touch(), self.refresh())).present(self.win)
        if tight:
            from .util import confirm
            confirm(self.win, f"{name} needs more memory than is free",
                    "It may run very slowly or fail. Close other apps first, or pick a smaller model.",
                    "Download anyway", go, destructive=False)
        else:
            go()

    def _delete(self, name):
        from .util import confirm
        confirm(self.win, f"Delete {name}?", "The download is removed; get it again any time.", "Delete",
                lambda: bg(delete, self.store.engine, name, done=lambda _r: self.refresh(),
                           error=lambda e: self.win.toast(str(e))))

    # ── chat ──
    def _clear(self):
        self.messages = []
        while (c := self.log.get_first_child()) is not None:
            self.log.remove(c)
        self.log.append(label("Answers come from the model on this computer. Small models make mistakes — check what "
                              "matters.", css=["dim-label", "caption"], wrap=True))
        self.rate.set_text("")

    def _bubble(self, who, text):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                    halign=Gtk.Align.END if who == "you" else Gtk.Align.START)
        b.append(label(who, css=["caption", "dim-label"]))
        t = Gtk.Label(label=text, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True,
                      max_width_chars=70, css_classes=["card", "chat-bubble"] + (["mine"] if who == "you" else []))
        b.append(t)
        self.log.append(b)
        GLib.idle_add(lambda: self.log_sw.get_vadjustment().set_value(self.log_sw.get_vadjustment().get_upper()) and False)
        return t

    def _send(self):
        text = self.entry.get_text().strip()
        it = self.model_pick.get_selected_item()
        if not text or not it:
            return
        self.entry.set_text("")
        self._bubble("you", text)
        self.messages.append({"role": "user", "content": text})
        bubble = self._bubble(it.get_string(), "…")
        set_busy(self.send, True)
        self.rate.set_text("Thinking… (the first answer loads the model into memory)")

        def done(r):
            content, rate = r
            bubble.set_text(content.strip() or "(no answer)")
            self.messages.append({"role": "assistant", "content": content})
            set_busy(self.send, False)
            self.rate.set_text(rate)

        def fail(e):
            bubble.set_text(f"Error: {e}")
            set_busy(self.send, False)
            self.rate.set_text("")
        bg(chat, self.store.engine, it.get_string(), list(self.messages), done=done, error=fail)


def find_cached(page):
    return getattr(page, "_c", None)
