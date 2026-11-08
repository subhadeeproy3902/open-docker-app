"""The data hub. Holds the last known lists, refreshes them only when Docker says
something changed (event stream, health-check noise filtered out), and tells the
pages through GObject signals. Pages never talk to the Engine for lists directly."""
import json
import time

from gi.repository import GLib, GObject

from .engine import Engine, EngineError
from .stats import Sampler
from .util import bg, cname, labels, thread

# Events that change what the lists show. exec_* (health checks) are left out on purpose:
# they fire every few seconds per container and used to make the old app reload constantly.
EVENTS = ["create", "destroy", "die", "kill", "oom", "pause", "unpause", "rename", "restart", "start",
          "stop", "update", "health_status", "pull", "delete", "tag", "untag", "import", "load", "prune",
          "remove"]


class Store(GObject.Object):
    __gsignals__ = {
        "engine": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "containers": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "images": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "volumes": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "networks": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "stats": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, address=None):
        super().__init__()
        import os
        from .engine import docker_host_env
        self.engine = Engine(address or None)
        os.environ["DOCKER_HOST"] = docker_host_env(self.engine.path)   # docker CLI calls (compose, exec…) follow along
        self.sampler = Sampler(self.engine, self._on_sample)
        self.up = None
        self.info = {}
        self.containers = []
        self.images = None
        self.volumes = None
        self.networks = None
        self._pending = set()
        self._flush_id = 0
        self._events_gen = 0
        self._busy = set()
        GLib.timeout_add_seconds(3, self._watch_engine)
        GLib.timeout_add_seconds(30, self._tick)       # keeps "Up 5 min" texts fresh
        self._watch_engine()

    def switch(self, address):
        """Talk to another engine (Docker / rootless / Podman / remote tcp)."""
        import os
        from .engine import docker_host_env
        self._events_gen += 1
        self.engine = Engine(address)
        self.sampler.engine = self.engine
        self.sampler.set_running({})
        os.environ["DOCKER_HOST"] = docker_host_env(address)
        self.up = None
        self.containers, self.images, self.volumes, self.networks, self.info = [], None, None, None, {}
        self.emit("containers")
        self._watch_engine()

    # ── engine up/down ──
    def _watch_engine(self):
        def done(alive):
            if alive != self.up:
                self.up = alive
                if alive:
                    self._start_events()
                    self.refresh("containers")
                    bg(self.engine.info, done=self._set_info, error=lambda e: None)
                else:
                    self._events_gen += 1
                    self.containers, self.images, self.volumes, self.networks = [], None, None, None
                    self.sampler.set_running({})
                self.emit("engine", alive)
        bg(self.engine.alive, done=done, error=lambda e: done(False))
        return True

    def _set_info(self, info):
        self.info = info or {}

    def _tick(self):
        if self.up:
            self.refresh("containers")
        return True

    # ── refresh ──
    def refresh(self, what):
        """Ask for a refresh; repeated asks within 250 ms collapse into one."""
        self._pending.add(what)
        if not self._flush_id:
            self._flush_id = GLib.timeout_add(250, self._flush)

    def _flush(self):
        self._flush_id = 0
        todo, self._pending = self._pending, set()
        for what in todo:
            if what in self._busy:          # one request of a kind at a time
                self._pending.add(what)
                self._flush_id = self._flush_id or GLib.timeout_add(400, self._flush)
                continue
            self._busy.add(what)
            fn = {"containers": self.engine.containers, "images": self.engine.images,
                  "volumes": self.engine.volumes, "networks": self.engine.networks}[what]
            bg(fn, done=lambda r, w=what: self._got(w, r), error=lambda e, w=what: self._failed(w, e))
        return False

    def _got(self, what, result):
        self._busy.discard(what)
        setattr(self, what, result or [])
        if what == "containers":
            self.sampler.set_running({c["Id"]: cname(c) for c in self.containers if c.get("State") == "running"})
        self.emit(what)

    def _failed(self, what, err):
        self._busy.discard(what)
        if isinstance(err, EngineError) and err.status == 0:
            self._watch_engine()

    # ── events ──
    def _start_events(self):
        self._events_gen += 1
        thread(self._events_loop, self._events_gen)

    def _events_loop(self, gen):
        filters = {"type": ["container", "image", "volume", "network"], "event": EVENTS}
        while gen == self._events_gen:
            try:
                st = self.engine.events(filters)
            except Exception:  # noqa: BLE001
                time.sleep(3)
                continue
            buf = b""
            while gen == self._events_gen:
                chunk = st.read(2.0)
                if chunk is None:
                    continue
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                kinds = set()
                for ln in lines:
                    try:
                        kinds.add(json.loads(ln).get("Type"))
                    except ValueError:
                        pass
                if kinds:
                    GLib.idle_add(self._on_events, kinds)
            st.close()
            time.sleep(1)

    def _on_events(self, kinds):
        m = {"container": "containers", "image": "images", "volume": "volumes", "network": "networks"}
        for k in kinds:
            what = m.get(k)
            if what == "containers" or getattr(self, what or "", None) is not None:
                self.refresh(what)
        if "container" in kinds and self.images is not None:
            self.refresh("images")          # "in use" counts change when containers come and go
        return False

    # ── stats ──
    def _on_sample(self):
        self.emit("stats")
        return False

    def want_stats(self, on):
        (self.sampler.want.set if on else self.sampler.want.clear)()

    # ── helpers for pages ──
    def project_of(self, c):
        return labels(c).get("com.docker.compose.project", "")

    def by_id(self, cid):
        return next((c for c in self.containers if c["Id"] == cid), None)
