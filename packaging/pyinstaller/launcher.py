"""Entry point for the self-contained macOS / Windows builds (PyInstaller)."""
import sys

if "--run-backups" in sys.argv:                 # scheduled backups without a window
    from berth.backups import cli
    sys.exit(cli())

from berth.app import main  # noqa: E402

sys.exit(main())
