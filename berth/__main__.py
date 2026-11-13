import json
import os
import sys


def _prefs():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(base, "berth", "settings.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


if "--mcp" in sys.argv:                                  # MCP server for AI assistants (stdin / stdout), no window
    from berth.mcp import main as mcp_main
    sys.exit(mcp_main())

if "--run-backups" in sys.argv:                          # scheduled backups, no window (systemd timer, cron, …)
    from berth.backups import cli
    sys.exit(cli())

if _prefs().get("software"):
    os.environ.setdefault("GSK_RENDERER", "cairo")      # no GPU at all

from berth.app import main  # noqa: E402

sys.exit(main())
