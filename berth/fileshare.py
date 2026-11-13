"""Send files to a phone (or anyone): a small built-in web server that serves chosen files and folders straight from
disk, over the local Wi-Fi (link + QR code) or publicly through a Cloudflare quick tunnel.

  * Nothing is uploaded anywhere first; files stream from disk with HTTP Range support (videos play and seek,
    big downloads resume), folders can be browsed or fetched as one .zip built on the fly.
  * Per share: optional PIN, expiry, download limit; "receive" shares let a phone upload files into a folder.
  * The server only runs while at least one share is active, and is bound to the LAN only then.
Standard library only."""
import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import secrets
import socket
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8787
CHUNK = 256 * 1024
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"          # no look-alikes (l/1, o/0)


def lan_ip():
    """The address other devices on this network use to reach us (no traffic is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))                     # TEST-NET address: routing lookup only
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


class Share:
    def __init__(self, paths=(), receive_dir=None, pin=None, expires=None, max_downloads=None, title=None):
        self.id = "".join(secrets.choice(_ALPHABET) for _ in range(6))
        self.paths = [os.path.abspath(p) for p in paths]
        self.receive_dir = os.path.abspath(receive_dir) if receive_dir else None
        self.pin = pin
        self.expires = expires
        self.max_downloads = max_downloads
        self.downloads = 0
        self.uploads = []
        self.created = time.time()
        self.secret = secrets.token_bytes(16)
        self.title = title or (os.path.basename(self.paths[0]) + (f" + {len(self.paths) - 1} more" if len(self.paths) > 1
                                                                   else "") if self.paths else "Send to this computer")
        self.public_url = None
        self.tunnel_id = None

    @property
    def size(self):
        total = 0
        for p in self.paths:
            if os.path.isdir(p):
                for root, _d, files in os.walk(p):
                    total += sum(os.path.getsize(os.path.join(root, f)) for f in files
                                 if os.path.isfile(os.path.join(root, f)))
            elif os.path.isfile(p):
                total += os.path.getsize(p)
        return total

    def expired(self):
        if self.expires and time.time() >= self.expires:
            return True
        return bool(self.max_downloads and self.downloads >= self.max_downloads)

    def token(self):
        return hmac.new(self.secret, b"ok", hashlib.sha256).hexdigest()[:32]

    def resolve(self, index, sub=""):
        """Path of file #index (and a sub path inside a shared folder), refusing anything outside the share."""
        try:
            base = self.paths[int(index)]
        except (ValueError, IndexError):
            return None
        target = os.path.realpath(os.path.join(base, sub)) if sub else os.path.realpath(base)
        root = os.path.realpath(base)
        if target != root and not target.startswith(root + os.sep):
            return None
        return target if os.path.exists(target) else None


class Server:
    """One HTTP server for every active share; started on the first share, stopped after the last."""

    def __init__(self, port=DEFAULT_PORT):
        self.port, self.httpd, self.thread = port, None, None
        self.shares = {}
        self.lock = threading.Lock()
        self.on_change = None                            # called (from a worker thread) when a share changes

    # life cycle
    def add(self, share):
        with self.lock:
            self.shares[share.id] = share
        self._ensure()
        return share

    def remove(self, sid):
        with self.lock:
            self.shares.pop(sid, None)
            empty = not self.shares
        if empty:
            self.stop()

    def _ensure(self):
        if self.httpd:
            return
        handler = type("H", (_Handler,), {"server_ref": self})

        class _Quiet(ThreadingHTTPServer):
            def handle_error(self, request, client_address):     # phones drop connections all the time
                import sys
                if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, TimeoutError)):
                    from .util import log_error
                    log_error("file server", sys.exc_info()[1])
        last = None
        for port in range(self.port, self.port + 20):    # next free port if 8787 is taken
            try:
                self.httpd = _Quiet(("0.0.0.0", port), handler)
                self.port = port
                break
            except OSError as e:
                last = e
        if not self.httpd:
            raise OSError(f"No free port near {self.port}: {last}")
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="berth-files", daemon=True)
        self.thread.start()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def sweep(self):
        """Remove expired shares. Returns their titles."""
        gone = [s for s in list(self.shares.values()) if s.expired()]
        for s in gone:
            self.remove(s.id)
        return [s.title for s in gone]

    def lan_url(self, share):
        return f"http://{lan_ip()}:{self.port}/s/{share.id}/"

    def local_url(self, share):
        return f"http://127.0.0.1:{self.port}/s/{share.id}/"

    def changed(self):
        if self.on_change:
            self.on_change()


