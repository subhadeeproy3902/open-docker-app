"""`berth --mcp`: a small Model Context Protocol server (JSON-RPC over stdin / stdout) so AI assistants can see the
user's containers (#24). Reading — list containers, logs, details, images, stacks — is always allowed. Starting,
stopping and restarting a container only works when the user switched it on in Settings → Local AI and assistants, and the
assistant's own app asks before each call as well. Talks to the engine chosen in Berth's settings (Local AI and assistants)."""
import json
import os
import sys

PROTOCOL = "2025-06-18"


def _settings():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(base, "berth", "settings.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _engine():
    from .engine import Engine
    return Engine(_settings().get("engine") or None)


def _name(c):
    return ((c.get("Names") or ["?"])[0]).lstrip("/")


def _find(engine, name):
    for c in engine.containers():
        if name in (_name(c), c["Id"], c["Id"][:12]):
            return c
    raise ValueError(f"No container named {name!r}")


TOOLS = [
    {"name": "list_containers", "title": "List containers",
     "description": "Every container with its state, image, stack (Compose project) and published ports.",
     "inputSchema": {"type": "object", "properties": {"running_only": {"type": "boolean"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "container_logs", "title": "Read a container's logs",
     "description": "The last lines of a container's logs (stdout and stderr).",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"},
                                                      "lines": {"type": "integer", "minimum": 1, "maximum": 2000}},
                     "required": ["name"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "inspect_container", "title": "Container details",
     "description": "State, exit code, health, restart count, image, ports, mounts and networks of a container. "
                    "Environment values are not included (they often hold secrets).",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "list_images", "title": "List images", "description": "Local images with tags and sizes.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}},
    {"name": "container_action", "title": "Start, stop or restart a container",
     "description": "Start, stop or restart one container. Only works when the user allowed it in Berth → Settings → "
                    "Local AI and assistants.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"},
                                                      "action": {"type": "string", "enum": ["start", "stop", "restart"]}},
                     "required": ["name", "action"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}},
]


def call(tool, args):
    e = _engine()
    if tool == "list_containers":
        out = []
        for c in e.containers():
            if args.get("running_only") and c.get("State") != "running":
                continue
            out.append({"name": _name(c), "state": c.get("State"), "status": c.get("Status"), "image": c.get("Image"),
                        "stack": (c.get("Labels") or {}).get("com.docker.compose.project", ""),
                        "ports": sorted({f"{p.get('PublicPort')}->{p.get('PrivatePort')}" for p in c.get("Ports") or []
                                         if p.get("PublicPort")})})
        return json.dumps(out, indent=1)
    if tool == "container_logs":
        c = _find(e, args["name"])
        out, err = e.logs_text(c["Id"], tail=str(min(int(args.get("lines") or 200), 2000)))
        return (out + (("\n--- stderr ---\n" + err) if err.strip() else ""))[-200000:] or "(no log lines)"
    if tool == "inspect_container":
        d = e.inspect(_find(e, args["name"])["Id"])
        st = d.get("State") or {}
        return json.dumps({"name": d.get("Name", "").lstrip("/"), "image": (d.get("Config") or {}).get("Image"),
                           "state": st.get("Status"), "exit_code": st.get("ExitCode"), "oom_killed": st.get("OOMKilled"),
                           "health": (st.get("Health") or {}).get("Status"), "started": st.get("StartedAt"),
                           "restart_count": d.get("RestartCount"), "command": (d.get("Config") or {}).get("Cmd"),
                           "ports": (d.get("NetworkSettings") or {}).get("Ports"),
                           "networks": list(((d.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
                           "mounts": [{"type": m.get("Type"), "source": m.get("Name") or m.get("Source"),
                                       "target": m.get("Destination")} for m in d.get("Mounts") or []]}, indent=1)
    if tool == "list_images":
        return json.dumps([{"tags": im.get("RepoTags") or [], "size": im.get("Size"), "id": im["Id"][7:19]}
                           for im in e.images()], indent=1)
    if tool == "container_action":
        if not _settings().get("mcp_actions"):
            raise PermissionError("Not allowed: switch on “Let assistants start, stop and restart containers” in "
                                  "Berth → Settings → Local AI and assistants.")
        if args.get("action") not in ("start", "stop", "restart"):
            raise ValueError("action must be start, stop or restart")
        c = _find(e, args["name"])
        e.action(c["Id"], args["action"])
        return f"{args['action']} {_name(c)}: done"
    raise ValueError(f"Unknown tool {tool}")


def handle(msg):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        import re
        try:
            with open(os.path.join(os.path.dirname(__file__), "util.py")) as f:     # no GTK in this process
                VERSION = re.search(r'^VERSION = "([^"]+)"', f.read(), re.M).group(1)
        except (OSError, AttributeError):
            VERSION = "unknown"
        res = {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
               "serverInfo": {"name": "berth", "title": "Berth", "version": VERSION},
               "instructions": "Docker containers on this computer, through Berth. Reading is always allowed; "
                               "container_action needs the user's permission in Berth's settings (Local AI and assistants)."}
    elif method == "tools/list":
        res = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            text = call(params.get("name"), params.get("arguments") or {})
            res = {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as ex:  # noqa: BLE001 — tool errors are results the assistant can read, not protocol errors
            res = {"content": [{"type": "text", "text": str(ex)}], "isError": True}
    elif method == "ping":
        res = {}
    elif mid is None:              # notifications (initialized, cancelled…) need no answer
        return None
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": res}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            out = handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
    return 0
