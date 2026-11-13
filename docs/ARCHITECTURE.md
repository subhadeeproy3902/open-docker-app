# Architecture

Berth is a single Python process: GTK 4 + libadwaita for the UI, the standard library for everything else.

```mermaid
flowchart TB
    subgraph UI["UI thread (GTK main loop)"]
        APP["app.py — window, sidebar, status bar, theme"]
        PAGES["containers.py · pages.py · logs.py · db_page.py · sharing_page.py · send_page.py · stacks_page.py · backups_page.py · k8s.py · settings_page.py"]
        WIDGETS["charts.py (cairo) · files.py · edit.py · compose.py · scan.py · palette.py (Ctrl+K) · quick.py"]
    end
    STORE["store.py — last known lists, refresh on events, GObject signals"]
    POOL["util.bg — 4 worker threads"]
    STREAMS["dedicated threads — events, logs, pull, exec"]
    SAMPLER["stats.py — cgroup v2 sampler, sleeps unless visible"]
    ENGINE["engine.py — HTTP over unix://, tcp://, npipe:// (Windows) or ssh:// (sshtunnel.py)"]
    BACKENDS["db.py · db_edit.py · dbtools.py (databases) · sharing.py (proxy, tunnels) · fileshare.py · backups.py · build.py · compose_check.py"]
    DOCKER[("Docker / Podman")]
    APP --> PAGES --> WIDGETS
    PAGES -- "requests" --> POOL --> ENGINE --> DOCKER
    POOL --> BACKENDS --> ENGINE
    DOCKER -- "events" --> STREAMS --> STORE -- "signals" --> PAGES
    SAMPLER -- "/sys/fs/cgroup" --> STORE
```

## Rules that keep it fast and light

1. **The UI thread never waits on Docker.** Requests run on a small thread pool (`util.bg`) and come back with
   `GLib.idle_add`. Long streams (events, logs, pulls, exec) have their own threads.
2. **Event-driven lists.** `store.py` listens to Docker's event stream with a server-side filter; `exec_*` health-check
   events are excluded, so a busy Compose stack doesn't cause constant reloads. Requests of the same kind are coalesced.
3. **In-place updates.** Containers live in a `Gtk.TreeListModel`; when the shape is unchanged only the rows' data and
   a `changed` signal are updated, so expansion and scroll position survive. CPU / memory cells are drawn with cairo
   instead of being `Gtk.Label`s, so a new number repaints one cell instead of re-measuring the table.
4. **Stats only while visible.** `stats.py` reads `cpu.stat`, `memory.current`, `memory.stat`, `io.stat` and
   `/proc/<pid>/net/dev` — a few file reads per container — every 1–5 s, and sleeps whenever the window is hidden or
   (by default) unfocused. Without cgroup v2 (macOS / Windows / remote engines) it falls back to the Engine's one-shot stats.
5. **No idle work.** Charts redraw only when a sample arrives; there are no animations or timers while idle; closing the
   window exits the process (unless the optional tray is on).

## Platform layer (`util.py`)

`IS_LINUX / IS_MAC / IS_WIN / IN_FLATPAK`, `host()` (prefixes `flatpak-spawn --host` inside the Flatpak so `docker`,
`kubectl`, `kind` and `pkexec` run on the host), `run_in_terminal()` (Linux terminals, macOS Terminal via AppleScript,
Windows `cmd`), and `themed()` (icon fallbacks for themes that lack an icon).

## Theming (`theme.py`)

Desktops that recolour GTK 4 through `~/.config/gtk-4.0/gtk.css` (matugen, pywal, Gradience…) override libadwaita at
USER priority, which makes libadwaita's own light/dark switch invisible. When the user picks Light or Dark in Berth and
the desktop palette points the other way, Berth loads a complete Adwaita palette one step above USER priority.

## Editing a container safely (`edit.py`)

Docker can update name, restart policy and CPU / memory limits in place. Everything else needs a new container:
stop → rename the old one aside → create the replacement with the same networks, aliases, labels and volumes → start →
verify it runs → delete the old one. Any failure removes the replacement and restores the original.

## Volumes and scanning

Volume browsing, export, import and clone use a throwaway `busybox` helper container (labelled
`io.github.subhadeeproy3902.berth.helper=volume`, auto-removed). Vulnerability scans run `aquasec/trivy` as a container
with its database cached in the `berth-trivy-cache` volume. Every scan's counts are kept in `~/.local/state/berth/scans.json` (last 10 per
image) for the Security column and the scan window's comparison. **Safe updates** scan the current and the new version
and re-tag the current one back when the new one adds critical / high findings.

## Alerts and scheduled scans (`watch.py`)

One timer in `app.py` ticks once a minute. With every alert off it does nothing. Otherwise it reads `cpu.stat` and
`memory.*` from each running container's cgroup (one stats call per container on non-local engines) and
`shutil.disk_usage` of Docker's data folder; an alert repeats at most once an hour (disk: every six hours).
The same tick runs the daily image-update check app-wide and the daily / weekly scan of the images in use.

## Databases (`db.py`, `db_page.py`)

