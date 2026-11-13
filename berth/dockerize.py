"""Dockerize this folder (#31), like `docker init` without an account: detect what the project is, write a Dockerfile,
a .dockerignore and a compose.yaml, let the user read and edit them, save, and optionally build and run."""
import json
import os
import re

from gi.repository import Adw, GLib, Gtk

from . import compose
from .util import confirm, esc, follow_content

IGNORE_COMMON = [".git", ".gitignore", "Dockerfile", ".dockerignore", "compose.yaml", "*.log", ".env", ".env.*",
                 ".DS_Store", ".vscode", ".idea"]


def _read(folder, name):
    try:
        with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _has(folder, *names):
    return any(os.path.exists(os.path.join(folder, n)) for n in names)


def detect(folder):
    """{kind, label, port, ...} for the project in this folder, or None."""
    if _has(folder, "package.json"):
        try:
            pkg = json.loads(_read(folder, "package.json") or "{}")
        except ValueError:
            pkg = {}
        pm = "pnpm" if _has(folder, "pnpm-lock.yaml") else "yarn" if _has(folder, "yarn.lock") else "npm"
        scripts = pkg.get("scripts") or {}
        main = pkg.get("main") or next((f for f in ("server.js", "index.js", "app.js", "main.js") if _has(folder, f)),
                                       "index.js")
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        port = 3000
        m = re.search(r"\b(?:PORT|port)\D{0,20}(\d{4,5})", _read(folder, main))
        if m:
            port = int(m.group(1))
        return {"kind": "node", "label": f"Node.js ({pm})", "pm": pm, "start": "start" in scripts,
                "build": "build" in scripts, "main": main, "port": port, "next": "next" in deps}
    if _has(folder, "pyproject.toml", "requirements.txt", "Pipfile", "manage.py") or \
            any(f.endswith(".py") for f in os.listdir(folder)):
        text = (_read(folder, "requirements.txt") + _read(folder, "pyproject.toml")).lower()
        fw = "django" if ("django" in text or _has(folder, "manage.py")) else "fastapi" if "fastapi" in text else \
            "flask" if "flask" in text else ""
        entry = next((f for f in ("main.py", "app.py", "server.py", "run.py") if _has(folder, f)), "main.py")
        port = 8000 if fw in ("django", "fastapi") else 5000 if fw == "flask" else 8000
        m = re.search(r"port\s*=\s*(\d{4,5})", _read(folder, entry))
        if m:
            port = int(m.group(1))
        tool = "uv" if _has(folder, "uv.lock") else "pip"
        return {"kind": "python", "label": "Python" + (f" ({fw.capitalize()})" if fw else ""), "fw": fw,
                "entry": entry, "port": port, "tool": tool, "req": _has(folder, "requirements.txt"),
                "pyproject": _has(folder, "pyproject.toml")}
    if _has(folder, "go.mod"):
        return {"kind": "go", "label": "Go", "port": 8080}
    if _has(folder, "pom.xml"):
        return {"kind": "maven", "label": "Java (Maven)", "port": 8080}
    if _has(folder, "build.gradle", "build.gradle.kts"):
        return {"kind": "gradle", "label": "Java (Gradle)", "port": 8080}
    if _has(folder, "index.html"):
        return {"kind": "static", "label": "Static website", "port": 80}
    return None


def _node(d):
    pm = d["pm"]
    lock = {"npm": "package*.json", "yarn": "package.json yarn.lock", "pnpm": "package.json pnpm-lock.yaml"}[pm]
    install = {"npm": "npm ci", "yarn": "corepack enable && yarn install --frozen-lockfile",
               "pnpm": "corepack enable && pnpm install --frozen-lockfile"}[pm]
    cmd = f'["{pm}", "start"]' if d["start"] else f'["node", "{d["main"]}"]'
    lines = ["# syntax=docker/dockerfile:1", "FROM node:22-alpine", "WORKDIR /app", f"COPY {lock} ./",
             f"RUN {install}" if pm != "npm" else "RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi",
             "COPY . ."]
    if d["build"]:
        lines.append(f"RUN {pm} run build")
    lines += ["ENV NODE_ENV=production", f"ENV PORT={d['port']}", f"EXPOSE {d['port']}", "USER node", f"CMD {cmd}"]
    return "\n".join(lines) + "\n", ["node_modules", "npm-debug.log*", "dist", "build", ".next", "coverage"]


