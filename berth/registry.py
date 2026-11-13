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


# ── browsing (#34): the Registry v2 API, plus Docker Hub's own API for listing and deleting ──
import base64  # noqa: E402
import re  # noqa: E402
import ssl  # noqa: E402
import urllib.error  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402

ACCEPT = ", ".join(["application/vnd.oci.image.index.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.docker.distribution.manifest.v2+json",
                    "application/vnd.oci.image.manifest.v1+json"])
UA = {"User-Agent": "Berth (+https://github.com/subhadeeproy3902/open-docker-app)"}
INDEXES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")


def is_hub(reg):
    return reg in ("docker.io", "index.docker.io", "registry-1.docker.io", HUB, "Docker Hub")


def credentials(reg):
    """(user, secret) docker has for this registry — from config.json or its credential helper — or None."""
    try:
        with open(config_path()) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    keys = [HUB, "docker.io", "index.docker.io"] if is_hub(reg) else [reg, f"https://{reg}", f"http://{reg}"]
    for k in keys:
        a = (cfg.get("auths") or {}).get(k) or {}
        if a.get("auth"):
            user, _, pw = base64.b64decode(a["auth"]).decode(errors="replace").partition(":")
            return user, pw
    helper = next((v for k, v in (cfg.get("credHelpers") or {}).items() if k in keys), None) or cfg.get("credsStore")
    if helper:
        for k in keys:
            try:
                r = subprocess.run(host([f"docker-credential-{helper}", "get"]), input=k, capture_output=True,
                                   text=True, timeout=10)
                if r.returncode == 0:
                    d = json.loads(r.stdout)
                    return d.get("Username", ""), d.get("Secret", "")
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
    return None


class RegistryError(Exception):
    pass


