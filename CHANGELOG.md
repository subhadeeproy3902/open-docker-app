# Changelog

All notable changes to Berth are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project uses [Semantic Versioning](https://semver.org/).

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
