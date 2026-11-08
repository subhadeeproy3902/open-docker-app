"""Light / dark that really switches.

Desktops that theme GTK 4 through ~/.config/gtk-4.0/gtk.css (matugen, pywal, gradience…) override
libadwaita's colours at USER priority, so Adw's FORCE_LIGHT alone changes nothing visible. When the
user picks Light or Dark in Berth we load a full palette one step above USER priority; "Follow system"
removes it and the desktop's own colours apply again."""
import os
import re

from gi.repository import Adw, Gdk, Gtk

LIGHT = {
    "accent_bg_color": "#3584e4", "accent_fg_color": "#ffffff", "accent_color": "#0461be",
    "destructive_bg_color": "#e01b24", "destructive_fg_color": "#ffffff", "destructive_color": "#c30000",
    "success_bg_color": "#2ec27e", "success_fg_color": "#ffffff", "success_color": "#007c3d",
    "warning_bg_color": "#e5a50a", "warning_fg_color": "rgba(0,0,0,0.8)", "warning_color": "#905400",
    "error_bg_color": "#e01b24", "error_fg_color": "#ffffff", "error_color": "#c30000",
    "window_bg_color": "#fafafb", "window_fg_color": "rgba(0,0,6,0.8)",
    "view_bg_color": "#ffffff", "view_fg_color": "rgba(0,0,6,0.8)",
    "headerbar_bg_color": "#ffffff", "headerbar_fg_color": "rgba(0,0,6,0.8)",
    "headerbar_border_color": "rgba(0,0,6,0.8)", "headerbar_backdrop_color": "#fafafb",
    "headerbar_shade_color": "rgba(0,0,6,0.12)", "headerbar_darker_shade_color": "rgba(0,0,6,0.12)",
    "sidebar_bg_color": "#ebebed", "sidebar_fg_color": "rgba(0,0,6,0.8)", "sidebar_backdrop_color": "#f2f2f4",
    "sidebar_shade_color": "rgba(0,0,6,0.07)", "sidebar_border_color": "rgba(0,0,6,0.07)",
    "secondary_sidebar_bg_color": "#f3f3f5", "secondary_sidebar_fg_color": "rgba(0,0,6,0.8)",
    "secondary_sidebar_backdrop_color": "#f6f6fa", "secondary_sidebar_shade_color": "rgba(0,0,6,0.07)",
    "secondary_sidebar_border_color": "rgba(0,0,6,0.07)",
    "card_bg_color": "#ffffff", "card_fg_color": "rgba(0,0,6,0.8)", "card_shade_color": "rgba(0,0,6,0.07)",
    "dialog_bg_color": "#fafafb", "dialog_fg_color": "rgba(0,0,6,0.8)",
    "popover_bg_color": "#ffffff", "popover_fg_color": "rgba(0,0,6,0.8)", "popover_shade_color": "rgba(0,0,6,0.07)",
    "thumbnail_bg_color": "#ffffff", "thumbnail_fg_color": "rgba(0,0,6,0.8)",
    "shade_color": "rgba(0,0,6,0.07)", "scrollbar_outline_color": "#ffffff",
}
DARK = {
    "accent_bg_color": "#3584e4", "accent_fg_color": "#ffffff", "accent_color": "#81d0ff",
    "destructive_bg_color": "#c01c28", "destructive_fg_color": "#ffffff", "destructive_color": "#ff938c",
    "success_bg_color": "#26a269", "success_fg_color": "#ffffff", "success_color": "#78e9ab",
    "warning_bg_color": "#cd9309", "warning_fg_color": "rgba(0,0,0,0.8)", "warning_color": "#ffc252",
    "error_bg_color": "#c01c28", "error_fg_color": "#ffffff", "error_color": "#ff938c",
    "window_bg_color": "#222226", "window_fg_color": "#ffffff",
    "view_bg_color": "#1d1d20", "view_fg_color": "#ffffff",
    "headerbar_bg_color": "#2e2e32", "headerbar_fg_color": "#ffffff",
    "headerbar_border_color": "#ffffff", "headerbar_backdrop_color": "#222226",
    "headerbar_shade_color": "rgba(0,0,6,0.36)", "headerbar_darker_shade_color": "rgba(0,0,12,0.9)",
    "sidebar_bg_color": "#2e2e32", "sidebar_fg_color": "#ffffff", "sidebar_backdrop_color": "#28282c",
    "sidebar_shade_color": "rgba(0,0,6,0.25)", "sidebar_border_color": "rgba(0,0,6,0.36)",
    "secondary_sidebar_bg_color": "#28282c", "secondary_sidebar_fg_color": "#ffffff",
    "secondary_sidebar_backdrop_color": "#252529", "secondary_sidebar_shade_color": "rgba(0,0,6,0.25)",
    "secondary_sidebar_border_color": "rgba(0,0,6,0.36)",
    "card_bg_color": "rgba(255,255,255,0.08)", "card_fg_color": "#ffffff", "card_shade_color": "rgba(0,0,6,0.36)",
    "dialog_bg_color": "#36363a", "dialog_fg_color": "#ffffff",
    "popover_bg_color": "#36363a", "popover_fg_color": "#ffffff", "popover_shade_color": "rgba(0,0,6,0.25)",
    "thumbnail_bg_color": "#39393d", "thumbnail_fg_color": "#ffffff",
    "shade_color": "rgba(0,0,6,0.25)", "scrollbar_outline_color": "rgba(0,0,12,0.95)",
}
SCHEMES = {"system": Adw.ColorScheme.DEFAULT, "light": Adw.ColorScheme.FORCE_LIGHT, "dark": Adw.ColorScheme.FORCE_DARK}
_provider = None


def _user_palette_dark():
    """True/False if the user's gtk-4.0 css defines a dark/light window background, None if it doesn't."""
    base = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "gtk-4.0")
    text = ""
    for name in ("gtk.css", "colors.css"):
        try:
            with open(os.path.join(base, name)) as f:
                text += f.read()
        except OSError:
            pass
    m = re.search(r"@define-color\s+window_bg_color\s+#([0-9a-fA-F]{6})", text)
    if not m:
        return None
    r, g, b = (int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4))
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < 0.5


def apply(name):
    global _provider
    Adw.StyleManager.get_default().set_color_scheme(SCHEMES.get(name, Adw.ColorScheme.DEFAULT))
    display = Gdk.Display.get_default()
    if _provider is not None:
        Gtk.StyleContext.remove_provider_for_display(display, _provider)
        _provider = None
    user_dark = _user_palette_dark()
    if name == "system" or user_dark is None:
        return                              # nothing overrides libadwaita: Adw's own switch works
    if (name == "dark") == user_dark:
        return                              # the desktop palette already matches; keep its colours
    pal = LIGHT if name == "light" else DARK
    css = "".join(f"@define-color {k} {v};\n" for k, v in pal.items())
    _provider = Gtk.CssProvider()
    _provider.load_from_string(css)
    Gtk.StyleContext.add_provider_for_display(display, _provider, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
