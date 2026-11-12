"""Tests for Send files (berth/fileshare.py): no Docker, no GUI. Run: python3 tests/fileshare.py"""
import http.client
import io
import os
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from berth import fileshare as fs  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  ({str(detail)[:140]})" if detail else ""), flush=True)


def req(srv, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, dict(r.getheaders()), data


tmp = tempfile.mkdtemp()
video = os.path.join(tmp, "clip.mp4")
with open(video, "wb") as f:
    f.write(bytes(range(256)) * 4096)                  # 1 MiB of known bytes
folder = os.path.join(tmp, "photos")
os.makedirs(os.path.join(folder, "sub"))
open(os.path.join(folder, "a.jpg"), "wb").write(b"jpeg-a")
open(os.path.join(folder, "sub", "b.txt"), "w").write("hello")
secret = os.path.join(tmp, "secret.txt")
open(secret, "w").write("do not serve")
inbox = os.path.join(tmp, "inbox")
os.makedirs(inbox)

srv = fs.Server(port=18787)
sh = srv.add(fs.Share([video, folder], pin="4821"))
base = f"/s/{sh.id}/"
try:
    st, _h, body = req(srv, "GET", base)
    check("PIN page shown first", st == 200 and b"Enter the PIN" in body and b"clip.mp4" not in body)
    st, _h, body = req(srv, "GET", base + "f/0")
    check("files are hidden without the PIN", b"clip.mp4" not in body and b"\x00\x01\x02" not in body)
    st, _h, _b = req(srv, "POST", base + "pin", "pin=1111", {"Content-Type": "application/x-www-form-urlencoded"})
    check("wrong PIN refused", st == 401)
    st, h, _b = req(srv, "POST", base + "pin", "pin=4821", {"Content-Type": "application/x-www-form-urlencoded"})
    cookie = h.get("Set-Cookie", "").split(";")[0]
    check("right PIN gives a cookie", st == 303 and cookie.startswith(f"berth_{sh.id}="), h.get("Set-Cookie"))
    ck = {"Cookie": cookie}
    st, _h, body = req(srv, "GET", base, headers=ck)
    check("index lists the file and the folder, with a video player and zip", st == 200 and b"clip.mp4" in body and
          b"photos" in body and b"<video" in body and b"zip" in body)
    st, h, body = req(srv, "GET", base + "f/0", headers=ck)
    check("full download", st == 200 and len(body) == 1048576 and h.get("Accept-Ranges") == "bytes")
    st, h, body = req(srv, "GET", base + "f/0", headers={**ck, "Range": "bytes=1000-1999"})
    check("Range request (video seeking / resume)", st == 206 and body == (bytes(range(256)) * 4096)[1000:2000]
          and h.get("Content-Range") == "bytes 1000-1999/1048576", h.get("Content-Range"))
    st, _h, body = req(srv, "GET", base + "f/1/sub/b.txt", headers=ck)
    check("file inside a shared folder", st == 200 and body == b"hello")
    st, _h, body = req(srv, "GET", base + "f/1/", headers=ck)
    check("folder browsing", st == 200 and b"a.jpg" in body and b"sub" in body)
    st, _h, body = req(srv, "GET", base + "f/1/../secret.txt", headers=ck)
    st2, _h, body2 = req(srv, "GET", base + "f/1/%2e%2e/secret.txt", headers=ck)
    check("no escaping the shared folder (../)", b"do not serve" not in body + body2, (st, st2))
    st, h, body = req(srv, "GET", base + "zip", headers=ck)
    z = zipfile.ZipFile(io.BytesIO(body))
    names = sorted(z.namelist())
    check("download everything as .zip", names == ["clip.mp4", "photos/a.jpg", "photos/sub/b.txt"] and
          z.read("photos/sub/b.txt") == b"hello", names)
    st, _h, _b = req(srv, "GET", "/s/nope00/")
    check("unknown link → 'link has ended'", st == 404)

    # receive: phone uploads into a folder, never overwrites
    rc = srv.add(fs.Share(receive_dir=inbox))
    for _ in range(2):
        st, _h, body = req(srv, "PUT", f"/s/{rc.id}/up/holiday.jpg", b"pixels", {"Content-Length": "6"})
    st, _h, body = req(srv, "PUT", f"/s/{rc.id}/up/..%2F..%2Fescape.txt", b"x", {"Content-Length": "1"})
    saved = sorted(os.listdir(inbox))
    check("receive: uploads saved, second one not overwritten", "holiday.jpg" in saved and "holiday (1).jpg" in saved,
          saved)
    check("receive: names can't escape the folder", not os.path.exists(os.path.join(tmp, "escape.txt"))
          and "escape.txt" in saved, saved)
    st, _h, _b = req(srv, "PUT", f"/s/{sh.id}/up/x.txt", b"x", {**ck, "Content-Length": "1"})
    check("upload refused on a send-only share", st in (403, 404), st)

    # limits
    one = srv.add(fs.Share([video], max_downloads=1))
    req(srv, "GET", f"/s/{one.id}/f/0")
    st, _h, _b = req(srv, "GET", f"/s/{one.id}/")
    check("download limit ends the share", st == 404)
    ex = srv.add(fs.Share([video], expires=time.time() + 1))
    time.sleep(1.2)
    st, _h, _b = req(srv, "GET", f"/s/{ex.id}/")
    check("expiry ends the share", st == 404)
    check("LAN address detected", fs.lan_ip().count(".") == 3, fs.lan_ip())
finally:
    for s in list(srv.shares):
        srv.remove(s)
check("server stops when nothing is shared", srv.httpd is None)
bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f" — failed: {bad}" if bad else ""))
sys.exit(1 if bad else 0)