class Client:
    """One registry. Handles anonymous, basic and bearer-token auth (Docker Hub, GHCR, Quay, GitLab, registry:2…)."""

    def __init__(self, registry, creds=None):
        self.reg = "docker.io" if is_hub(registry) else registry.rstrip("/").removeprefix("https://").removeprefix("http://")
        self.hub = self.reg == "docker.io"
        self.scheme = "http" if registry.startswith("http://") else "https"
        self.creds = creds if creds is not None else credentials(self.reg)
        self.tokens, self.basic = {}, False

    @property
    def base(self):
        return "https://registry-1.docker.io" if self.hub else f"{self.scheme}://{self.reg}"

    def _open(self, req):
        try:
            return urllib.request.urlopen(req, timeout=20)
        except urllib.error.URLError as e:
            # self-hosted registries often speak plain HTTP (docker allows that for localhost / insecure-registries)
            if self.scheme == "https" and not self.hub and isinstance(getattr(e, "reason", None), (ssl.SSLError, OSError)) \
                    and not isinstance(e, urllib.error.HTTPError):
                self.scheme = "http"
                req.full_url = req.full_url.replace("https://", "http://", 1)
                return urllib.request.urlopen(req, timeout=20)
            raise

    def _token(self, header, scope):
        p = dict(re.findall(r'(\w+)="([^"]*)"', header))
        q = {"service": p.get("service", "")}
        if scope or p.get("scope"):
            q["scope"] = scope or p["scope"]
        req = urllib.request.Request(p["realm"] + "?" + urllib.parse.urlencode(q), headers=dict(UA))
        if self.creds:
            req.add_header("Authorization", "Basic " + base64.b64encode(":".join(self.creds).encode()).decode())
        with self._open(req) as r:
            d = json.load(r)
        return d.get("token") or d.get("access_token")

    def req(self, method, path, scope=None, accept=None, raw=False):
        """(headers, body) — body is parsed JSON unless raw."""
        for attempt in range(2):
            req = urllib.request.Request(self.base + path, method=method, headers=dict(UA))
            if accept:
                req.add_header("Accept", accept)
            if self.tokens.get(scope):
                req.add_header("Authorization", "Bearer " + self.tokens[scope])
            elif self.basic and self.creds:
                req.add_header("Authorization", "Basic " + base64.b64encode(":".join(self.creds).encode()).decode())
            try:
                with self._open(req) as r:
                    body = r.read()
                    return r.headers, (body if raw else (json.loads(body) if body else {}))
            except urllib.error.HTTPError as e:
                hdr = e.headers.get("WWW-Authenticate", "")
                if e.code == 401 and attempt == 0 and hdr.lower().startswith("bearer"):
                    self.tokens[scope] = self._token(hdr, scope)
                    continue
                if e.code == 401 and attempt == 0 and hdr.lower().startswith("basic") and self.creds:
                    self.basic = True
                    continue
                msg = e.read().decode(errors="replace")[:300]
                try:
                    msg = json.loads(msg)["errors"][0]["message"]
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
                raise RegistryError(f"{e.code}: {msg or e.reason}") from None
        raise RegistryError("not authorised")

    # listing
    def repositories(self, namespace=""):
        if self.hub:
            ns = namespace or (self.creds[0] if self.creds else "")
            if not ns:
                raise RegistryError("Type a Docker Hub namespace (your user or organisation name)")
            out, url = [], f"https://hub.docker.com/v2/namespaces/{urllib.parse.quote(ns)}/repositories?page_size=100"
            while url and len(out) < 1000:
                r = urllib.request.Request(url, headers=dict(UA))
                tok = self._hub_jwt()
                if tok:
                    r.add_header("Authorization", "Bearer " + tok)
                try:
                    with urllib.request.urlopen(r, timeout=20) as f:
                        d = json.load(f)
                except urllib.error.HTTPError as e:
                    if out and e.code in (401, 403):          # Hub serves only the first page without signing in
                        break
                    raise RegistryError(f"Docker Hub: {e.code} {e.reason}" +
                                        (" — no such namespace" if e.code == 404 else "")) from None
                out += [f"{ns}/{x['name']}" for x in d.get("results") or []]
                url = d.get("next")
            return out
        try:
            _h, d = self.req("GET", "/v2/_catalog?n=1000", scope="registry:catalog:*")
        except RegistryError as e:
            raise RegistryError(f"This registry doesn't list its repositories ({e}). Type a repository name instead.")
        repos = d.get("repositories") or []
        return [r for r in repos if r.startswith(namespace)] if namespace else repos

    def tags(self, repo):
        _h, d = self.req("GET", f"/v2/{repo}/tags/list?n=1000", scope=f"repository:{repo}:pull")
        return sorted(d.get("tags") or [])

    def info(self, repo, tag):
        """{digest, platforms, size, created} of one tag."""
        scope = f"repository:{repo}:pull"
        h, m = self.req("GET", f"/v2/{repo}/manifests/{tag}", scope=scope, accept=ACCEPT)
        digest = h.get("Docker-Content-Digest", "")
        out = {"digest": digest, "platforms": [], "size": 0, "created": ""}
        if (m.get("mediaType") or h.get("Content-Type", "")) in INDEXES or "manifests" in m:
            ms = [x for x in m.get("manifests") or [] if (x.get("platform") or {}).get("os") != "unknown"]
            out["platforms"] = [f"{x['platform']['os']}/{x['platform']['architecture']}"
                                + (f"/{x['platform']['variant']}" if x['platform'].get("variant") else "")
                                for x in ms if x.get("platform")]
            if not ms:
                return out
            _h, m = self.req("GET", f"/v2/{repo}/manifests/{ms[0]['digest']}", scope=scope, accept=ACCEPT)
        cfg = m.get("config") or {}
        out["config"] = cfg.get("digest", "")         # = the local image ID once pulled (single-platform images)
        out["size"] = sum(x.get("size", 0) for x in m.get("layers") or []) + cfg.get("size", 0)
        if cfg.get("digest"):
            try:
                _h, c = self.req("GET", f"/v2/{repo}/blobs/{cfg['digest']}", scope=scope)
                out["created"] = (c.get("created") or "")[:19].replace("T", " ")
                if not out["platforms"] and c.get("os"):
                    out["platforms"] = [f"{c['os']}/{c.get('architecture', '')}"]
            except (RegistryError, ValueError):
                pass
        return out

    # changing
    def delete(self, repo, tag, digest):
        if self.hub:
            tok = self._hub_jwt()
            if not tok:
                raise RegistryError("Sign in to Docker Hub first (Settings → Registries)")
            ns, _, name = repo.partition("/")
            r = urllib.request.Request(f"https://hub.docker.com/v2/repositories/{ns}/{name}/tags/{tag}/", method="DELETE",
                                       headers=dict(UA))
            r.add_header("Authorization", "Bearer " + tok)
            try:
                urllib.request.urlopen(r, timeout=20).close()
            except urllib.error.HTTPError as e:
                raise RegistryError(f"{e.code}: {e.reason}") from None
            return
        try:
            self.req("DELETE", f"/v2/{repo}/manifests/{digest}", scope=f"repository:{repo}:delete,pull", raw=True)
        except RegistryError as e:
            if str(e).startswith("405"):
                raise RegistryError("This registry doesn't allow deleting. On registry:2 set "
                                    "REGISTRY_STORAGE_DELETE_ENABLED=true.") from None
            raise

    def _hub_jwt(self):
        if not (self.hub and self.creds):
            return None
        if "hub" not in self.tokens:
            r = urllib.request.Request("https://hub.docker.com/v2/users/login/", method="POST",
                                       data=json.dumps({"username": self.creds[0], "password": self.creds[1]}).encode(),
                                       headers={"Content-Type": "application/json", **UA})
            try:
                with urllib.request.urlopen(r, timeout=20) as f:
                    self.tokens["hub"] = json.load(f).get("token")
            except urllib.error.URLError:
                self.tokens["hub"] = None
        return self.tokens["hub"]


def ref_for(reg, repo, tag):
    """The name docker uses for a tag: nginx:1, user/app:2, ghcr.io/org/app:3."""
    if is_hub(reg):
        return f"{repo.removeprefix('library/')}:{tag}"
    return f"{reg}/{repo}:{tag}"
