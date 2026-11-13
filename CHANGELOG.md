# Changelog

All notable changes to Berth are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added — Send files (share0-style), phones, renames
- **Send files**: share files or folders straight from disk with a Wi-Fi link + QR code (no app, no account, nothing
  uploaded first); video/image preview, Range requests (seek, resume), folder browsing, download-everything .zip, PIN,
  expiry, download limit; **receive mode** (a phone uploads into a folder, never overwriting); one-tap **public link**
  (Cloudflare quick tunnel) for networks that block phones; firewall check + "Allow" (firewalld via polkit, ufw via
  pkexec, until reboot). Built-in QR generator (`berth/qr.py`, no dependency, verified with a decoder).
- **Phones for local addresses**: "Phones on this Wi-Fi too" gives an address its own Wi-Fi port and QR code; only
  that port is published to the network.
- **Rename / change ports everywhere**: ⋮ on container, image, volume and database rows; Rename container (live),
  Change ports (per port "this computer only" / "whole network", conflict check), Rename image (tag + untag),
  Rename volume (stop users → copy → re-point → restart → delete old), Clone volume; database explorer renames
  tables/views/keys/collections and Postgres databases (only with "Allow changes").
- Public links can **expire** (15 min … 1 week, or a date); Berth sweeps them, a systemd user timer is the backstop.
- **Backups** page: back up a database (pg_dump, mysqldump, mongodump, Redis RDB) or a volume now or on a schedule
  (hourly … weekly, keep the newest N), see every backup file, restore one. Runs while Berth is open (also in the
  tray) and catches up missed ones; on Linux a user timer (`berth --run-backups`) also runs them while it is closed.
- **Stacks** page: every Compose project — also ones taken down — with up / down / restart / pull-and-recreate, edit,
  logs and folder. **Compose check** before running a file: syntax, unset variables, port clashes with containers
  or other programs, taken container names, missing build folders, env files, external volumes and networks.
- Database explorer: **edit a cell in place** (double-click), select several rows (Ctrl/Shift+click) to copy or
  delete them, **export a whole table** to CSV / JSON, **import** CSV / JSON (add or replace, all or nothing).
- Containers: **selection mode** — tick containers or whole stacks, then start / stop / restart / remove together.
- **Notifications** when a container stops unexpectedly, runs out of memory or turns unhealthy, and when a backup
  fails (Settings → Notifications).
- **Run dialog with every common option**: network and alias, memory / CPU / GPU, health check, env file,
  entrypoint, user, TTY, init, read-only, capabilities, privileged, labels, ports on this computer only.
- Images: **Build…** from a folder with a Dockerfile (BuildKit through the docker CLI, the engine API without it,
  .dockerignore honoured); **Check for updates** (daily) marks tags with a newer version; **Update** pulls it and
  recreates the containers that use it with the same settings.
- **Ctrl+K / ⌘K** (and a Search button in the sidebar): jump to any container, stack, image, volume, network,
  database or page, or start a common task.
