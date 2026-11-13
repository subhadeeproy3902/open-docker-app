"""Tell the user when a newer Berth is released. Once a day, one anonymous request to GitHub's public releases API
(the only thing sent is "Berth/<version>" as the user agent). Off in Settings; never in the Flatpak, which Flathub
updates."""
import json
import os
import re
import time
import urllib.request

from gi.repository import GLib

from .util import VERSION

API = "https://api.github.com/repos/subhadeeproy3902/open-docker-app/releases/latest"
STATE = os.path.join(GLib.get_user_state_dir(), "berth", "update.json")


def _v(s):
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:3]) or (0,)


def newer(latest, current=VERSION):
    return _v(latest) > _v(current)


def check(force=False):
    """(version, url) of a newer release, or None. At most one request a day unless forced."""
    try:
        with open(STATE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if not force and time.time() - st.get("checked", 0) < 86400:
        tag, url = st.get("tag"), st.get("url")
    else:
        req = urllib.request.Request(API, headers={"User-Agent": f"Berth/{VERSION}",
                                                   "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        tag, url = data.get("tag_name", ""), data.get("html_url", "")
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump({"checked": time.time(), "tag": tag, "url": url}, f)
    if tag and newer(tag):
        return tag.lstrip("v"), url
    return None
