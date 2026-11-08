# Changelog

All notable changes to Berth are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project uses [Semantic Versioning](https://semver.org/).

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