- **Windows: no setup with Docker Desktop.** Berth talks to Docker Desktop (and Podman) through the Windows named
  pipe, like the `docker` command — the unsafe "Expose daemon on tcp://localhost:2375" setting is no longer needed.
  Tested in CI on a Windows runner with Docker (ping, lists, live streams, a container's logs).
- **Remote engines over SSH**: `ssh://user@host` in Settings — the system's OpenSSH and your keys, one tunnel per
  engine, clear messages when keys or the remote socket are the problem.
- **Update notice**: once a day Berth asks GitHub for the latest release and shows a banner (Settings → off; never
  in the Flatpak).
- `berth --self-test`; CI now runs every suite (app paths, click-everything, UI audit, SSH) and installs Berth with
  `scripts/install.sh` on Fedora, Debian, Ubuntu, openSUSE and Arch, then opens every screen; the macOS and Windows
  builds run the self-test.
- **Edit data in the database explorer**: click a row to open it in a side panel with every value in full (type,
  key, NULL, pretty JSON, binary saved to a file); change values, add rows, delete rows — PostgreSQL and
  MySQL/MariaDB by primary key; Redis strings, hashes, lists, sets, sorted sets, streams, RedisJSON, plus new key,
  expiry and delete; MongoDB and the search engines as JSON documents. Empty or delete a table / collection / index.
  Everything needs "Allow changes"; a row changed by someone else meanwhile is reported, never overwritten blindly.
- **Sort and filter**: click a column header to sort, type a condition to filter — over the whole table, on the
  server.
- `docs/GAPS.md`: what Berth still lacks, checked page by page against Docker Desktop, Dockhand and SQL clients.
- **Database tab** on every database container's detail page: the explorer opens right there.
- Container list shows **"public"** and **"address"** badges next to names that are shared or have a local address.
- **Settings → Local addresses proxy**: status, Stop, Remove, and a shortcut to Ports & sharing.
- Every icon-only button has a tooltip (incl. libadwaita's own back/menu buttons). `tools/ui_audit.py` walks every
  page, tab and dialog and fails on an icon button without a tooltip or a field without a hint.
- **Diagnostics**: every error goes to `~/.cache/berth/berth.log`; Settings → Copy diagnostic report / Open the log
  folder.

### Changed
- **Responsive on every screen size**: the window has size classes (sm < 900, md, lg ≥ 1280, xl ≥ 1600, 2xl ≥ 1920,
  3xl ≥ 2560 sp). Spacing and type scale up on xl–3xl; grids pick their columns per class (databases 1→6, send cards
  1→4, resource tiles 2→4, dashboard 1→2); tables share extra width across columns; Settings is wider and two columns
  on 2xl / 3xl. Ranges are in sp, so a larger text size behaves like a smaller screen.
- **Ports & sharing** is a dashboard: summary tiles (incl. ports open to the whole network), addresses and public links,
  and a one-line-per-port table with exposure badges and actions. **Databases** are cards with engine badges,
  status, exposure and Open / Start. The Berth logo is in the sidebar and on the engine-off screen.

### Fixed
- Found by clicking through every engine and page (`tools/walkthrough.py`, `tools/demo/engines.py`):
  long values in the row panel were cut off after the first line; selecting a second row cleared the selection
  (so Copy / Delete never appeared); Vespa documents with their own `id` field couldn't be opened, and documents
  opened as one JSON blob (now field by field, saving only edited fields); Vespa's new-document template wasn't
  valid JSON; MongoDB opened the non-existent default database ("test") with no way to pick the real one;
  Typesense / search engines without curl in their image couldn't be reached when their port isn't reachable
  (always the case on Docker Desktop for macOS / Windows) — a tiny curl helper in the container's network now
  bridges it; dialogs whose content grows after opening (Compose check, Stacks check, Pull results, backups)
  stayed at their first size and hid it; the Compose check said every image "will be downloaded" when the image
  list wasn't loaded yet; Settings offered Stop / Remove for a proxy that doesn't exist; Pull lists official
  images first. `_id` is read-only in MongoDB documents.
- The click-through suites refuse to run against anything but the demo engine (unless told otherwise).
- The volume page header no longer overflows a 1024 px window (Import, Clone and Rename moved into ⋮). CI fails
  on any layout wider than the window.
- **macOS**: keyboard shortcuts use ⌘ (they were Ctrl only). **Windows**: "Pull and recreate" no longer needs `sh`.
  The macOS / Windows builds now bundle every module (some were imported lazily and could be missing).
- **The database grid** looks and behaves like a SQL client: row numbers, columns sized to their content, the last
  one filling the width, horizontal scrolling, and a card only as tall as its rows. Rows are in primary-key order
  (they sorted as text, 1, 10, 11, 2, and moved after an edit). The first table opens by itself.
- **No more empty space or cut-off dialogs**: Ports, Share files, SQLite, Add address, Share publicly and Pull size
  to their content (Pull suggests popular images); container Inspect / Mounts, image, network, Disk usage and
  Settings use two columns on large windows; list tables hug their rows.
- MySQL ignored errors for values over 100 KB (a too-long value looked saved). Volume files: an empty folder said
  "this image has no shell", and unknown owners showed as UNKNOWN (now their uid). An invalid CSS rule on the Send
  files drop zone.
- **Large values in the database explorer** ("field larger than field limit (131072)" on a Postgres table with
  big checkpoint blobs): long text is cut on the server to 4,000 characters with the full length shown, binary
  columns show their size, huge Redis strings/hashes are sampled, pages that are still too large shrink
  automatically (200 → 50 → 10 → 1 rows), and error messages are capped. Covered for Postgres, MySQL/MariaDB,
  Redis, MongoDB, SQLite and the HTTP engines.
- Change ports warns when the port is already used by another program on this computer, not only by containers.
- Database, Send files and Ports & sharing cards have equal heights, aligned rows and buttons at the same place.
- **Memory leak (up to 2.7 GB over a day)**: list pages rebuilt every row every 30 s, even when hidden, and the old
  rows could never be freed. Rows are now updated in place; hidden pages don't rebuild. Detail pages release their
  logs and streams when left.
- The database explorer opened the empty default database in multi-database containers (e.g. 25 databases, "postgres"
  empty): it now opens the one with data.
- Recreating a container (Edit / ports) re-published 127.0.0.1-only ports on all interfaces.
- A locked (PIN) share page no longer shows the file names before the PIN is entered.

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