One `Backend` per engine with the same small interface — `connect`, `databases`, `tree` / `children`, `browse`,
`structure`, `query`, `is_destructive` — so the UI (`db_page.Explorer`) is engine-agnostic. Detection is by image name
(with an env fallback), see `db.IMAGES`.

- **Transport.** SQL / Redis / MongoDB run the client that ships in the image through `docker exec`; credentials come
  from the container's env or its `*_FILE` secret and are passed as env of that exec only. HTTP engines go through
  `db.Http`: published port → container IP (local Linux engines) → curl / wget inside the container.
- **Read-only first.** The UI passes `allow_write=True` only while the user's “Allow changes” switch is on (reset when
  the page closes). Each backend enforces read-only at the lowest level it can (server-side read-only transactions for
  Postgres, read-only session + statement check for MySQL, allow-lists for Redis and HTTP, EJSON-only filters and no
  shell for MongoDB).
- **Editing (`db_edit.py`).** One `Editor` per engine: `annotate` decides how a row is picked out (primary key, a
  hash field, a document id) or says why it can't be edited; `fetch` returns every value in full; `save` / `delete`
  change one row and report a row that changed meanwhile instead of overwriting it. Values travel as data — quoted
  literals, hex literals (MySQL), separate argv items (Redis), env variables (MongoDB), JSON bodies — and through a
  file in the container's `/tmp` when they are too large for a command line.
- **Whole tables and databases (`dbtools.py`).** Export streams `row_to_json` / `JSON_OBJECT` / `EJSON` lines straight
  to a file (`engine.exec_to_file`), then writes CSV or JSON. Import goes through `\copy` (Postgres), batched hex
  `INSERT`s in one transaction (MySQL) or `insertMany` (MongoDB). Backups use the image's own dump tool
  (`pg_dump -Fc`, `mysqldump`, `mongodump --archive`, `redis-cli --rdb`); restores use the matching restore tool (Redis:
  stop, put the RDB in place, start).
- **Reaching HTTP engines** without curl in the image: a short-lived `curlimages/curl` helper in the target's network
  namespace (`NetworkMode: container:<id>`, labelled `…berth.helper=http`) — needed on Docker Desktop, where container
  IPs aren't routable.
- **SQLite** is a copy: fetched with the archive API (plus `-wal` / `-shm`), folded into a private temp file and
  opened `mode=ro`; removed when the explorer closes.

## Ports & sharing (`sharing.py`, `sharing_page.py`)

- **Local addresses**: `berth-proxy` (caddy:2-alpine), published on 127.0.0.1 only, CA in the `berth-proxy-data`
  volume. Routes live in Berth's settings; `apply_routes` connects the proxy to a network of each target, writes the
  Caddyfile with the archive API and runs `caddy reload` inside the container (the admin API is never published). A
  route signature (target state + IPs) is checked on container events and re-applied when it changes.
- **Public links**: one `cloudflared` container per shared port with `NetworkMode: container:<target>`, labelled
  `io.berth.role=tunnel`; the quick-tunnel URL is read from its log. `App.do_shutdown` removes every tunnel not
  labelled `io.berth.keep=1`.

## Backups (`backups.py`, `backups_page.py`)

Schedules live in settings; what happened (last run, result) in a separate state file, so the app and
`berth --run-backups` (a systemd user timer on Linux, installed only once a schedule exists) never overwrite each
other. A lock file keeps two runs apart; files are named `<target>[-<db>]-<timestamp>.<ext>`, so pruning to "keep the
newest N" only ever touches a schedule's own files.

## Stacks and the Compose check (`stacks_page.py`, `compose_check.py`)

Stacks come from container labels plus the projects Berth started (remembered in settings, so a stack that was taken
down stays listed). The check runs `docker compose config --format json` (syntax, interpolation, unset variables) and
then compares the result with this computer: published ports in use by other containers or programs, taken container
names, build folders, env files, external volumes and networks.

## Engines on other machines and systems

- **SSH** (`sshtunnel.py`): one `ssh -N -L 127.0.0.1:<port>:/var/run/docker.sock` per remote engine with the system's
  OpenSSH and the user's keys; Berth then talks to the local port. The docker CLI follows `DOCKER_HOST=ssh://…` itself.
- **Windows named pipe** (`engine._PipeSock`): Docker Desktop's `\\.\pipe\docker_engine` wrapped so `http.client` and
  streams can use it — reads on a helper thread once the request is written (a synchronous pipe can't read and write
  at once), timeouts without `select()`, and `close()` cancels a waiting read.

## Testing

`tests/connect.py` (every engine against real Docker), `tests/drive.py` and `tests/clickall.py` (the app driven
off-screen against the demo engine from `tools/demo/up.sh`; both refuse any other engine), `tools/ui_audit.py` and
`tools/walkthrough.py` (screenshots of every screen and every engine, `tools/demo/engines.py` starts one seeded engine
at a time), `tests/ssh.py`, `tests/fileshare.py`, `tools/pipe_check.py` (Windows), `tools/distro_test.sh` (five Linux
distributions through the installer) and `berth --self-test`. CI runs all of them.
