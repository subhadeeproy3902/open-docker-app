#!/bin/sh
# Every app-level suite, off-screen, against the demo engine — what CI runs on each change:
#   berth --self-test   (startup without an engine)
#   tests/drive.py      (user paths: databases, editing, stacks, backups, build, Ctrl+K, …)
#   tests/clickall.py   (clicks every button on every page)
#   tools/ui_audit.py   (every page, tab and dialog: tooltips, labels, screenshots)
#   tests/fileshare.py  (Send files server)
#   tests/ssh.py        (remote engines over SSH)
# Each UI suite gets its own headless Wayland compositor (weston; GTK 4.24's broadway server crashes), and a layout wider than the window ("exceeds … width") fails
# the run. Inside an Arch Linux container (current GTK 4 / libadwaita) with host networking, so it reaches the demo
# engine on 127.0.0.1:23750. Start that first:  sh tools/demo/up.sh
set -u
cd "$(dirname "$0")/.." || exit 1
OUT="${OUT:-$PWD/ci-out}"
mkdir -p "$OUT"
docker run --rm --network host -v "$PWD:/src:ro" -v "$OUT:/out" -v /var/run/docker.sock:/var/run/docker.sock \
  -e BERTH_NO_UPDATE_CHECK=1 -e BERTH_TEST_SKIP_K8S=1 archlinux:latest sh -c '
    pacman -Syu --noconfirm --needed python python-gobject python-cairo gtk4 libadwaita vte4 dbus openssh docker \
      docker-compose docker-buildx weston ttf-dejavu >/dev/null || exit 1
    cp -r /src /tmp/berth && cd /tmp/berth || exit 1
    n=10
    rc=0
    ui() {               # ui NAME CONFIG-ENGINE TIMEOUT COMMAND… — a fresh display per suite
      name=$1; engine=$2; t=$3; shift 3
      n=$((n + 1))
      export XDG_RUNTIME_DIR=/tmp/xdg && mkdir -p -m 0700 "$XDG_RUNTIME_DIR"
      weston --backend=headless --socket="wl-$n" --width=1920 --height=1200 --idle-time=0 >"/out/weston-$name.log" 2>&1 &
      bpid=$!
      sleep 3
      mkdir -p "/tmp/cfg-$name/berth"
      # saver off: a headless compositor never focuses the window, and the resource saver pauses stats then
      echo "{\"onboarded\": true, \"saver\": false, \"engine\": \"$engine\"}" > "/tmp/cfg-$name/berth/settings.json"
      GDK_BACKEND=wayland WAYLAND_DISPLAY="wl-$n" GSK_RENDERER=cairo XDG_CONFIG_HOME="/tmp/cfg-$name" \
        timeout "$t" dbus-run-session -- "$@" > "/out/$name.log" 2>&1 || { echo "   $name exited with an error"; rc=1; }
      kill "$bpid" 2>/dev/null
      if grep -q "exceeds .* width" "/out/$name.log"; then
        echo "   layout wider than the window:"; grep "exceeds .* width" "/out/$name.log" | sort -u | head -3; rc=1
      fi
    }
    echo "== self-test (no engine)"
    ui selftest /tmp/none.sock 300 python3 -m berth --self-test
    grep "self-test:" /out/selftest.log || rc=1
    echo "== drive"
    ui drive tcp://127.0.0.1:23750 1800 python3 tests/drive.py
    grep -E "^FAIL|passed" /out/drive.log || rc=1
    grep -q "^[0-9]*/[0-9]* passed$" /out/drive.log || rc=1
    echo "== clickall"
    ui clickall tcp://127.0.0.1:23750 1200 python3 tests/clickall.py
    grep -E "^=== (total|errors)" /out/clickall.log || rc=1
    grep -q "^=== errors: 0" /out/clickall.log || rc=1
    echo "== ui audit"
    ui audit tcp://127.0.0.1:23750 900 python3 tools/ui_audit.py /out/audit
    tail -1 /out/audit.log
    grep -q "^0 problem" /out/audit.log || rc=1
    echo "== send files"
    python3 tests/fileshare.py > /out/fileshare.log 2>&1 || rc=1
    tail -1 /out/fileshare.log
    echo "== ssh"
    DOCKER_HOST=unix:///var/run/docker.sock python3 tests/ssh.py > /out/ssh.log 2>&1 || rc=1
    tail -1 /out/ssh.log
    exit $rc'
