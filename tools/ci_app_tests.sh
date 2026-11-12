#!/bin/sh
# Every app-level suite, off-screen, against the demo engine — what CI runs on each change:
#   tests/drive.py      (user paths: databases, editing, stacks, backups, build, Ctrl+K, …)
#   tests/clickall.py   (clicks every button on every page)
#   tools/ui_audit.py   (every page, tab and dialog: tooltips, labels, screenshots)
#   tests/fileshare.py  (Send files server)
#   tests/ssh.py        (remote engines over SSH)
#   berth --self-test   (startup without an engine)
# It runs inside an Arch Linux container (current GTK 4 / libadwaita) with host networking, so it reaches the demo
# engine on 127.0.0.1:23750. Start that first:  sh tools/demo/up.sh
set -eu
cd "$(dirname "$0")/.."
OUT="${OUT:-$PWD/ci-out}"
mkdir -p "$OUT"
docker run --rm --network host -v "$PWD:/src:ro" -v "$OUT:/out" -v /var/run/docker.sock:/var/run/docker.sock \
  -e BERTH_NO_UPDATE_CHECK=1 -e BERTH_TEST_SKIP_K8S=1 archlinux:latest sh -c '
    set -e
    pacman -Syu --noconfirm --needed python python-gobject gtk4 libadwaita vte4 dbus openssh docker docker-compose \
      ttf-dejavu >/dev/null
    cp -r /src /tmp/berth && cd /tmp/berth
    gtk4-broadwayd :5 >/dev/null 2>&1 &
    sleep 2
    export GDK_BACKEND=broadway BROADWAY_DISPLAY=:5
    cfg() { mkdir -p "/tmp/$1/berth"; echo "{\"onboarded\": true, \"engine\": \"tcp://127.0.0.1:23750\"}" \
            > "/tmp/$1/berth/settings.json"; echo "/tmp/$1"; }
    rc=0
    echo "== self-test (no engine)"
    mkdir -p /tmp/none/berth && echo "{\"onboarded\": true, \"engine\": \"/tmp/none.sock\"}" > /tmp/none/berth/settings.json
    XDG_CONFIG_HOME=/tmp/none dbus-run-session -- python3 -m berth --self-test 2>&1 | grep "self-test" || rc=1
    echo "== drive";     XDG_CONFIG_HOME=$(cfg d) timeout 1500 dbus-run-session -- python3 tests/drive.py \
                           > /out/drive.log 2>&1 || rc=1;    grep -E "^FAIL|passed" /out/drive.log || rc=1
    echo "== clickall";  XDG_CONFIG_HOME=$(cfg c) timeout 1200 dbus-run-session -- python3 tests/clickall.py \
                           > /out/clickall.log 2>&1 || rc=1; grep -E "^=== (total|errors)" /out/clickall.log
    grep -q "^=== errors: 0" /out/clickall.log || rc=1
    echo "== ui audit";  XDG_CONFIG_HOME=$(cfg a) timeout 900 dbus-run-session -- python3 tools/ui_audit.py /out/audit \
                           > /out/audit.log 2>&1 || rc=1;    tail -1 /out/audit.log
    echo "== send files"; python3 tests/fileshare.py > /out/fileshare.log 2>&1 || rc=1; tail -1 /out/fileshare.log
    echo "== ssh";       DOCKER_HOST=unix:///var/run/docker.sock python3 tests/ssh.py > /out/ssh.log 2>&1 || rc=1
    tail -1 /out/ssh.log
    exit $rc'
