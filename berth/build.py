"""Build an image from a folder and a Dockerfile, and find images that have a newer version in their registry.
No GTK here.

Building prefers the docker CLI (BuildKit: RUN --mount, heredocs, cache mounts…) and falls back to the engine API
(the classic builder) when there is no CLI — so it works with any Docker-compatible engine on any system."""
import fnmatch
import json
import os
import tarfile
import tempfile

from .util import host_which


def cli_argv(ctx, dockerfile, tag, args=None, target="", nocache=False, pull=False):
    argv = ["docker", "build", "-t", tag]
    if dockerfile and os.path.abspath(dockerfile) != os.path.join(os.path.abspath(ctx), "Dockerfile"):
        argv += ["-f", dockerfile]
    for k, v in (args or {}).items():
        argv += ["--build-arg", f"{k}={v}"]
    if target:
        argv += ["--target", target]
    if nocache:
        argv.append("--no-cache")
    if pull:
        argv.append("--pull")
    return argv + [ctx]


def has_cli():
    return bool(host_which("docker"))


def _ignore_rules(ctx):
    rules = []
    try:
        with open(os.path.join(ctx, ".dockerignore"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    neg = line.startswith("!")
                    pat = line[1:] if neg else line
                    rules.append((neg, pat.strip("/").removeprefix("./")))
    except OSError:
        pass
    return rules


def _ignored(rel, rules):
    """.dockerignore semantics, simplified: last matching rule wins; a pattern also covers what is inside a folder."""
    out = False
    for neg, pat in rules:
        p = pat.replace("**/", "*/") if "**" in pat else pat
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p + "/*") or ("**" in pat and fnmatch.fnmatch(
                rel, pat.replace("**/", ""))):
            out = not neg
    return out


def context_tar(ctx, dockerfile):
    """The build context as a tar file (honouring .dockerignore). Returns (tar path, Dockerfile path in the tar)."""
    ctx = os.path.abspath(ctx)
    rules = _ignore_rules(ctx)
    fd, path = tempfile.mkstemp(suffix=".tar")
    os.close(fd)
    df_abs = os.path.abspath(dockerfile or os.path.join(ctx, "Dockerfile"))
    inside = os.path.relpath(df_abs, ctx).replace(os.sep, "/")
    with tarfile.open(path, "w") as tf:
        for root, dirs, files in os.walk(ctx):
            rel_root = os.path.relpath(root, ctx).replace(os.sep, "/")
            rel_root = "" if rel_root == "." else rel_root + "/"
            dirs[:] = [d for d in dirs if not _ignored(rel_root + d, rules) or any(n for n, _p in rules)]
            for name in files:
                rel = rel_root + name
                if _ignored(rel, rules) and rel != inside:
                    continue
                tf.add(os.path.join(root, name), arcname=rel, recursive=False)
        if inside.startswith(".."):                  # a Dockerfile outside the folder travels along
            tf.add(df_abs, arcname=".berth.Dockerfile")
            inside = ".berth.Dockerfile"
    return path, inside


def api_build(engine, ctx, dockerfile, tag, args=None, target="", nocache=False, pull=False, on_line=None):
    """Build through the engine API. on_line(text) gets the log as it arrives. Returns the image id."""
    tar, df = context_tar(ctx, dockerfile)
    q = {"t": tag, "dockerfile": df, "rm": "1"}
    if args:
        q["buildargs"] = json.dumps(args)
    if target:
        q["target"] = target
    if nocache:
        q["nocache"] = "1"
    if pull:
        q["pull"] = "1"
    image_id, err = None, None
    try:
        r, c = engine.build(tar, q)
        try:
            buf = b""
            while True:
                chunk = r.read1(65536) if hasattr(r, "read1") else r.read(4096)
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    try:
                        m = json.loads(ln)
                    except ValueError:
                        continue
                    if m.get("error"):
                        err = m["error"]
                    if (m.get("aux") or {}).get("ID"):
                        image_id = m["aux"]["ID"]
                    text = m.get("stream") or m.get("status") or m.get("error") or ""
                    if text and on_line:
                        on_line(text if text.endswith("\n") else text + "\n")
        finally:
            c.close()
    finally:
        os.remove(tar)
    if err:
        raise RuntimeError(err.strip())
    return image_id


# ── newer versions in the registry ──
def check_updates(engine, images, on_progress=None):
    """{repo:tag: True/False/None} — True when the registry's digest for that tag differs from the one this image was
    pulled with. None: couldn't tell (built locally, offline, private without login…)."""
    out = {}
    tags = []
    for im in images:
        digests = im.get("RepoDigests") or []
        for t in im.get("RepoTags") or []:
            if t != "<none>:<none>":
                tags.append((t, digests))
    for i, (tag, digests) in enumerate(tags):
        repo = tag.rsplit(":", 1)[0]
        mine = {d.split("@", 1)[1] for d in digests if d.split("@", 1)[0] == repo}
        if not mine:
            out[tag] = None                                 # built here or loaded: nothing to compare with
            continue
        try:
            d = (engine.distribution(tag) or {}).get("Descriptor", {}).get("digest")
            out[tag] = (d not in mine) if d else None
        except Exception:
            out[tag] = None
        if on_progress:
            on_progress(i + 1, len(tags))
    return out
