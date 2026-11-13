# Changelog

All notable changes to Berth are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.4.0] - 2026-11-13

Berth grows from a Docker desktop into a place to build, watch and fix what you run: safer updates, alerts and
history, several containers at once, registries, templates and Dockerize, a stack's environment, graph and network,
structured logs, metrics and traces, databases anywhere, and local AI. (Issues #8, #9, #13, #14, #15, #22, #23, #24,
#28, #29, #30, #31, #33, #34 — pull requests #38–#46.)

### Added
- **Safe updates** (Settings → Image updates, off by default): before an image update, Berth scans the current and the
  new version with Trivy. If the new one brings critical or high vulnerabilities the current one doesn't have, Berth
  keeps the current version under its name, removes the new download and tells you why; containers are not touched.
- **Scan history**: every scan is remembered (last 10 per image); the scan window compares with the previous one.
- **Security column** on the Images page: the last scan's result for each image (critical, high or clean).
- **Scheduled scans** (Settings → Image updates): scan every image a container uses each day or week, with a
  notification when something critical or high turns up.
- **Alerts** (Settings → Alerts, #15): a desktop notification when a container stays above a CPU limit for 5 minutes,
  passes a share of its memory limit, or when Docker's disk runs low. Checked once a minute, only while an alert is on.
- The daily "newer image version" check now also runs while the Images page is closed, with a notification.
- App stores: brand colours in the metainfo; the menu entry lists one main category (Development).
- **Split view and separate windows** (#22): ⋮ → *Split view with* shows two containers side by side, each with all its
  tabs; ⋮ → *Open in a new window* gives a container its own window. Each one streams only while it is on screen.
- **Container details on the design system** (#8): a page header with image · status · stack and the actions on the
  right, the tab bar as the toolbar row, Exec and Files in cards inside the page margins, the Files warning as an
  inline note, bars that follow the density setting.
- **Short status text** (#9): `Up 2 d`, `Exited 137 · 3 h`, `Unhealthy · 4 min`; the full status and what the exit
  code means (137 = killed / out of memory, 143 = stopped, 139 = crashed …) in the tooltip; failed exits in amber.
- **Registries page** (#34, under Build): browse Docker Hub namespaces and any Registry v2 registry (self-hosted, GHCR,
  Quay, GitLab…): tags with digest, size, platforms and date, whether your copy matches, pull, copy a tag to another
  registry, delete tags where the registry allows it. Uses docker's own credentials.
- **Layer explorer** (#13): "Where does the size go?" bar and legend in the image details, each step's share of the
  size, the biggest layers marked, metadata-only steps dimmed, BuildKit history shown as the Dockerfile step.
- **Templates** (#31, Stacks → Templates…): 13 ready-made stacks (PostgreSQL + Adminer, MariaDB + Adminer, Redis,
  MongoDB + Mongo Express, SeaweedFS S3, RabbitMQ, Redpanda + Console, OpenSearch + Dashboards, Vespa, Meilisearch,
  Ollama + Open WebUI, Nginx, Traefik) as plain compose files in `berth/templates/`. The Compose dialog now shows
  what a file creates (services, images, ports, volumes) next to the check, and offers to open the app afterwards.
- **Dockerize a folder** (#31, Stacks): detects Node.js (npm / yarn / pnpm), Python (pip / uv, Flask / FastAPI /
  Django), Go, Java (Maven / Gradle) and static sites, writes a Dockerfile, .dockerignore and compose.yaml to read and
  edit before saving, then builds and runs it if you want.
- **Environment and secrets per stack** (#23, Stacks → ⋮): every variable of every service with where it comes from
  (compose file, `${…}` from .env, env_file, image default, set when created), secrets masked until shown, values
  that differ between services, and editing the .env / env_file files with checks — saving recreates only the
  services whose settings changed.
- **Dependency graph** (#14, Stacks → ⋮): services with state and health, depends_on arrows with their condition,
  each service's networks, volumes under the services that mount them. Click a service to open it.
- **Network map** (#33, Networks → Map): networks with their containers and IPs, published ports, live traffic, and
  a reachability check — "can web reach api on port 8000?" — run from inside the container's network.
- The demo stack (tools/demo) now has two networks, depends_on with a health condition, environment variables with
  secrets, an env_file and an api that listens on port 8000.
- **Structured logs** (#29): JSON and key=value lines become fields — the **Fields** button lists each field's top
  values, a click filters on it (`route=/cart`), several filters combine; **saved filters** per stack or container;
  **pause / resume** the live view; **export** what is shown as text, JSON lines or CSV (with the fields as columns);
  Python / Java / Node stack traces are grouped into one entry.
- **App metrics** (#30): a **Metrics** tab on every container finds its Prometheus endpoint (labels, exposed and
  listening ports, /metrics or /actuator/prometheus), scrapes it every 5 s while the tab is open, and charts it —
  counters as a rate per second. Starter charts for node, Postgres, Redis, nginx, Node.js, Python, Go and Java
  exporters; any other series can be added.
- **Databases outside Docker** (Databases → Connect…): a connection string for PostgreSQL, MySQL / MariaDB, Redis /
  Valkey or MongoDB, on this computer or in the cloud, opens the same explorer. Berth runs the database's own client in
  a short-lived container (removed when you close it); recent connections are remembered without their password.
- **Query tabs and saved queries**: several queries side by side, each with its own result; save a query under a
  name per database; Run uses only the selected text when something is selected.
- **Schema editing** (PostgreSQL, MySQL / MariaDB — Structure view): new table, add / rename / drop a column, add an
  index. Each dialog shows the exact SQL first and runs it only with "Allow changes" on.
- **Local AI** (#24, sidebar → Local AI): open models through an Ollama container — pick one by download size and the
  memory it needs (with a warning when that's more than is free), download with live progress, chat, copy the
  OpenAI-compatible address. CPU by default; the NVIDIA GPU only when you switch it on. Ollama stops after a while
  without use (Settings → Local AI and assistants).
- **MCP server** (#24): `berth --mcp` lets AI assistants (Claude, Cursor, VS Code…) list containers, read logs and
  details and list images. Starting, stopping and restarting needs a switch in Settings; "Copy config" gives the
  snippet for the assistant.
- Berth removes the client containers of outside databases when it quits.
- Registries: Docker Hub tags come from Hub's own API (one request per 100 tags) — reading each tag's manifest would
  count against Docker Hub's pull rate limit.
- Local AI: the idle stop no longer stops Ollama at once when Berth has no record of its last use.
- README: new and refreshed screenshots (split view, dependency graph, network map, registries, local AI, images,
  stacks, settings, container details) and the 1.4 features.
- **Loaders on buttons**: while an action runs — start, stop, restart, pause, delete (rows, stacks, the container
  screen, bulk actions), the engine button, Run, Pull, Build, Apply, Share, Connect, a query, Local AI — the button
  shows a small ring spinner, keeps its width and can't be pressed twice.
- **Metrics history** (#28, Settings → History, off by default): a sample a minute of every container's CPU and
  memory while Berth runs, kept 1 / 7 / 30 days (at most 100 MB). Resource usage gets 6 h, 24 h and 7 d ranges with
  starts, restarts, crashes, out-of-memory and pulls marked on the charts, "Compare with the day before", and a click
  on a chart opens the logs of that moment.
- **Traces** (sidebar → Observe): a small OpenTelemetry receiver (OTLP over HTTP, protobuf or JSON) on 127.0.0.1 and
  Docker's bridge; recent traces per service and a span waterfall. Opt-in, in memory only.
- **Files per image layer**: a Files button on each layer that adds data lists what that step added or deleted,
  biggest first.
- **Stop a running query** (PostgreSQL, MySQL / MariaDB): a Stop button next to Run cancels it on the server.
- **Foreign keys** in the schema editor.
- READMEs in Japanese and Chinese list the 1.4 features.
- The self-test prints each step and a Python stack trace on a crash (for the occasional Windows CI failure).

### Changed
- Flatpak builds on the GNOME 51 runtime (was 48). A Flathub-ready manifest that builds a pinned release tag is in
  `packaging/flatpak/flathub/`. The AUR PKGBUILD has the real checksum and a `.SRCINFO`.

## [1.3.0] - 2026-11-13

The biggest release so far: Berth now reaches into your databases, shares what you run, backs it up, and works the
same on Linux, macOS and Windows. (Issues #25, #26, #27 — pull request #36.)

### Added — Connect: databases, addresses, links, files
- **Database explorer** for PostgreSQL (incl. PostGIS, TimescaleDB, pgvector), MySQL / MariaDB, Redis / Valkey,
  MongoDB, Vespa, OpenSearch / Elasticsearch, Typesense, Meilisearch and SQLite files. No drivers and no extra
  ports: Berth uses the client inside the image (or a tiny helper in the container's network).
  - Tree of schemas / tables / keys / collections / indices with sizes; paged data in a SQL-client grid (row
    numbers, columns sized to their content, horizontal scroll); **sort** by a column and **filter** rows, on the
    server; structure and indexes; a query console with history; export to CSV / JSON / Markdown.
  - Click a row: every value **in full** in a side panel (JSON formatted, binary saved to a file).
  - **Edit data**: change values, add and delete rows (by primary key), edit a cell in place (double-click),
    select several rows to copy or delete; Redis keys of every type (plus new key, expiry); MongoDB and search
    documents field by field. Read-only until you switch on **Allow changes**; a row changed by someone else is
    reported, never overwritten.
  - **Whole tables**: export every row to CSV / JSON, import CSV / JSON (add or replace, all or nothing).
  - **Rename** tables, keys, collections and Postgres databases; empty or delete a table.
  - Large values never break a page (long text cut on the server with its length, binary as its size, pages that
    shrink by themselves).
  - A **Database** tab on every database container.
- **Ports & sharing**: friendly local addresses like `https://shop.localhost` (one small proxy, 127.0.0.1 only;
  phones on the same Wi-Fi optional), **public links** through a Cloudflare tunnel (no account, can expire, stop
  when Berth quits unless kept), and every published port with a warning on the ones open to the whole network.
- **Send files**: share files or folders with a Wi-Fi link and QR code (preview, resume, PIN, expiry, download
  limit), receive files from a phone, optional public link.

### Added — manage more with fewer clicks
- **Backups** page: back up a database (pg_dump, mysqldump, mongodump, Redis RDB) or a volume now or on a schedule,
  keep the newest N, restore with one click. Missed backups run at start; on Linux a user timer
  (`berth --run-backups`) also runs them while Berth is closed.
- **Stacks** page: every Compose project (also ones taken down) with up / down / restart / pull-and-recreate, edit,
  logs, folder. A **Compose check** before running a file: syntax, unset variables, port clashes with containers or
  other programs, taken names, missing build folders, env files, volumes and networks.
- **Containers**: selection mode for bulk start / stop / restart / remove; rename and change ports of any
  container (warns about ports used by other programs); "public" / "address" badges.
- **Notifications** when a container crashes, runs out of memory or turns unhealthy, and when a backup fails.
- **Run dialog** with every common option (network and alias, memory / CPU / GPU, health check, env file, user,
  entrypoint, TTY, init, read-only, capabilities, privileged, labels, ports on this computer only).
- **Images**: build from a folder with a Dockerfile (BuildKit through the CLI, the engine API without it);
  "update available" for pulled tags and a one-click update that recreates the containers using them; rename
  images and volumes; clone volumes.
- **Search everything** with Ctrl+K (⌘K on macOS) or the Search button: containers, stacks, images, volumes,
  networks, databases, pages and tasks.
- **Engines**: remote engines over SSH (`ssh://user@host`, your SSH keys); on **Windows**, Docker Desktop and
  Podman through their named pipe — no TCP setting needed.
- An **update notice** when a new Berth is out (daily, can be turned off; never in the Flatpak).
- **Diagnostics**: errors go to a log; Settings → Copy diagnostic report.

### Changed
- Responsive on every screen size (size classes up to 2560 sp): grids pick their columns, detail pages and
  Settings use two columns on large windows, dialogs size to their content, list tables hug their rows.
- Every icon-only button has a tooltip; Settings stays pinned at the bottom of the sidebar.
- macOS keyboard shortcuts use ⌘.

### Fixed
- A memory leak that could reach 2.7 GB over a day (list rows rebuilt every 30 s).
- Recreating a container no longer re-publishes 127.0.0.1-only ports on every interface.
- Windows: crashes on the Disk usage page and in the explorer (Linux-only calls); macOS / Windows builds now
  include every module.
- Charts couldn't draw on Debian, Ubuntu and openSUSE: the installer now adds the Python cairo bridge.
- MongoDB opened the empty default database instead of the one with data; empty volume folders said "no shell";
  a MySQL value too long for its column looked saved.
- Many layout fixes found by clicking through every page and engine (values cut off in the row panel, dialogs
  hiding results that arrived later, headers wider than small windows).

### Tests
- Integration against real Docker for every engine (edit, export / import, backup / restore round trips), the app
  driven like a user, click-everything, a UI audit with screenshots of every screen, SSH engines, Send files.
- CI installs Berth with `scripts/install.sh` on Fedora, Debian, Ubuntu, openSUSE and Arch and opens every screen;
  the macOS and Windows builds run `berth --self-test`; a Windows job talks to Docker through the named pipe.

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
