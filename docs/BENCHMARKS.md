# Benchmarks

## Method

`tools/bench.py` starts Berth, waits until the engine and container list are ready, opens the Containers page and then
measures the process itself through `/proc/self`:

1. **ready_s** — seconds from process start until the container list is loaded and the window is shown.
2. **cpu_live** — CPU time used over 30 s while live stats are sampled every 2 s (as when you look at the window).
3. **cpu_paused** — CPU time over 30 s with live stats paused (what the resource saver does when the window loses focus).
4. **rss** — resident memory.

```sh
gtk4-broadwayd :9 & GDK_BACKEND=broadway BROADWAY_DISPLAY=:9 python3 tools/bench.py
```

## Results (v1.0.0)

Machine: HP Victus 15 (i5-12450H, 16 GB), EndeavourOS, Linux 7.2, GTK 4.22, libadwaita 1.9, Python 3.14;
engine with 6 running containers (the demo stacks in `tools/demo/`).

| Run | ready | RSS | CPU live (% of one core) | CPU paused |
|---|---|---|---|---|
| 1 | 1.28 s | 108 MB | 0.40 % | 0.07 % |
| 2 | 1.17 s | 108 MB | 0.43 % | 0.13 % |

Notes:
- The demo engine is remote (`tcp://`), so stats came from the Engine's one-shot stats API. With a local engine the
  cgroup reader is cheaper still.
- Measured with the Broadway backend (software rendering). On a real Wayland session GTK's GPU renderer adds some
  driver memory; the *Software rendering* switch in Settings avoids the GPU entirely.
- Installed size: ~1 MB (`~/.local/share/berth`); release tarball ~260 KB (most of it is the icon set). GTK 4 and libadwaita are shared system
  libraries; the Flatpak build uses the shared GNOME runtime.

## Docker Desktop, for comparison

From Docker's documentation and the linked guides (not measured here): requires hardware virtualisation (KVM);
4 GB RAM minimum, 8 GB recommended; the VM is allocated 2 GB of memory by default; the virtual disk image defaults to
about 64 GB. See the README for links.
