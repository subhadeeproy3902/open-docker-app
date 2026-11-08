#!/bin/sh
# Berth installer for any Linux distribution.
#
#   curl -fsSL https://raw.githubusercontent.com/subhadeeproy3902/open-docker-app/main/scripts/install.sh | sh
#   # or, from a downloaded release / git checkout:   sh scripts/install.sh
#
# What it does:
#   1. installs GTK 4, libadwaita and PyGObject with your package manager (asks for sudo once)
#   2. if your libadwaita is older than 1.7 (e.g. Ubuntu 24.04), installs the Flatpak build instead
#   3. installs Berth for your user into ~/.local (no root needed for this part)
# Options:  --flatpak  (always use Flatpak)   --no-deps  (skip step 1)   --uninstall
set -eu

REPO="subhadeeproy3902/open-docker-app"
APPID="io.github.subhadeeproy3902.Berth"
MODE="native"; DEPS=1
for a in "$@"; do
  case "$a" in
    --flatpak) MODE="flatpak" ;;
    --no-deps) DEPS=0 ;;
    --uninstall) MODE="uninstall" ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
  esac
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
SUDO=""; [ "$(id -u)" -ne 0 ] && have sudo && SUDO="sudo"

uninstall() {
  if have flatpak && flatpak info "$APPID" >/dev/null 2>&1; then flatpak uninstall -y "$APPID" || true; fi
  rm -rf "$HOME/.local/share/berth" "$HOME/.local/bin/berth" \
         "$HOME/.local/share/applications/$APPID.desktop" "$HOME/.local/share/metainfo/$APPID.metainfo.xml" \
         "$HOME/.local/share/icons/hicolor/scalable/apps/$APPID.svg" \
         "$HOME/.local/share/icons/hicolor/symbolic/apps/$APPID-symbolic.svg"
  say "Berth removed (your settings stay in ~/.config/berth)."
}

install_deps() {
  [ -r /etc/os-release ] || die "Can't tell which distribution this is (no /etc/os-release). Try: sh install.sh --flatpak"
  . /etc/os-release
  ids="${ID:-} ${ID_LIKE:-}"
  say "Installing GTK 4, libadwaita and PyGObject for ${PRETTY_NAME:-your system}…"
  case " $ids " in
    *" arch "*|*" manjaro "*|*" endeavouros "*)
      $SUDO pacman -S --needed --noconfirm python python-gobject gtk4 libadwaita make curl
      $SUDO pacman -S --needed --noconfirm vte4 || warn "vte4 not installed (built-in terminal disabled)" ;;
    *" debian "*|*" ubuntu "*)
      $SUDO apt-get update -qq
      $SUDO apt-get install -y python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 make curl ca-certificates
      $SUDO apt-get install -y gir1.2-vte-3.91 || warn "VTE not installed (built-in terminal disabled)" ;;
    *" fedora "*|*" rhel "*|*" centos "*)
      $SUDO dnf install -y python3 python3-gobject gtk4 libadwaita make curl
      $SUDO dnf install -y vte291-gtk4 || warn "VTE not installed (built-in terminal disabled)" ;;
    *" opensuse "*|*" suse "*)
      $SUDO zypper --non-interactive install python3 python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 make curl
      $SUDO zypper --non-interactive install typelib-1_0-Vte-3_91 || warn "VTE not installed (built-in terminal disabled)" ;;
    *" alpine "*)
      $SUDO apk add python3 py3-gobject3 gtk4.0 libadwaita make curl
      $SUDO apk add vte3-gtk4 || warn "VTE not installed (built-in terminal disabled)" ;;
    *" void "*)
      $SUDO xbps-install -Sy python3 python3-gobject gtk4 libadwaita make curl
      $SUDO xbps-install -y vte3-gtk4 || warn "VTE not installed (built-in terminal disabled)" ;;
    *" solus "*)
      $SUDO eopkg install -y python3 python-gobject libgtk-4 libadwaita make ;;
    *" gentoo "*)
      $SUDO emerge --noreplace dev-python/pygobject gui-libs/gtk gui-libs/libadwaita ;;
    *" nixos "*)
      warn "On NixOS, Berth is easiest as a Flatpak."; MODE="flatpak"; return 0 ;;
    *) warn "Unknown distribution '$ID' — skipping dependencies (install GTK 4, libadwaita ≥ 1.7 and PyGObject yourself)." ;;
  esac
}

adw_ok() {
  python3 - <<'PY' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
ok = (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 7) and (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 12)
raise SystemExit(0 if ok else 1)
PY
}

install_flatpak() {
  if ! have flatpak; then
    say "Installing Flatpak itself…"
    . /etc/os-release 2>/dev/null || true
    case " ${ID:-} ${ID_LIKE:-} " in
      *" debian "*|*" ubuntu "*) $SUDO apt-get update -qq && $SUDO apt-get install -y flatpak ;;
      *" fedora "*) $SUDO dnf install -y flatpak ;;
      *" arch "*) $SUDO pacman -S --needed --noconfirm flatpak ;;
      *" opensuse "*|*" suse "*) $SUDO zypper --non-interactive install flatpak ;;
      *) die "Please install Flatpak (https://flatpak.org/setup/) and run this again." ;;
    esac
  fi
  flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
  tmp="$(mktemp -d)"
  say "Downloading the Berth Flatpak…"
  fetch "https://github.com/$REPO/releases/latest/download/berth.flatpak" "$tmp/berth.flatpak"
  say "Installing (the first time Flatpak also fetches the GNOME runtime, shared with other apps)…"
  flatpak install --user -y --noninteractive "$tmp/berth.flatpak"
  rm -rf "$tmp"
  say "Done. Open Berth from your app menu, or run:  flatpak run $APPID"
}

fetch() {
  if have curl; then curl -fL --progress-bar -o "$2" "$1"; elif have wget; then wget -q --show-progress -O "$2" "$1"; else die "Need curl or wget."; fi
}

install_native() {
  src=""
  here="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)"
  if [ -n "$here" ] && [ -f "$here/../berth/app.py" ]; then
    src="$(cd "$here/.." && pwd)"
  else
    tmp="$(mktemp -d)"
    say "Downloading the latest Berth release…"
    fetch "https://github.com/$REPO/releases/latest/download/berth-linux.tar.gz" "$tmp/berth.tar.gz"
    tar -xzf "$tmp/berth.tar.gz" -C "$tmp"
    src="$(find "$tmp" -maxdepth 2 -name Makefile -exec dirname {} \; | head -1)"
  fi
  [ -n "$src" ] || die "Couldn't find Berth's files."
  say "Installing Berth into ~/.local…"
  make -C "$src" install-user >/dev/null
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "Add ~/.local/bin to your PATH to run 'berth' from a terminal." ;; esac
  say "Done. Open Berth from your app menu, or run:  berth"
}

case "$MODE" in
  uninstall) uninstall; exit 0 ;;
  flatpak) install_flatpak; exit 0 ;;
esac
[ "$DEPS" -eq 1 ] && install_deps
if [ "$MODE" = "flatpak" ]; then install_flatpak; exit 0; fi
if adw_ok; then
  install_native
else
  warn "This system's GTK/libadwaita is older than Berth needs (GTK ≥ 4.12, libadwaita ≥ 1.7)."
  say  "Using the Flatpak build instead — it brings its own up-to-date GTK."
  install_flatpak
fi
if ! have docker && ! have podman; then
  warn "No Docker found. Install Docker Engine (https://docs.docker.com/engine/install/) or Podman, then open Berth."
fi
