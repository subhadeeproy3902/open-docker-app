# Contributing to Berth

Thanks for helping! Bug reports, docs fixes, translations, packaging and code are all welcome.

## Development setup

You need Python ≥ 3.10, GTK ≥ 4.12, libadwaita ≥ 1.7 and PyGObject (see the install section of the README for your
distro's package names), plus a Docker engine you can reach.

```sh
git clone https://github.com/subhadeeproy3902/open-docker-app && cd open-docker-app
python3 -m berth            # run straight from the source tree
make check                  # byte-compile every module
```

Optional: `vte4` (built-in terminal), `kubectl` + `kind` (Kubernetes view), `docker compose` (Compose features).

## Project layout

| Path | What lives there |
|---|---|
| `berth/engine.py` | Docker Engine API client (stdlib only, unix / tcp sockets) |
| `berth/store.py` | State + event stream, tells pages what changed |
| `berth/stats.py` | Live metrics from cgroup v2 (falls back to the Engine's stats API) |
| `berth/containers.py`, `pages.py`, `logs.py`, `files.py`, `edit.py`, `compose.py`, `k8s.py`, `scan.py` | Pages and features |
| `berth/app.py`, `settings_page.py`, `theme.py`, `tray.py`, `util.py` | Window, settings, theming, platform layer |
| `tests/` | End-to-end UI tests that drive the real app |
| `tools/` | Screenshot generator, benchmark, demo engine |
| `packaging/` | Flatpak, Arch, PyInstaller (macOS / Windows) |

Design notes: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Tests

The UI tests drive the real app off-screen with GTK's Broadway backend, so nothing appears on your screen. They
create their own throwaway containers, volumes, networks and compose projects and remove them afterwards.

```sh
gtk4-broadwayd :9 &
export GDK_BACKEND=broadway BROADWAY_DISPLAY=:9 XDG_CONFIG_HOME=/tmp/berth-test
dbus-run-session -- python3 tests/drive.py       # 59 end-to-end behaviours (Kubernetes part needs kubectl + kind)
dbus-run-session -- python3 tests/clickall.py    # clicks every control on every page, reports errors / dead buttons
```

Screenshots for the README come from an isolated demo engine:

```sh
sh tools/demo/up.sh                               # docker:dind with two demo stacks on tcp://127.0.0.1:23750
python3 tools/shots.py docs/screenshots-new dark 1360 860   # with the engine set to tcp://127.0.0.1:23750 in Settings
sh tools/demo/down.sh
```

## Ground rules

- **Never block the UI thread.** Every Engine call goes through `util.bg()`; streams use their own threads.
- **Lists update from the event stream**, not from polling. Update rows in place.
- **Stay light.** No new dependencies beyond the standard library and PyGObject without discussing it first.
  No timers or animations while idle.
- **Destructive actions ask first**; deleting data (volumes) requires typing its name.
- Add a check to `tests/drive.py` for every new user-visible behaviour, and make sure `tests/clickall.py` stays clean.

## Pull requests

1. Fork, create a branch (`feat/…`, `fix/…`, `docs/…`).
2. Keep the change focused; describe *what* and *why* in the PR (the template helps).
3. Run `make check` and the relevant tests; attach before/after screenshots for UI changes (light and dark).
4. By contributing you agree your work is released under the MIT license.

## Translations

The README exists in English, 简体中文 and 日本語 — improvements and new languages are welcome. Keep technical terms
(Docker, compose, socket, …) in English and keep the structure identical to `README.md`.
