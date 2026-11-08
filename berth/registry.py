"""Registry sign-in through the docker CLI, so credentials go wherever docker keeps them
(~/.docker/config.json or the configured credential helper) and `docker push/pull` use them too."""
import json
import os
import subprocess

from .util import host

HUB = "https://index.docker.io/v1/"


def config_path():
    return os.path.join(os.environ.get("DOCKER_CONFIG") or os.path.expanduser("~/.docker"), "config.json")


def logins():
    """Registries docker has credentials for."""
    try:
        with open(config_path()) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return []
    regs = set((cfg.get("auths") or {}).keys()) | set((cfg.get("credHelpers") or {}).keys())
    return sorted(regs)


def pretty(reg):
    return "Docker Hub" if reg in (HUB, "docker.io", "index.docker.io", "https://index.docker.io/v1/") else reg


def login(registry, user, password):
    argv = ["docker", "login"] + ([registry] if registry and registry not in ("docker.io", HUB) else []) + \
           ["-u", user, "--password-stdin"]
    r = subprocess.run(host(argv), input=password, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "login failed")
    return r.stdout.strip()


def logout(registry):
    argv = ["docker", "logout"] + ([registry] if registry not in (HUB, "docker.io") else [])
    r = subprocess.run(host(argv), capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip() or "logout failed")
