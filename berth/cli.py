"""Run a docker CLI command (compose, push, buildx…) and stream its output into a dialog."""
import os
import signal
import subprocess
import threading

from gi.repository import Adw, GLib, Gtk, Pango

from .util import IS_WIN, esc, host


class CommandDialog(Adw.Dialog):
    """Shows `argv` running, live output, and the result. on_done(ok) is called on the UI thread."""

    def __init__(self, win, title, argv, cwd=None, on_done=None, autoclose=False):
        super().__init__(title=title, content_width=760, content_height=520)
        self.win, self.argv, self.on_done, self.autoclose = win, argv, on_done, autoclose
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.stop = Gtk.Button(label="Stop")
        self.stop.add_css_class("destructive-action")
        self.stop.connect("clicked", lambda *_: self._stop())
        hb.pack_end(self.stop)
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        cmd = Gtk.Label(label="$ " + " ".join(argv), xalign=0, wrap=True, wrap_mode=Pango.WrapMode.CHAR, selectable=True,
                        margin_start=14, margin_end=14, margin_top=8, css_classes=["monospace", "dim-label", "caption"])
        box.append(cmd)
        self.status = Gtk.Label(label="Running…", xalign=0, margin_start=14, css_classes=["heading"])
        box.append(self.status)
        self.text = Gtk.TextView(editable=False, monospace=True, left_margin=12, right_margin=12, top_margin=6,
                                 bottom_margin=10, wrap_mode=Gtk.WrapMode.CHAR)
        self.text.add_css_class("logview")
        self.sw = Gtk.ScrolledWindow(child=self.text, vexpand=True)
        box.append(self.sw)
        tv.set_content(box)
        self.set_child(tv)
        self.proc = None
        self.lines, self.lock = [], threading.Lock()
        self.timer = GLib.timeout_add(150, self._flush)
        threading.Thread(target=self._run, args=(cwd,), daemon=True).start()
        self.connect("closed", lambda *_: self._cleanup())

    def _run(self, cwd):
        env = dict(os.environ, COMPOSE_ANSI="never", BUILDKIT_PROGRESS="plain", NO_COLOR="1")
        try:
            kw = {} if IS_WIN else {"start_new_session": True}
            self.proc = subprocess.Popen(host(self.argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, bufsize=1, env=env, **kw)
        except OSError as e:
            with self.lock:
                self.lines.append(f"Could not start: {e}\n")
            GLib.idle_add(self._finished, 127)
            return
        for line in self.proc.stdout:
            with self.lock:
                self.lines.append(line)
        GLib.idle_add(self._finished, self.proc.wait())

    def _flush(self):
        with self.lock:
            q, self.lines = self.lines, []
        if q:
            buf = self.text.get_buffer()
            buf.insert(buf.get_end_iter(), "".join(q))
            adj = self.sw.get_vadjustment()
            GLib.idle_add(lambda: adj.set_value(adj.get_upper()) or False)
        return True

    def _finished(self, code):
        self._flush()
        ok = code == 0
        self.status.set_text("Done" if ok else f"Failed (exit code {code})")
        self.status.set_css_classes(["heading", "success" if ok else "error"])
        self.stop.set_label("Close")
        self.stop.set_css_classes(["suggested-action"] if ok else [])
        self.proc = None
        if self.on_done:
            self.on_done(ok)
        if ok and self.autoclose:
            GLib.timeout_add(900, lambda: (self.get_root() is not None and self.close()) and False)
        return False

    def _stop(self):
        if self.proc and self.proc.poll() is None:
            _stop(self.proc, signal.SIGINT)
            self.status.set_text("Stopping…")
        else:
            self.close()

    def _cleanup(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if self.proc and self.proc.poll() is None:
            _stop(self.proc, signal.SIGTERM)


def _stop(proc, sig):
    try:
        if IS_WIN:
            proc.terminate()
        else:
            os.killpg(proc.pid, sig)
    except OSError:
        pass


def run_capture(argv, timeout=60):
    """Blocking helper for short CLI calls; returns stdout or raises with stderr."""
    r = subprocess.run(host(argv), capture_output=True, text=True, timeout=timeout,
                       env=dict(os.environ, NO_COLOR="1", BUILDKIT_PROGRESS="plain"))
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[:500] or f"exit {r.returncode}")
    return r.stdout


def run_command_text(container_cfg):
    """The `docker run …` command that recreates a container (from its inspect data)."""
    import shlex
    cfg, hc = container_cfg.get("Config") or {}, container_cfg.get("HostConfig") or {}
    a = ["docker", "run", "-d", "--name", container_cfg.get("Name", "").lstrip("/")]
    pol = (hc.get("RestartPolicy") or {}).get("Name")
    if pol and pol != "no":
        a += ["--restart", pol]
    for k, binds in (hc.get("PortBindings") or {}).items():
        for b in binds or []:
            a += ["-p", f"{b.get('HostPort')}:{k.replace('/tcp', '')}"]
    for m in container_cfg.get("Mounts") or []:
        src = m.get("Name") if m.get("Type") == "volume" else m.get("Source")
        if m.get("Type") in ("volume", "bind"):
            a += ["-v", f"{src}:{m.get('Destination')}" + ("" if m.get("RW", True) else ":ro")]
    for e in cfg.get("Env") or []:
        a += ["-e", e]
    for n in ((container_cfg.get("NetworkSettings") or {}).get("Networks") or {}):
        if n not in ("bridge",):
            a += ["--network", n]
            break
    if hc.get("Memory"):
        a += ["--memory", str(hc["Memory"])]
    if hc.get("NanoCpus"):
        a += ["--cpus", str(hc["NanoCpus"] / 1e9)]
    if cfg.get("WorkingDir"):
        a += ["-w", cfg["WorkingDir"]]
    a.append(cfg.get("Image", ""))
    a += cfg.get("Cmd") or []
    return shlex.join(a)
