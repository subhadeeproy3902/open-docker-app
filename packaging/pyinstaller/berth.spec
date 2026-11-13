# PyInstaller spec for the macOS (.app) and Windows builds:  pyinstaller packaging/pyinstaller/berth.spec
import os, sys
from PyInstaller.utils.hooks import collect_submodules
ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
datas = [(os.path.join(ROOT, "berth", "style.css"), "berth"), (os.path.join(ROOT, "data", "icons"), os.path.join("data", "icons"))]
a = Analysis([os.path.join(SPECPATH, "launcher.py")], pathex=[ROOT], datas=datas,
             hiddenimports=["berth.app"] + collect_submodules("berth"),     # also the ones imported lazily
             hooksconfig={"gi": {"module-versions": {"Gtk": "4.0", "Gdk": "4.0", "Gsk": "4.0", "Adw": "1"},
                                 "icons": ["Adwaita", "hicolor"], "themes": ["Adwaita"], "languages": ["en_US"]}},
             excludes=["tkinter"])
pyz = PYZ(a.pure)
icon = os.path.join(ROOT, "data", "icons", "berth.icns" if sys.platform == "darwin" else "berth.ico")
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Berth", console=False,
          icon=icon if os.path.exists(icon) else None)
coll = COLLECT(exe, a.binaries, a.datas, name="Berth")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="Berth.app", icon=icon if os.path.exists(icon) else None,
                 bundle_identifier="io.github.subhadeeproy3902.Berth",
                 info_plist={"CFBundleShortVersionString": "1.2.1", "NSHighResolutionCapable": True,
                             "LSMinimumSystemVersion": "12.0"})
