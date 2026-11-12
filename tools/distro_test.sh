#!/bin/sh
# Berth on other Linux distributions: in a fresh container per distro, run the real installer (scripts/install.sh,
# which installs GTK 4 / libadwaita / PyGObject with that distro's package manager), check the installed launcher,
# then open every page, detail page, dialog and the database explorer off-screen (tools/ui_audit.py over the
# broadway backend) against the demo engine. Nothing is installed on this computer.
#
#   sh tools/demo/up.sh && sh tools/distro_test.sh [fedora debian ubuntu opensuse arch]
# Exit code 1 if any distro failed. Logs and screenshots: ${OUT:-/tmp/berth-distros}/<distro>/
set -u
cd "$(dirname "$0")/.." || exit 1
SRC="$(pwd)"
OUT="${OUT:-/tmp/berth-distros}"
mkdir -p "$OUT"
DISTROS="${*:-fedora debian ubuntu opensuse arch}"

image() {
  case "$1" in
    fedora) echo "fedora:latest" ;;
    debian) echo "debian:trixie" ;;
    ubuntu) echo "ubuntu:25.04" ;;
    opensuse) echo "opensuse/tumbleweed:latest" ;;
    arch) echo "archlinux:latest" ;;
  esac
}

# the broadway display server and a session bus, per distro (not something Berth itself needs)
extras() {
  case "$1" in
    fedora) echo "dnf install -y gtk4-devel-tools dbus-daemon findutils >/dev/null 2>&1 || dnf install -y gtk4 dbus-daemon >/dev/null" ;;
    debian|ubuntu) echo "apt-get install -y libgtk-4-bin dbus >/dev/null" ;;
    opensuse) echo "zypper --non-interactive install gtk4-tools findutils dejavu-fonts >/dev/null; for p in dbus-1-daemon dbus-1-tools dbus-1; do zypper --non-interactive install \$p >/dev/null 2>&1 || true; done" ;;
    arch) echo "pacman -S --needed --noconfirm dbus >/dev/null" ;;
  esac
}

fails=""
for d in $DISTROS; do
  img="$(image "$d")"
  [ -n "$img" ] || { echo "unknown distro $d"; continue; }
  mkdir -p "$OUT/$d"
  echo "== $d ($img)"
  docker run --rm --network host -v "$SRC:/src:ro" -v "$OUT/$d:/out" -e BERTH_NO_WELCOME=1 \
    -e BERTH_NO_UPDATE_CHECK=1 -e AUDIT_W=1650 -e AUDIT_H=950 "$img" sh -c "
      set -e
      cp -r /src /tmp/berth && cd /tmp/berth
      if [ -f /etc/debian_version ]; then export DEBIAN_FRONTEND=noninteractive; fi
      if command -v pacman >/dev/null; then pacman -Sy --noconfirm >/dev/null; fi
      sh scripts/install.sh > /out/install.log 2>&1 || { echo 'installer failed'; tail -20 /out/install.log; exit 1; }
      $(extras "$d")
      ~/.local/bin/berth --version
      python3 -c 'import gi; gi.require_version(\"Gtk\",\"4.0\"); gi.require_version(\"Adw\",\"1\"); from gi.repository import Gtk, Adw; print(\"GTK\", Gtk.get_major_version(), Gtk.get_minor_version(), \"· libadwaita\", Adw.get_major_version(), Adw.get_minor_version())'
      python3 -m py_compile berth/*.py
      B=\$(command -v gtk4-broadwayd || find / -name gtk4-broadwayd -type f 2>/dev/null | head -1)
      [ -n \"\$B\" ] || { echo 'no broadway server on this distro'; exit 1; }
      \"\$B\" :7 >/dev/null 2>&1 &
      sleep 2
      mkdir -p /tmp/cfg/berth && echo '{\"onboarded\": true, \"engine\": \"tcp://127.0.0.1:23750\"}' > /tmp/cfg/berth/settings.json
      BUS=''; command -v dbus-run-session >/dev/null && BUS='dbus-run-session --'
      GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 XDG_CONFIG_HOME=/tmp/cfg timeout 500 \$BUS \
        python3 tools/ui_audit.py /out/shots > /out/audit.log 2>&1 || true
      grep -E 'problem|Traceback|Error' /out/audit.log | grep -vE 'portal|Gsk-WARNING|bus' | tail -8
      grep -q '^0 problem' /out/audit.log
      if grep -qE 'Traceback \(most recent|TypeError|AttributeError|NameError|ImportError' /out/audit.log; then
        echo 'Python errors while drawing — see audit.log'; grep -E 'TypeError|AttributeError|NameError|ImportError' /out/audit.log | sort | uniq -c | head -5; exit 1
      fi
    " > "$OUT/$d/run.log" 2>&1
  rc=$?
  tail -6 "$OUT/$d/run.log"
  [ $rc -eq 0 ] && echo "   ✓ $d" || { echo "   ✗ $d (see $OUT/$d/)"; fails="$fails $d"; }
done
[ -z "$fails" ] && echo "All distros passed" || { echo "Failed:$fails"; exit 1; }