# ── firewall (so phones can reach the server) ───────────────────────────────────────────────────────────────────
def firewall_state(port):
    """('firewalld' | 'ufw' | None, open: bool|None). Uses only read-only queries."""
    import shutil
    import subprocess
    if shutil.which("firewall-cmd"):
        try:
            r = subprocess.run(["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5)
            if r.stdout.strip() == "running":
                q = subprocess.run(["firewall-cmd", f"--query-port={port}/tcp"], capture_output=True, text=True,
                                   timeout=5)
                return "firewalld", q.stdout.strip() == "yes"
        except (OSError, subprocess.SubprocessError):
            pass
    if shutil.which("ufw"):
        return "ufw", None                               # status needs root; assume it may block
    return None, True


def open_firewall(port):
    """Open the port until the next reboot. firewalld asks for the password itself (polkit); ufw goes through
    pkexec. Returns (ok, message)."""
    import subprocess
    kind, _open = firewall_state(port)
    if kind == "firewalld":
        cmd = ["firewall-cmd", f"--add-port={port}/tcp"]
    elif kind == "ufw":
        cmd = ["pkexec", "ufw", "allow", f"{port}/tcp"]
    else:
        return True, "No firewall to change"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


# ── public link (Cloudflare quick tunnel to the file server) ────────────────────────────────────────────────────
def start_public(engine, port, progress=None):
    """A cloudflared container on the host network pointing at the file server. Returns (container id, url)."""
    from . import sharing
    sharing.ensure_image(engine, sharing.TUNNEL_IMAGE, progress)
    name = f"berth-files-{port}"
    for c in engine.containers_filtered({"name": [f"^/{name}$"]}):
        engine.remove_container(c["Id"], force=True)
    r = engine.create_container(name, {
        "Image": sharing.TUNNEL_IMAGE, "Cmd": ["tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"],
        "Labels": {sharing.ROLE: "tunnel", sharing.TARGET: "Send files", sharing.PORT: str(port),
                   sharing.KEEP: "0", sharing.MODE: "files"},
        "HostConfig": {"NetworkMode": "host", "RestartPolicy": {"Name": "no"}}})
    engine.action(r["Id"], "start")
    if progress:
        progress("Waiting for Cloudflare to assign an address…")
    return r["Id"], sharing.wait_url(engine, r["Id"])


# ── the web pages phones see ────────────────────────────────────────────────────────────────────────────────────
_LOGO = None


def _logo_data_url():
    global _LOGO
    if _LOGO is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for p in (os.path.join(here, "data", "icons", "hicolor", "128x128", "apps", "io.github.subhadeeproy3902.Berth.png"),
                  os.path.join(here, "data", "icons", "berth-512.png")):
            try:
                with open(p, "rb") as f:
                    _LOGO = "data:image/png;base64," + base64.b64encode(f.read()).decode()
                    break
            except OSError:
                continue
        else:
            _LOGO = ""
    return _LOGO


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--dim:#667085;--line:#e4e7ec;--accent:#0797ef;--accent2:#0a223b}
@media(prefers-color-scheme:dark){:root{--bg:#0d1520;--card:#162131;--fg:#e8edf4;--dim:#94a3b8;--line:#22324a}}
*{box-sizing:border-box}body{margin:0;font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:720px;margin:0 auto;padding:20px 16px 48px}
header{display:flex;align-items:center;gap:12px;margin:4px 0 18px}header img{width:44px;height:44px;border-radius:11px}
header b{display:block;font-size:20px}header span{color:var(--dim);font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden}
.row{display:flex;align-items:center;gap:12px;padding:14px 16px;border-top:1px solid var(--line)}.row:first-child{border-top:0}
.row .name{flex:1;min-width:0}.row .name b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.row .name small{color:var(--dim)}.ic{width:36px;height:36px;border-radius:9px;background:color-mix(in srgb,var(--accent) 14%,transparent);display:grid;place-items:center;font-size:18px;flex:none}
a.btn,button{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:0;border-radius:999px;padding:10px 16px;
 font:600 15px system-ui;background:var(--accent);color:#fff;text-decoration:none;cursor:pointer;white-space:nowrap}
a.btn.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
.top{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
video,img.prev{display:block;width:100%;max-height:60vh;background:#000;border-radius:12px;margin:10px 0}
.pin{display:grid;gap:12px;max-width:340px;margin:12vh auto;text-align:center}
.pin input{font:600 28px/1 ui-monospace,monospace;text-align:center;letter-spacing:.2em;padding:14px;border-radius:14px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
.drop{border:2px dashed var(--line);border-radius:16px;padding:28px 16px;text-align:center;color:var(--dim)}
.bar{height:6px;background:var(--line);border-radius:9px;overflow:hidden;margin-top:6px}.bar i{display:block;height:100%;width:0;background:var(--accent)}
footer{color:var(--dim);font-size:13px;text-align:center;margin-top:22px}.err{color:#d92d20}
"""


def _page(title, body, sub=""):
    logo = _logo_data_url()
    return (f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,"
            f"initial-scale=1'><title>{html.escape(title)} · Berth</title><style>{CSS}</style><main><header>"
            f"{'<img alt=Berth src=' + chr(34) + logo + chr(34) + '>' if logo else ''}<div><b>{html.escape(title)}</b>"
            f"<span>{html.escape(sub)}</span></div></header>{body}<footer>Shared with Berth · straight from the "
            f"sender's computer</footer></main></html>").encode()


_ICONS = {"video": "🎬", "image": "🖼️", "audio": "🎵", "text": "📄", "application/pdf": "📕", "zip": "🗜️"}


def _icon(path):
    if os.path.isdir(path):
        return "📁"
    mt = mimetypes.guess_type(path)[0] or ""
    return _ICONS.get(mt) or _ICONS.get(mt.split("/")[0]) or ("🗜️" if "zip" in mt or "tar" in mt else "📄")


class _Handler(BaseHTTPRequestHandler):
    server_ref = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # helpers
    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _share(self):
        parts = urllib.parse.urlparse(self.path).path.split("/")
        if len(parts) < 3 or parts[1] != "s":
            return None, []
        srv = self.server_ref
        with srv.lock:
            sh = srv.shares.get(parts[2])
        if sh is None or sh.expired():
            if sh is not None:
                srv.remove(sh.id)
                srv.changed()
            return None, []
        return sh, [urllib.parse.unquote(p) for p in parts[3:]]

    def _authed(self, sh):
        if not sh.pin:
            return True
        cookie = self.headers.get("Cookie", "")
        return f"berth_{sh.id}={sh.token()}" in cookie.replace(" ", "")

    def _gone(self):
        self._send(404, _page("This link has ended", "<div class=card><div class=row><div class=name>"
                              "<b>Nothing to see here</b><small>The share was stopped, expired or reached its download "
                              "limit. Ask the sender for a new link.</small></div></div></div>"))

    # routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        sh, rest = self._share()
        if sh is None:
            return self._gone()
        if not self._authed(sh):
            return self._pin_form(sh)
        if not rest or rest == [""]:
            return self._index(sh)
        if rest[0] == "f" and len(rest) >= 2:
            p = sh.resolve(rest[1], "/".join(rest[2:]))
            if p is None:
                return self._gone()
            if os.path.isdir(p):
                return self._folder(sh, rest[1], "/".join(r for r in rest[2:] if r), p)
            return self._file(sh, p, inline=self.headers.get("Sec-Fetch-Dest") in ("video", "image", "audio")
                              or "inline" in urllib.parse.urlparse(self.path).query)
        if rest[0] == "zip":
            return self._zip(sh)
        return self._gone()

    def do_POST(self):
        sh, rest = self._share()
        if sh is None:
            return self._gone()
        if rest[:1] == ["pin"]:
            n = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(min(n, 2048)).decode(errors="replace"))
            if hmac.compare_digest((form.get("pin") or [""])[0].strip(), str(sh.pin)):
                return self._send(303, b"", headers={"Location": f"/s/{sh.id}/",
                                                     "Set-Cookie": f"berth_{sh.id}={sh.token()}; Path=/s/{sh.id}/; "
                                                                   "HttpOnly; SameSite=Lax"})
            time.sleep(1)                                 # slows down guessing
            return self._pin_form(sh, wrong=True)
        return self._gone()

    def do_PUT(self):
        sh, rest = self._share()
        if sh is None or not sh.receive_dir or not self._authed(sh) or rest[:1] != ["up"] or len(rest) < 2:
            self.close_connection = True                  # the unread body must not be parsed as a request
            return self._send(403, b"forbidden", "text/plain", {"Connection": "close"})
        name = os.path.basename(rest[1].replace("\\", "/")).strip() or "file"
        name = "".join(ch for ch in name if ch not in '<>:"|?*\x00').lstrip(".") or "file"
        dest = os.path.join(sh.receive_dir, name)
        base, ext = os.path.splitext(dest)
        i = 1
        while os.path.exists(dest):                       # never overwrite
            dest = f"{base} ({i}){ext}"
            i += 1
        n = int(self.headers.get("Content-Length") or 0)
        tmp = dest + ".part"
        try:
            with open(tmp, "wb") as f:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(CHUNK, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            if left:
                os.remove(tmp)
                return self._send(400, b"incomplete", "text/plain")
            os.replace(tmp, dest)
        except OSError as e:
            return self._send(500, str(e).encode(), "text/plain")
        sh.uploads.append((os.path.basename(dest), n, time.time()))
        self.server_ref.changed()
        self._send(200, json.dumps({"saved": os.path.basename(dest)}).encode(), "application/json")

    # pages
    def _pin_form(self, sh, wrong=False):
        body = (f"<form class=pin method=post action='/s/{sh.id}/pin'><div>Enter the PIN the sender gave you</div>"
                f"<input name=pin inputmode=numeric autocomplete=one-time-code autofocus maxlength=12>"
                f"<button>Open</button>{'<div class=err>That PIN is not right</div>' if wrong else ''}</form>")
        # the title is generic on purpose: a locked share must not reveal what is inside
        self._send(401 if wrong else 200, _page("Shared files", body, "Protected with a PIN"))

    def _index(self, sh):
        rows = []
        for i, p in enumerate(sh.paths):
            if not os.path.exists(p):
                continue
            name = html.escape(os.path.basename(p))
            mt = mimetypes.guess_type(p)[0] or ""
            if os.path.isdir(p):
                rows.append(f"<a class=row href='f/{i}/' style='color:inherit;text-decoration:none'><div class=ic>📁</div>"
                            f"<div class=name><b>{name}</b><small>Folder · tap to open</small></div>"
                            f"<span class='btn ghost' style='padding:8px 12px'>Open</span></a>")
                continue
            prev = ""
            if mt.startswith("video/"):
                prev = f"<video src='f/{i}?inline' controls preload=metadata playsinline></video>"
            elif mt.startswith("image/"):
                prev = f"<img class=prev src='f/{i}?inline' alt=''>"
            rows.append(f"<div class=row style='flex-wrap:wrap'><div class=ic>{_icon(p)}</div><div class=name><b>{name}"
                        f"</b><small>{human(os.path.getsize(p))}</small></div><a class=btn href='f/{i}' download>"
                        f"Download</a>{'<div style=flex-basis:100%>' + prev + '</div>' if prev else ''}</div>")
        top = ""
        if len(sh.paths) > 1 or any(os.path.isdir(p) for p in sh.paths):
            top = "<div class=top><a class=btn href='zip'>⬇ Download everything (.zip)</a></div>"
        recv = ""
        if sh.receive_dir:
            recv = _UPLOAD
        left = []
        if sh.expires:
            mins = max(1, int((sh.expires - time.time()) / 60))
            left.append(f"ends in {mins} min" if mins < 120 else f"ends in {mins // 60} h")
        if sh.max_downloads:
            left.append(f"{sh.max_downloads - sh.downloads} download(s) left")
        sub = (f"{len(sh.paths)} item(s) · {human(sh.size)}" if sh.paths else "Send files to the sender's computer") + \
              (" · " + " · ".join(left) if left else "")
        body = top + (f"<div class=card>{''.join(rows)}</div>" if rows else "") + recv
        self._send(200, _page(sh.title, body, sub))

    def _folder(self, sh, idx, sub, path):
        rows = []
        up = "/".join(sub.split("/")[:-1])
        rows.append(f"<a class=row href='/s/{sh.id}/f/{idx}/{urllib.parse.quote(up)}{'/' if up else ''}' "
                    f"style='color:inherit;text-decoration:none'><div class=ic>↩</div><div class=name><b>Back</b></div></a>"
                    if sub else f"<a class=row href='/s/{sh.id}/' style='color:inherit;text-decoration:none'>"
                                f"<div class=ic>↩</div><div class=name><b>All shared items</b></div></a>")
        try:
            entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            entries = []
        for e in entries:
            rel = urllib.parse.quote((sub + "/" if sub else "") + e.name)
            href = f"/s/{sh.id}/f/{idx}/{rel}" + ("/" if e.is_dir() else "")
            meta = "Folder" if e.is_dir() else human(e.stat().st_size)
            btn = "" if e.is_dir() else f"<a class=btn href='{href}' download>Download</a>"
            rows.append(f"<div class=row><div class=ic>{_icon(e.path)}</div><div class=name><a href='{href}' "
                        f"style='color:inherit'><b>{html.escape(e.name)}</b></a><small>{meta}</small></div>{btn}</div>")
        self._send(200, _page(os.path.basename(path), f"<div class=card>{''.join(rows)}</div>", sh.title))

    def _file(self, sh, path, inline=False):
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            try:
                if a:
                    start, end = int(a), int(b) if b else size - 1
                else:
                    start, end = size - int(b), size - 1
                if not (0 <= start <= end < size):
                    raise ValueError
                code = 206
            except ValueError:
                return self._send(416, b"", headers={"Content-Range": f"bytes */{size}"})
        name = os.path.basename(path)
        disp = "inline" if inline else "attachment"
        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f"{disp}; filename*=UTF-8''{urllib.parse.quote(name)}")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        left = end - start + 1
        try:
            with open(path, "rb") as f:
                f.seek(start)
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return
        if not inline and start == 0 and left == 0:       # a complete download counts once
            sh.downloads += 1
            self.server_ref.changed()

    def _zip(self, sh):
        """Stream one .zip of everything (stored, not compressed: fast, and media is compressed already)."""
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(sh.title)}.zip")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            return

        class _W:
            def __init__(s, out):
                s.out, s.n = out, 0

            def write(s, b):
                s.out.write(b)
                s.n += len(b)
                return len(b)

            def tell(s):
                return s.n

            def flush(s):
                s.out.flush()
        try:
            with zipfile.ZipFile(_W(self.wfile), "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for p in sh.paths:
                    if os.path.isdir(p):
                        top = os.path.dirname(p)
                        for root, _dirs, files in os.walk(p):
                            for fn in files:
                                full = os.path.join(root, fn)
                                if os.path.isfile(full):
                                    z.write(full, os.path.relpath(full, top))
                    elif os.path.isfile(p):
                        z.write(p, os.path.basename(p))
            sh.downloads += 1
            self.server_ref.changed()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


_UPLOAD = """<div class=card style='margin-top:14px'><div class=row style='display:block'><div class=drop id=d>
<b>Send files to this computer</b><br><small>Pick photos, videos or documents — they are saved on the sender's machine</small>
<br><br><label class=btn>Choose files<input id=f type=file multiple hidden></label></div><div id=l></div></div></div>
<script>
const f=document.getElementById('f'),l=document.getElementById('l');
f.onchange=()=>{[...f.files].forEach(up)};
function up(file){const r=document.createElement('div');r.className='row';
r.innerHTML='<div class=ic>⬆</div><div class=name><b></b><small>Uploading…</small><div class=bar><i></i></div></div>';
r.querySelector('b').textContent=file.name;l.appendChild(r);
const x=new XMLHttpRequest();x.open('PUT','up/'+encodeURIComponent(file.name));
x.upload.onprogress=e=>{if(e.lengthComputable)r.querySelector('i').style.width=(e.loaded/e.total*100)+'%'};
x.onload=()=>{r.querySelector('small').textContent=x.status==200?'Saved ✓':'Failed ('+x.status+')'};
x.onerror=()=>{r.querySelector('small').textContent='Failed — check the connection'};x.send(file)}
</script>"""
