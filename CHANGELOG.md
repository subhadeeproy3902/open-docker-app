# Changelog

All notable changes to Berth are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added — Connect (sidebar section)
- **Databases** (#25): find database containers and explore them — PostgreSQL (incl. PostGIS, TimescaleDB, pgvector),
  MySQL / MariaDB, Redis / Valkey, MongoDB, Vespa, OpenSearch / Elasticsearch, Typesense, Meilisearch, and SQLite files
  from any container or volume.
  - Tree of schemas / tables / keys / collections / indices with row estimates and sizes; paged data; structure
    (columns, types, constraints, indexes; Vespa `.sd` schemas; mappings); a query console (SQL, Redis commands, YQL,
    `METHOD /path` + JSON) with Ctrl+Enter and per-database history; export to CSV / JSON / Markdown; row inspector.
  - No drivers, no published ports: Berth runs the client inside the image (`psql`, `mariadb`, `redis-cli`, `mongosh`)
    or speaks HTTP to the container. Passwords come from the container's env (or `*_FILE` secrets) and travel as env
    variables of the exec, never in argv.
  - **Read-only by default**, enforced as low as possible: Postgres in read-only transactions, MySQL
    `SESSION TRANSACTION READ ONLY` plus a statement check, read-only command lists for Redis / HTTP engines, Mongo
    filters are data (EJSON) and the shell needs “Allow changes”. Destructive statements ask again even then.
  - SQLite opens a private copy (including recent WAL writes); the original is never touched.
- **Ports & sharing** (#26, #27):
  - **Local addresses**: `http(s)://name.localhost` for any container port — one opt-in Caddy container published on
    127.0.0.1 only, reaching targets by name (user networks) or IP (default bridge, re-pointed after restarts). HTTPS
    with a local CA you can export and trust.
  - **Public links**: share a port with a Cloudflare quick tunnel (no account) or your own tunnel token; `cloudflared`
    runs in the target's network namespace. A warning first, a status-bar marker while anything is public, and links
    stop when Berth quits unless you keep them.
  - **Ports in use**: every published port, flagged when it is open to the whole network (0.0.0.0).
- Container menu: *Browse database*, *Give it a local address…*, *Share publicly…*.
- `tests/connect.py`: integration test against a real engine (every database engine, read-only enforcement, SQLite +
  WAL, proxy over HTTP/HTTPS, IP change after restart, public tunnel fetched over the internet); runs in CI without
  the tunnel.

## [1.2.1] - 2026-11-09

### Fixed
- **Older GTK (Debian 13, MX Linux 25, Ubuntu 25.04, GNOME 48 Flatpak):** "The old GL renderer has been removed" and a
  slow start. GTK 4.14–4.19 call the fast renderer `ngl`; 4.20+ call it `gl`. Berth now picks the right name.
- **Overlapping or cut-off layout on small windows and large fonts** (reported on MX Linux):
  - the welcome footer: the button could cover the step dots;
  - every page was as wide as the widest page (Logs, 764 px with DejaVu Sans 11), so narrow windows cut the right side off;
  - toolbars now wrap onto a second line instead of overflowing (Containers, Images, Volumes, Logs, Kubernetes …);
  - time-range and filter buttons no longer squeeze their labels into "1…";
  - Resource usage: tiles switch between 4 across and 2 × 2, charts stack, the time range moved to the toolbar row;
  - the Logs histogram shrinks on short windows; welcome steps scroll so Back / Continue stay visible.
  Checked at the minimum window size (760 × 520) with DejaVu Sans 11 at 115 % text.

> The `Theme parser error: gtk.css:8625 … Expected a valid color` warnings some people see come from their desktop
> theme's own `gtk.css`, not from Berth. They are harmless.

## [1.2.0] - 2026-11-08

### Added
- **Welcome on first run**, four short steps, all skippable:
  1. What Berth is.
  2. **Connect your engine**: lists every engine found (Docker, rootless, Podman, Colima, OrbStack, Rancher, contexts)
     with its live state, and says exactly what to do: start it (one click, polkit), join the `docker` group (with a
     copy button), install one (per platform), or type a remote address.
  3. **Make it yours**: theme, text size, density, tray.
  4. **Run your first container**: a small sample web page on `localhost:8088`, with keyboard tips.
  Reopen it any time: menu → *Welcome tour*. People upgrading keep their settings and are not shown it.
- Empty Containers page offers **Run a sample container** and **Pull an image** instead of a dead end.

### Changed
- **Opens in about a second** (it took 3 s or more on hybrid-graphics laptops). Berth now draws with OpenGL instead of GTK's default Vulkan. Creating a Vulkan
  instance loads every GPU driver; on hybrid-graphics laptops it took ~2 s and woke the sleeping discrete GPU
  (measured: GL 80 ms, Vulkan 2,065 ms). `GSK_RENDERER` still overrides it.
- Listing engines (`docker context ls`) no longer runs on the UI thread at startup.
- Credits: the About window, LICENSE, READMEs and package metadata now credit **The Berth contributors**. Conduct
  and security reports go through GitHub's private reporting instead of a personal e-mail address.

### Fixed
- Wrapped text in dialogs could be cut off with "…".
- The Containers page kept saying "Nothing matches" after a search was cleared.

## [1.1.1] - 2026-11-08

### Changed
- **New logo**: a container ship on a wave. It is now the app icon everywhere: Linux app menus and launchers, the
  macOS `.app` (drawn on Apple's icon grid), the Windows `.exe` (16–256 px), the tray, the About window, the README
  and the release page. Every size is generated from one artwork by `tools/make_icons.py`.
- Each release now also carries `berth-logo.png` (1024 px).

## [1.1.0] - 2026-11-08

### Changed — a redesigned interface
- Every page now has the same frame: a large title with a live subtitle ("6 running · 8 total"), actions on the right,
  a toolbar row underneath, and tables inside a rounded card instead of running edge to edge.
- One spacing system (8-point grid) drives page margins, row height, cell padding and card padding everywhere.
- Sidebar grouped into Manage / Observe / Build / Orchestrate, with an engine card at the bottom; a status bar with
  engine state, CPU, RAM and free disk.
- Table headers are quieter (small caps); live CPU / memory numbers use monospaced digits and size themselves from the
  font, so they never clip at any text size.
- Logs explorer, Resource usage and Disk usage aligned to the same margins.

### Added — Settings → Accessibility
- **Text size** 85 / 100 / 115 / 130 / 150 % (scales every label, table and chart; independent of the desktop setting).
- **Density**: compact, comfortable, spacious.
- **Stronger borders and contrast**: clear outlines on cards, tables, buttons and inputs, a thick focus ring, less dimmed text.
- **Larger icons** in the sidebar, toolbars and tables.
- **Reduce motion**: turns off sliding and fading animations.
- All apply instantly, without a restart.

### Docs
- Step-by-step install guides: [docs/INSTALL-macOS.md](docs/INSTALL-macOS.md) and
  [docs/INSTALL-Windows.md](docs/INSTALL-Windows.md) (no Apple account, payment or certificate needed).
- New screenshots.

### Fixed
- macOS: the app is now ad-hoc signed in CI, which avoids the "Berth is damaged" message on Apple Silicon.
- Status bar: no empty dividers while the engine is stopped.

## [1.0.1] - 2026-11-08

### Fixed
- Builds: opening a build showed "no record found" — `buildx history ls` reports refs as `builder/node/id`, but inspect,
  logs and delete only accept the id. Build logs (which buildx writes to stderr) now show in full.
- Containers: the CPU column was clipped on narrow windows.

### Added
- Find unhealthy containers at a glance: an **All · Running · Unhealthy · Stopped** filter with counts, the unhealthy count
  in the Containers title, a red marker on stacks that contain one (they open automatically), and a clickable
  "⚠ N unhealthy" alert in the status bar.
- `install.sh`: `BERTH_ICON=docker-desktop` keeps your icon theme's Docker icon instead of Berth's own.

### Changed
- CI: newer actions (checkout 7, setup-python 7, upload-artifact 7, download-artifact 8, action-gh-release 3);
  losslessly optimized images (ImgBot).

## [1.0.0] - 2026-11-08

The first public release.

### Highlights
- Native GTK 4 + libadwaita Docker desktop: ~110 MB RAM while open, ~1 MB installed, nothing running when closed.
- Containers grouped by Compose project with live CPU / memory, start / stop / restart / pause / delete per container or stack.
- Container details: Logs, Inspect, Mounts, Exec (built-in VTE terminal), Files (browse + edit), Stats.
- Edit containers: live changes (name, restart policy, limits, networks) and safe recreate (env, ports, volumes, command, image).
- Grafana / Loki-style log explorer with level histogram, drag-to-zoom, ranges, level filters, regex, multi-container.
- Images: Docker Hub search + pull, run, vulnerability scan (Trivy), tag, push, save / load, layer history.
- Volumes: browse and edit files, export / import / clone. Networks: create, connect / disconnect.
- Compose: run any compose file, up, pull & recreate, edit the compose file, down.
- Builds (buildx history) and build cache; resource usage graphs; disk usage clean-up.
- Kubernetes view (any kubeconfig) with YAML editing; local clusters with kind.
- Engines: Docker (system / rootless), Podman, docker contexts, remote tcp://; Colima / OrbStack / Rancher / Docker Desktop sockets on macOS.
- Registry sign-in, light / dark / system theme (works with custom GTK themes), resource saver, optional tray icon.
- Packages: install script for any Linux distro, Flatpak, Arch PKGBUILD, macOS .dmg and Windows .zip (preview).
