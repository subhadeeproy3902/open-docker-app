"""Entry point for the self-contained macOS / Windows builds (PyInstaller)."""
import sys

from berth.app import main

sys.exit(main())