def _python(d):
    lines = ["# syntax=docker/dockerfile:1", "FROM python:3.13-slim", "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1",
             "WORKDIR /app"]
    if d["tool"] == "uv":
        lines += ["COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv", "COPY pyproject.toml uv.lock ./",
                  "RUN uv sync --frozen --no-dev --no-install-project", "ENV PATH=/app/.venv/bin:$PATH"]
    elif d["req"]:
        lines += ["COPY requirements.txt ./", "RUN pip install --no-cache-dir -r requirements.txt"]
    lines.append("COPY . .")
    if d["tool"] != "uv" and not d["req"] and d["pyproject"]:
        lines.append("RUN pip install --no-cache-dir .")
    lines += ["RUN useradd --create-home app", "USER app", f"EXPOSE {d['port']}"]
    mod = d["entry"].removesuffix(".py")
    cmd = {"django": f'["python", "manage.py", "runserver", "0.0.0.0:{d["port"]}"]',
           "fastapi": f'["uvicorn", "{mod}:app", "--host", "0.0.0.0", "--port", "{d["port"]}"]',
           "flask": f'["flask", "--app", "{mod}", "run", "--host", "0.0.0.0", "--port", "{d["port"]}"]'}.get(
        d["fw"], f'["python", "{d["entry"]}"]')
    lines.append(f"CMD {cmd}")
    return "\n".join(lines) + "\n", ["__pycache__", "*.pyc", ".venv", "venv", ".pytest_cache", ".mypy_cache",
                                      "dist", "build", "*.egg-info"]


def _go(d):
    return ("# syntax=docker/dockerfile:1\nFROM golang:1.24-alpine AS build\nWORKDIR /src\nCOPY go.* ./\n"
            "RUN go mod download\nCOPY . .\nRUN CGO_ENABLED=0 go build -o /out/app .\n\n"
            "FROM gcr.io/distroless/static-debian12\nCOPY --from=build /out/app /app\n"
            f"EXPOSE {d['port']}\nUSER nonroot\nENTRYPOINT [\"/app\"]\n"), ["bin", "vendor"]


def _maven(d):
    return ("# syntax=docker/dockerfile:1\nFROM maven:3.9-eclipse-temurin-21 AS build\nWORKDIR /src\nCOPY pom.xml .\n"
            "RUN mvn -q dependency:go-offline\nCOPY src ./src\nRUN mvn -q package -DskipTests\n\n"
            "FROM eclipse-temurin:21-jre\nWORKDIR /app\nCOPY --from=build /src/target/*.jar app.jar\n"
            f"EXPOSE {d['port']}\nCMD [\"java\", \"-jar\", \"app.jar\"]\n"), ["target"]


def _gradle(d):
    return ("# syntax=docker/dockerfile:1\nFROM gradle:8-jdk21 AS build\nWORKDIR /src\nCOPY . .\n"
            "RUN gradle --no-daemon bootJar || gradle --no-daemon jar\n\n"
            "FROM eclipse-temurin:21-jre\nWORKDIR /app\nCOPY --from=build /src/build/libs/*.jar app.jar\n"
            f"EXPOSE {d['port']}\nCMD [\"java\", \"-jar\", \"app.jar\"]\n"), ["build", ".gradle"]


def _static(d):
    return "FROM nginx:alpine\nCOPY . /usr/share/nginx/html\nEXPOSE 80\n", []


def generate(folder, d, port=None):
    """{"Dockerfile": text, ".dockerignore": text, "compose.yaml": text}."""
    d = dict(d, port=port or d["port"])
    df, ignore = {"node": _node, "python": _python, "go": _go, "maven": _maven, "gradle": _gradle,
                  "static": _static}[d["kind"]](d)
    name = re.sub(r"[^a-z0-9-]+", "-", os.path.basename(os.path.abspath(folder)).lower()).strip("-") or "app"
    host = 8080 if d["port"] == 80 else d["port"]
    comp = (f"services:\n  {name}:\n    build: .\n    ports: [\"127.0.0.1:{host}:{d['port']}\"]\n"
            "    restart: unless-stopped\n")
    return {"Dockerfile": df, ".dockerignore": "\n".join(IGNORE_COMMON + ignore) + "\n", "compose.yaml": comp}


class DockerizeDialog(Adw.Dialog):
    def __init__(self, win, store):
        super().__init__(title="Dockerize a folder", content_width=760, content_height=640)
        self.win, self.store, self.folder, self.info, self.views = win, store, None, None, {}
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        hb.pack_start(cancel)
        self.save_btn = Gtk.Button(label="Save files", sensitive=False)
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", lambda *_: self._save())
        hb.pack_end(self.save_btn)
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=18, margin_end=18,
                      margin_bottom=18)
        g = Adw.PreferencesGroup()
        self.folder_row = Adw.ActionRow(title="Project folder", subtitle="Choose the folder with your code",
                                        activatable=True)
        self.folder_row.add_suffix(Gtk.Image(icon_name="folder-open-symbolic"))
        self.folder_row.connect("activated", lambda *_: self._pick())
        self.kind_row = Adw.ActionRow(title="Detected", subtitle="—")
        self.port = Adw.SpinRow.new_with_range(1, 65535, 1)
        self.port.set_title("Port the app listens on")
        self.port.connect("notify::value", lambda *_: self._regen())
        for w in (self.folder_row, self.kind_row, self.port):
            g.add(w)
        box.append(g)
        self.tabs = Adw.ViewStack(vexpand=True)
        for name in ("Dockerfile", ".dockerignore", "compose.yaml"):
            v = Gtk.TextView(monospace=True, top_margin=10, bottom_margin=10, left_margin=12, right_margin=12)
            v.add_css_class("logview")
            self.views[name] = v
            sw = Gtk.ScrolledWindow(child=v, vexpand=True, min_content_height=260)
            self.tabs.add_titled(sw, name, name)
        sw_bar = Adw.InlineViewSwitcher(stack=self.tabs)
        self.files_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False)
        self.files_box.append(Gtk.Label(label="Read and edit before saving:", xalign=0, css_classes=["heading"]))
        self.files_box.append(sw_bar)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["card"], overflow=Gtk.Overflow.HIDDEN)
        card.append(self.tabs)
        self.files_box.append(card)
        box.append(self.files_box)
        tv.set_content(Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                          propagate_natural_height=True, max_content_height=760))
        self.set_child(tv)
        follow_content(self, 760)

    def _pick(self):
        d = Gtk.FileDialog(title="Project folder", accept_label="Choose")

        def done(dlg, res):
            try:
                f = dlg.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.use_folder(f.get_path())
        d.select_folder(self.win, None, done)

    def use_folder(self, path):
        self.folder = path
        self.folder_row.set_subtitle(esc(path))
        self.info = detect(path)
        if not self.info:
            self.kind_row.set_subtitle("Nothing recognised — expected package.json, pyproject.toml / requirements.txt, "
                                       "go.mod, pom.xml, build.gradle or index.html")
            self.files_box.set_visible(False)
            self.save_btn.set_sensitive(False)
            return
        self.kind_row.set_subtitle(esc(self.info["label"]))
        self.port.set_value(self.info["port"])
        self._regen()
        self.files_box.set_visible(True)
        self.save_btn.set_sensitive(True)

    def _regen(self):
        if not (self.folder and self.info):
            return
        files = generate(self.folder, self.info, int(self.port.get_value()))
        for name, text in files.items():
            self.views[name].get_buffer().set_text(text)

    def _text(self, name):
        b = self.views[name].get_buffer()
        return b.get_text(b.get_start_iter(), b.get_end_iter(), False)

    def _save(self):
        existing = [n for n in self.views if os.path.exists(os.path.join(self.folder, n))]
        if existing:
            confirm(self.win, "Replace existing files?", f"{esc(', '.join(existing))} already exist in this folder "
                    "and will be overwritten.", "Replace", self._write)
        else:
            self._write()

    def _write(self):
        try:
            for name in self.views:
                with open(os.path.join(self.folder, name), "w") as f:
                    f.write(self._text(name))
        except OSError as e:
            self.win.toast(f"Couldn't save: {e}")
            return
        self.close()
        d = Adw.AlertDialog(heading="Saved", body="Dockerfile, .dockerignore and compose.yaml are in the folder. "
                                                  "Build the image and start it now?")
        d.add_response("later", "Later")
        d.add_response("run", "Build and run")
        d.set_response_appearance("run", Adw.ResponseAppearance.SUGGESTED)

        def go(_d, r):
            if r != "run":
                return
            o = compose.ComposeOpen(self.win, self.store)
            o.use_file(os.path.join(self.folder, "compose.yaml"),
                       re.sub(r"[^a-z0-9-]+", "-", os.path.basename(self.folder).lower()).strip("-") or "app")
            o.build.set_active(True)
            o.present(self.win)
        d.connect("response", go)
        d.present(self.win)
