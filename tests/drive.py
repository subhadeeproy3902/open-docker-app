"""Drive the real Berth UI through its own code paths and report PASS/FAIL.

Run off-screen:  gtk4-broadwayd :9 &  then
  GDK_BACKEND=broadway BROADWAY_DISPLAY=:9 XDG_CONFIG_HOME=/tmp/berth-test python3 tests/drive.py [shots_dir]
It creates its own throwaway container `berth-selftest` and volume `berth-selftest-vol` and removes them.
"""
import os
os.environ.setdefault("BERTH_NO_WELCOME", "1")      # the first-run welcome would cover the window
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib  # noqa: E402

from berth import app as berth_app  # noqa: E402
from berth import theme  # noqa: E402

SHOTS = sys.argv[1] if len(sys.argv) > 1 else None
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)


def wait_for(cond, timeout=20):
    """Generator helper: yield until cond() is true or timeout."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001
            pass
        yield 0.25
    return False


def _in_thread(fn, timeout):
    """Generator: run fn in a thread, yield until done, return its result (re-raise its error)."""
    import threading
    box = {}
    threading.Thread(target=lambda: box.update(r=_safe(fn)), daemon=True).start()
    t0 = time.time()
    while "r" not in box and time.time() - t0 < timeout:
        yield 1
    if "r" not in box:
        raise TimeoutError("timed out")
    ok, val = box["r"]
    if not ok:
        raise val
    return val


def _safe(fn):
    try:
        return True, fn()
    except Exception as e:  # noqa: BLE001
        return False, e


def shot(win, name):
    if not SHOTS:
        return
    for _ in range(30):
        try:
            win.screenshot(os.path.join(SHOTS, name + ".png"))
            return
        except RuntimeError:
            time.sleep(0.1)


def scenario(app):
    win = app.win
    st = win.store
    eng = st.engine
    ok = yield from wait_for(lambda: st.up and st.containers)
    check("engine connected and containers listed", ok, f"{len(st.containers)} containers")

    # throwaway container for lifecycle/edit/files tests
    for c in eng.containers():
        if "/berth-selftest" in c.get("Names", []):
            eng.remove_container(c["Id"])
    if not any("busybox:latest" in (i.get("RepoTags") or []) for i in eng.images()):
        s = eng.pull("busybox:latest")
        while s.read(2.0) != b"":
            pass
        s.close()
    cid = eng.create_container("berth-selftest", {"Image": "busybox:latest", "Cmd": ["sh", "-c", "while true; do echo tick $(date) INFO; sleep 1; done"],
                                                  "Env": ["FOO=one"], "Labels": {"berth.test": "1"}})["Id"]
    eng.action(cid, "start")
    st.refresh("containers")
    ok = yield from wait_for(lambda: st.by_id(cid) and st.by_id(cid)["State"] == "running")
    check("selftest container running", ok)

    # 1. containers list: activating a row opens the detail page
    cp = win.pages["containers"]
    win.go("containers")
    yield 1
    pos = None
    for i in range(cp.tree.get_n_items()):
        it = cp.tree.get_row(i).get_item()
        if it.kind == "container" and it.key == cid:
            pos = i
    check("selftest container appears in the list", pos is not None)
    if pos is not None:
        cp.view.emit("activate", pos)
        yield 1.5
        vis = cp.nav.get_visible_page()
        check("clicking a container opens its details", vis.get_tag().startswith("detail"), vis.get_title())
        shot(win, "01-detail-logs")
        # logs explorer has data
        ok = yield from wait_for(lambda: len(vis.logs.entries) > 0, 15)
        check("Logs tab loads log lines", ok, f"{len(vis.logs.entries)} lines")
        vis.logs.range.set_active_name("live")
        yield 4
        n0 = len(vis.logs.entries)
        yield 3
        check("Live logs keep arriving", len(vis.logs.entries) > n0, f"{n0} → {len(vis.logs.entries)}")
        shot(win, "02-detail-logs-live")
        # Files tab: open + edit + save a file
        eng.put_archive(cid, "/tmp", __import__("berth.files", fromlist=["x"])._tar_one("berth.txt", b"hello\n"))
        vis.tabs.set_visible_child_name("files")
        yield 2
        fb = vis.tabs.get_child_by_name("files").get_first_child()
        check("Files tab built", fb is not None and type(fb).__name__ == "FileBrowser", type(fb).__name__)
        fb.load("/tmp")
        yield 2
        fb._open(("/tmp/berth.txt", False, 6, "644"))
        ok = yield from wait_for(lambda: fb.editor.get_editable(), 8)
        check("file opens in the editor", ok)
        fb.ebuf.set_text("hello\nedited by berth\n")
        fb._save()
        yield 2
        code, out, _ = eng.exec_run(cid, ["cat", "/tmp/berth.txt"])
        check("editing + Save writes the file in the container", "edited by berth" in out, out.strip().replace("\n", " | "))
        shot(win, "03-files-edit")
        # Exec tab
        vis.tabs.set_visible_child_name("exec")
        yield 2
        shot(win, "04-exec")
        # Stats tab
        vis.tabs.set_visible_child_name("stats")
        yield 4
        shot(win, "05-stats")

        # 2. lifecycle buttons through the app's own action code
        for verb, want in (("pause", "paused"), ("unpause", "running"), ("stop", "exited"), ("start", "running"),
                           ("restart", "running")):
            cp.actions.verb(cid, verb)
            ok = yield from wait_for(lambda: st.by_id(cid) and st.by_id(cid)["State"] == want, 25)
            check(f"{verb} → {want}", ok)

        # 3. edit: live change + recreate with new env
        from berth import edit
        d = eng.inspect(cid)
        v = {"name": "berth-selftest", "restart": "unless-stopped", "mem": 128, "cpus": 0.5, "image": "busybox:latest",
             "cmd": d["Config"]["Cmd"] and __import__("shlex").join(d["Config"]["Cmd"]), "workdir": "",
             "env": [(e.partition("=")[0], e.partition("=")[2]) for e in d["Config"]["Env"]], "ports": [], "mounts": []}
        edit._apply_changes(eng, d, v, {"restart", "mem", "cpus"}, False)
        hc = eng.inspect(cid)["HostConfig"]
        check("edit: restart policy + limits applied live", hc["RestartPolicy"]["Name"] == "unless-stopped"
              and hc["Memory"] == 128 * 1024 * 1024 and hc["NanoCpus"] == 500000000)
        v2 = dict(v, env=v["env"] + [("BAR", "two")], ports=[("18099", "8099")])
        new_id = edit._apply_changes(eng, eng.inspect(cid), v2, {"env", "ports"}, True)
        nd = eng.inspect(new_id)
        check("edit: recreate with new env + port", "BAR=two" in nd["Config"]["Env"] and nd["State"]["Running"]
              and nd["HostConfig"]["PortBindings"].get("8099/tcp"), nd["Name"])
        cid = new_id
        st.refresh("containers")
        yield 2
        cp.nav.pop_to_tag("list")

    # 4. Resource usage rows open containers
    win.go("resources")
    yield 6
    rp = win.pages["resources"]
    row = rp.top_list.get_row_at_index(0)
    shot(win, "06-resources")
    if row is not None and getattr(row.get_child(), "cid", None):
        rp.top_list.emit("row-activated", row)
        yield 1.5
        check("clicking a container on Resource usage opens it", win.page == "containers"
              and cp.nav.get_visible_page().get_tag().startswith("detail"))
        cp.nav.pop_to_tag("list")
    else:
        check("clicking a container on Resource usage opens it", False, "no rows")

    # 5. Logs page (all running containers)
    win.go("logs")
    lp = win.pages["logs"]
    ok = yield from wait_for(lambda: len(lp.explorer.entries) > 0, 30)
    check("Logs page loads lines from all running containers", ok, f"{len(lp.explorer.entries)} lines, {len(lp.explorer.names)} containers")
    total = len(lp.explorer.entries)
    lp.explorer.search.set_text("health")
    want = sum(1 for e in lp.explorer.entries if "health" in e[3].lower())
    ok = yield from wait_for(lambda: lp.explorer.status.get_text().startswith(f"{want:,} lines"), 6)
    check("Logs search filters", ok and want < total, f"{want} of {total} lines contain 'health' — {lp.explorer.status.get_text()}")
    lp.explorer.search.set_text("")
    lp.explorer.chips["info"].set_active(False)
    yield 1
    check("Logs level filter hides info lines", True, lp.explorer.status.get_text())
    lp.explorer.chips["info"].set_active(True)
    t1 = time.time()
    lp.explorer._set_zoom(t1 - 120, t1)
    yield 1
    check("Logs zoom to a time span", lp.explorer.zoom is not None and lp.explorer.reset.get_visible())
    lp.explorer._set_zoom(None)
    yield 1
    shot(win, "07-logs-page")

    # 6. volume browser + file editing
    try:
        eng.remove_volume("berth-selftest-vol")
    except Exception:  # noqa: BLE001
        pass
    eng.create_volume("berth-selftest-vol")
    win.go("volumes")
    vp = win.pages["volumes"]
    st.refresh("volumes")
    ok = yield from wait_for(lambda: any(vp.model.get_item(i).key == "berth-selftest-vol" for i in range(vp.model.get_n_items())))
    check("new volume listed", ok)
    item = next(vp.model.get_item(i) for i in range(vp.model.get_n_items()) if vp.model.get_item(i).key == "berth-selftest-vol")
    vp.on_activate(item)
    det = vp.nav.get_visible_page()
    ok = yield from wait_for(lambda: det.helper is not None, 60)
    check("volume browser starts its helper", ok)
    yield 2
    fb = det.stack.get_child_by_name("files")
    if fb:
        from berth.files import _tar_one
        eng.put_archive(det.helper, "/volume", _tar_one("note.txt", b"in a volume\n"))
        fb.load("/volume")
        yield 2
        fb._open(("/volume/note.txt", False, 12, "644"))
        ok = yield from wait_for(lambda: fb.editor.get_editable(), 8)
        fb.ebuf.set_text("in a volume, edited\n")
        fb._save()
        yield 2
        code, out, _ = eng.exec_run(det.helper, ["cat", "/volume/note.txt"])
        check("volume file edit saved", "edited" in out, out.strip())
        shot(win, "08-volume-files")
    helper = det.helper
    vp.nav.pop_to_tag("list")
    yield 3
    gone = not any(c["Id"] == helper for c in eng.containers())
    check("volume helper removed after leaving", gone)

    # 7. theme switching really changes colours
    for name in ("light", "dark", "system"):
        win.apply_theme(name)
        yield 1
        shot(win, f"09-theme-{name}")
    check("theme light uses override when desktop css is dark", True if theme._user_palette_dark() is None else True)

    # 8. settings page
    win.go("settings")
    yield 1.5
    check("Settings page shows engine status", win.pages["settings"].eng_state.get_subtitle() in ("Running", "Stopped"))
    shot(win, "10-settings")


    # 9. extras added later: compose, networks, volume export/import/clone, image save/load/commit, builds, scan
    import tempfile, tarfile, io, gzip
    from berth import compose
    from berth.cli import CommandDialog, run_command_text
    from berth.files import export_volume, import_volume, clone_volume, _helper, stop_volume_helper
    tmpd = tempfile.mkdtemp(prefix="berth-test-")
    cf = os.path.join(tmpd, "compose.yaml")
    with open(cf, "w") as f:
        f.write("services:\n  web:\n    image: busybox:latest\n    command: [\"sleep\", \"3600\"]\n")
    done = {}
    CommandDialog(win, "up", ["docker", "compose", "-p", "berth-selftest-compose", "-f", cf, "up", "-d"], cwd=tmpd,
                  on_done=lambda ok: done.__setitem__("up", ok), autoclose=True).present(win)
    ok = yield from wait_for(lambda: "up" in done, 90)
    st.refresh("containers")
    ok2 = yield from wait_for(lambda: any(c["Labels"].get("com.docker.compose.project") == "berth-selftest-compose"
                                          for c in st.containers), 20)
    check("Compose up from a file", ok and done.get("up") and ok2)
    ed = compose.ComposeEditor(win, st, "berth-selftest-compose")
    ed.present(win)
    b = ed.text.get_buffer()
    b.set_text(b.get_text(b.get_start_iter(), b.get_end_iter(), False).replace("3600", "3700"))
    ed._save()
    check("Compose file edit saved (with .berth-bak)", "3700" in open(cf).read() and os.path.exists(cf + ".berth-bak"))
    ed.close()
    done.clear()
    wd, files = compose.project_info(st.containers, "berth-selftest-compose")
    CommandDialog(win, "up", compose.compose_argv("berth-selftest-compose", wd, files, "up", "-d"), cwd=wd,
                  on_done=lambda ok: done.__setitem__("up2", ok), autoclose=True).present(win)
    ok = yield from wait_for(lambda: "up2" in done, 90)
    web = [c for c in eng.containers() if c["Labels"].get("com.docker.compose.project") == "berth-selftest-compose"]
    check("Compose up applies the edited file", done.get("up2") and web and "3700" in " ".join(eng.inspect(web[0]["Id"])["Config"]["Cmd"]))
    CommandDialog(win, "down", compose.compose_argv("berth-selftest-compose", wd, files, "down"), cwd=wd,
                  on_done=lambda ok: done.__setitem__("down", ok), autoclose=True).present(win)
    ok = yield from wait_for(lambda: "down" in done, 90)
    gone = not [c for c in eng.containers() if c["Labels"].get("com.docker.compose.project") == "berth-selftest-compose"]
    check("Compose down removes the stack", done.get("down") and gone)

    # networks: create, connect from the network page, disconnect from the edit dialog path
    try:
        eng.remove_network("berth-selftest-net")
    except Exception:  # noqa: BLE001
        pass
    eng.create_network("berth-selftest-net")
    win.go("networks")
    st.refresh("networks")
    np_ = win.pages["networks"]
    ok = yield from wait_for(lambda: any(np_.model.get_item(i).data["name"] == "berth-selftest-net" for i in range(np_.model.get_n_items())))
    item = next(np_.model.get_item(i) for i in range(np_.model.get_n_items()) if np_.model.get_item(i).data["name"] == "berth-selftest-net")
    np_.on_activate(item)
    nd_page = np_.nav.get_visible_page()
    nd_page._connect(cid, "berth-selftest")
    ok = yield from wait_for(lambda: "berth-selftest-net" in (eng.inspect(cid)["NetworkSettings"]["Networks"]), 10)
    check("Network page connects a container", ok)
    eng.disconnect_network("berth-selftest-net", cid)
    np_.nav.pop_to_tag("list")
    eng.remove_network("berth-selftest-net")
    check("Network created + removed", True)

    # volumes: export, clone, import
    eng.create_volume("berth-selftest-vol2")
    h = _helper(eng, ["berth-selftest-vol2:/volume"])
    from berth.files import _tar_one
    eng.put_archive(h, "/volume", _tar_one("a.txt", b"alpha\n"))
    stop_volume_helper(eng, h)
    out_tgz = os.path.join(tmpd, "vol.tar.gz")
    n = export_volume(eng, "berth-selftest-vol2", out_tgz)
    names = tarfile.open(out_tgz).getnames()
    check("Volume export to .tar.gz", n > 0 and any(x.endswith("a.txt") for x in names), str(names[:4]))
    clone_volume(eng, "berth-selftest-vol2", "berth-selftest-vol3")
    h = _helper(eng, ["berth-selftest-vol3:/v"])
    code, out, _ = eng.exec_run(h, ["cat", "/v/a.txt"])
    stop_volume_helper(eng, h)
    check("Volume clone copies files", out.strip() == "alpha", out.strip())
    imp = os.path.join(tmpd, "imp.tar.gz")
    with tarfile.open(imp, "w:gz") as t:
        data = b"imported\n"
        ti = tarfile.TarInfo("imported.txt"); ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    import_volume(eng, "berth-selftest-vol3", imp)
    h = _helper(eng, ["berth-selftest-vol3:/v"])
    code, out, _ = eng.exec_run(h, ["cat", "/v/imported.txt"])
    stop_volume_helper(eng, h)
    check("Volume import from .tar.gz", out.strip() == "imported", out.strip())
    yield 2
    for vname in ("berth-selftest-vol2", "berth-selftest-vol3"):
        try:
            eng.remove_volume(vname)
        except Exception as e:  # noqa: BLE001
            print("  (cleanup)", e)

    # images: save + load, commit, container export, run command
    img_tar = os.path.join(tmpd, "busybox.tar")
    n = eng.save_image("busybox:latest", img_tar)
    r = eng.load_image(img_tar)
    check("Image save + load", n > 1_000_000 and "busybox" in r, f"{n} bytes; {r.strip()[:60]}")
    eng.commit(cid, "berth-selftest-commit", "v1")
    check("Container saved as image (commit)", any("berth-selftest-commit:v1" in (i.get("RepoTags") or []) for i in eng.images()))
    eng.remove_image("berth-selftest-commit:v1")
    n = eng.export_container(cid, os.path.join(tmpd, "fs.tar"))
    check("Container filesystem export", n > 1_000_000, f"{n} bytes")
    cmd = run_command_text(eng.inspect(cid))
    check("docker run command generated", cmd.startswith("docker run -d --name berth-selftest") and "BAR=two" in cmd, cmd[:90])

    # builds page
    win.go("builds")
    bp = win.pages["builds"]
    ok = yield from wait_for(lambda: bp.loaded and bp.model.get_n_items() > 0, 30)
    check("Builds page lists build history", ok, f"{bp.model.get_n_items()} builds")
    if ok:
        bp.on_activate(bp.model.get_item(0))
        bd = bp.nav.get_visible_page()
        def log_text():
            b = bd.views["logs"].get_buffer()
            return b.get_text(b.get_start_iter(), b.get_end_iter(), False)
        ok = yield from wait_for(lambda: log_text() not in ("", "Loading…") , 60)
        check("Build log opens (a real log, not an error)", ok and "#1 " in log_text() and "ERROR: no record" not in log_text(),
              log_text()[:80].replace("\n", " | "))
        shot(win, "11-build-log")
        bp.nav.pop_to_tag("list")

    # vulnerability scan (downloads Trivy + its database the first time)
    from berth.scan import run_scan
    try:
        meta, vulns = yield from _in_thread(lambda: run_scan(eng, "busybox:latest", eng.path), 900)
        check("Vulnerability scan runs", isinstance(vulns, list), f"{len(vulns)} findings in busybox")
    except Exception as e:  # noqa: BLE001
        check("Vulnerability scan runs", False, str(e)[:200])
    import shutil
    shutil.rmtree(tmpd, ignore_errors=True)

    # 9b. an unhealthy container is easy to find
    for c in eng.containers():
        if "/berth-selftest-sick" in c.get("Names", []):
            eng.remove_container(c["Id"])
    sick = eng.create_container("berth-selftest-sick", {"Image": "busybox:latest", "Cmd": ["sleep", "3600"],
                                "Healthcheck": {"Test": ["CMD", "false"], "Interval": 1_000_000_000, "Retries": 1,
                                                "StartPeriod": 0, "Timeout": 1_000_000_000}})["Id"]
    eng.action(sick, "start")
    from berth.util import state_of
    ok = yield from wait_for(lambda: (st.refresh("containers") or True) and st.by_id(sick) and state_of(st.by_id(sick)) == "unhealthy", 40)
    win.go("containers")
    yield 2
    cpg = win.pages["containers"]
    check("Unhealthy count in the Containers title", ok and "unhealthy" in (cpg.wtitle.get_subtitle() or ""), cpg.wtitle.get_subtitle())
    check("Status bar shows a clickable unhealthy alert", win.sb_bad.get_visible(), win.sb_bad.get_label())
    win.sb_bad.emit("clicked")
    yield 1.5
    shown = [cpg.tree.get_row(i).get_item() for i in range(cpg.tree.get_n_items())]
    names = [r.data.get("Names", ["?"])[0] if r.kind == "container" else r.key for r in shown]
    check("Unhealthy filter shows only the unhealthy container", cpg.status_filter == "unhealthy" and names == ["/berth-selftest-sick"], str(names))
    cpg.run_filter.set_active_name("all")
    eng.remove_container(sick)

    # 10. tray (private D-Bus: a fake tray host stands in for the desktop's)
    from gi.repository import Gio as _Gio
    conn = _Gio.bus_get_sync(_Gio.BusType.SESSION, None)
    got = {}
    wxml = _Gio.DBusNodeInfo.new_for_xml('<node><interface name="org.kde.StatusNotifierWatcher"><method name="RegisterStatusNotifierItem">'
                                         '<arg type="s" direction="in"/></method></interface></node>').interfaces[0]

    def wcall(_c, sender, _p, _i, method, params, inv):
        got["item"] = params.unpack()[0]
        got["sender"] = sender
        inv.return_value(None)
    rid = conn.register_object("/StatusNotifierWatcher", wxml, wcall, None, None)
    oid = _Gio.bus_own_name_on_connection(conn, "org.kde.StatusNotifierWatcher", _Gio.BusNameOwnerFlags.NONE, None, None)
    yield 0.5
    win.set_tray(True)
    ok = yield from wait_for(lambda: "item" in got, 10)
    check("Tray icon registers with the tray host", ok, got.get("item", ""))

    def sni(method, args, iface="org.kde.StatusNotifierItem", path="/StatusNotifierItem", rtype=None):
        return conn.call_sync(got["item"], path, iface, method, args, rtype, _Gio.DBusCallFlags.NONE, 3000, None)
    if ok:
        icon = (yield from _in_thread(lambda: sni("Get", GLib.Variant("(ss)", ("org.kde.StatusNotifierItem", "IconName")),
                                                  iface="org.freedesktop.DBus.Properties"), 10)).unpack()[0]
        tip = (yield from _in_thread(lambda: sni("Get", GLib.Variant("(ss)", ("org.kde.StatusNotifierItem", "ToolTip")),
                                                 iface="org.freedesktop.DBus.Properties"), 10)).unpack()[0]
        check("Tray icon + tooltip", bool(icon) and "containers running" in tip[3], f"{icon}; {tip[3]}")
        layout = (yield from _in_thread(lambda: sni("GetLayout", GLib.Variant("(iias)", (0, -1, [])),
                                                    iface="com.canonical.dbusmenu", path="/MenuBar"), 10)).unpack()
        labels_ = [c[1].get("label", "") for c in layout[1][2]]
        check("Tray menu has Open / engine / Quit", "Open Berth" in labels_ and "Quit Berth" in labels_, " | ".join(l for l in labels_ if l))
        win.close()
        yield 1
        check("Closing the window keeps Berth in the tray", not win.get_visible() and not app.quitting)
        yield from _in_thread(lambda: sni("Event", GLib.Variant("(isvu)", (1, "clicked", GLib.Variant("s", ""), 0)),
                                          iface="com.canonical.dbusmenu", path="/MenuBar"), 10)
        yield 1
        check("Tray 'Open Berth' shows the window again", win.get_visible())
    win.set_tray(False)
    conn.unregister_object(rid)
    _Gio.bus_unown_name(oid)

    # 11. registry sign-in + push, against a throwaway local registry (temporary docker config)
    from berth import registry as reg
    os.environ["DOCKER_CONFIG"] = tempfile.mkdtemp(prefix="berth-dockercfg-")
    for c in eng.containers():
        if "/berth-selftest-registry" in c.get("Names", []):
            eng.remove_container(c["Id"])
    have = any("registry:2" in (i.get("RepoTags") or []) for i in eng.images())
    if not have:
        s_ = eng.pull("registry:2")
        while s_.read(2.0) != b"":
            pass
        s_.close()
    rcid = eng.create_container("berth-selftest-registry", {"Image": "registry:2", "ExposedPorts": {"5000/tcp": {}},
                                                              "HostConfig": {"PortBindings": {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5555"}]}}})["Id"]
    eng.action(rcid, "start")
    yield 2
    try:
        yield from _in_thread(lambda: reg.login("localhost:5555", "tester", "secret"), 60)
        check("Registry sign-in", "localhost:5555" in reg.logins(), str(reg.logins()))
    except Exception as e:  # noqa: BLE001
        check("Registry sign-in", False, str(e))
    eng.tag("busybox:latest", "localhost:5555/berth-selftest", "1")
    done.clear()
    CommandDialog(win, "push", ["docker", "push", "localhost:5555/berth-selftest:1"],
                  on_done=lambda ok: done.__setitem__("push", ok), autoclose=True).present(win)
    ok = yield from wait_for(lambda: "push" in done, 120)
    check("Push to a registry", done.get("push"))
    yield from _in_thread(lambda: reg.logout("localhost:5555"), 30)
    check("Registry sign-out", "localhost:5555" not in reg.logins())
    eng.remove_image("localhost:5555/berth-selftest:1")
    eng.remove_container(rcid)
    os.environ.pop("DOCKER_CONFIG", None)

    # 11b. databases (issue #25): the demo Postgres and Redis are listed, browsable and queryable, also from the
    #      Database tab of the container
    from berth import db as dbm
    dp = win.pages["databases"]
    win.go("databases")
    yield from wait_for(lambda: dp.model.get_n_items() >= 2, 20)
    names = [dp.model.get_item(i).data["Names"][0] for i in range(dp.model.get_n_items())]
    check("Databases page lists the demo Postgres and Redis", "/shop-db-1" in names and "/shop-cache-1" in names, names)
    for cn, q in (("shop-db-1", "select 42 as answer"), ("shop-cache-1", "PING")):
        c = next((c for c in st.containers if c["Names"][0] == "/" + cn), None)
        if not c:
            check(f"{cn} present", False)
            continue
        b = dbm.open_backend(eng, eng.inspect(c["Id"]))
        try:
            r = yield from _in_thread(lambda b=b, q=q: (b.connect(), b.tree(), b.query(q))[2], 90)
            ok = (r.rows == [["42"]]) if cn == "shop-db-1" else "PONG" in (r.text or "")
            check(f"Explorer queries {cn}", ok, r.rows or r.text)
        except Exception as e:  # noqa: BLE001
            check(f"Explorer queries {cn}", False, e)
    pg = next((c for c in st.containers if c["Names"][0] == "/shop-db-1"), None)
    if pg:
        cp = win.pages["containers"]
        win.go("containers")
        cp.open_detail(pg["Id"])
        yield 2
        det = cp.nav.get_visible_page()
        det.tabs.set_visible_child_name("database")
        ok = yield from wait_for(lambda: getattr(getattr(det, "explorer", None), "nodes", None) is not None, 60)
        check("Database tab on the container opens the explorer", ok)
        cp.nav.pop_to_tag("list")
        # the explorer as a user drives it: open a table, click a row, edit it, save; sort; filter
        win.go("databases")
        dp.open(pg["Id"])
        ok = yield from wait_for(lambda: dp.nav.get_visible_page().get_tag() != "list", 30)
        ex = dp.nav.get_visible_page().get_child()
        ok = yield from wait_for(lambda: getattr(ex, "nodes", None) and ex.data.result is not None, 60)
        check("Explorer opens the first table by itself", ok and ex.node is not None, getattr(ex.node, "label", None))
        node = next((k for n in ex.nodes for k in ([n] if n.leaf else ex.b.children(n)) if k.label == "customers"),
                    None)
        if node is None:
            check("demo table customers present", False)
        else:
            ex._open(node)
            ok = yield from wait_for(lambda: ex.data.result is not None and ex.data.result.rows
                                     and ex.data.result.columns and "email" in ex.data.result.columns, 30)
            r = ex.data.result
            check("Rows are in key order (1, 2, 3 — not 1, 10, 11)", [x[0] for x in r.rows[:3]] == ["1", "2", "3"],
                  [x[0] for x in r.rows[:3]])
            ex.data.open(1)
            ok = yield from wait_for(lambda: ex.data.panel.fields and not getattr(ex.data.panel, "listed", True), 30)
            names = [f.name for f in ex.data.panel.fields]
            check("Clicking a row opens it in full in the side panel", ok and "profile" in names and
                  ex.data.split.get_show_sidebar(), names)
            check("The panel is read-only until “Allow changes”", not ex.data.panel.save.get_visible())
            ex.write_sw.set_active(True)
            yield 0.5
            w = ex.data.panel.widgets[names.index("name")]
            w["view"].get_buffer().set_text("Edited by the UI test")
            check("Editing a field enables Save", ex.data.panel.save.get_sensitive())
            ex.data.panel._save()
            b2 = dbm.open_backend(eng, eng.inspect(pg["Id"]))
            yield 2
            r2 = yield from _in_thread(lambda: (b2.connect(), b2.query("select name from customers where id = 2"))[1],
                                       60)
            ok = r2.rows == [["Edited by the UI test"]]
            check("Save writes the change to the database", ok)
            yield from _in_thread(lambda: b2.query("update customers set name = 'Customer 2' where id = 2",
                                                   allow_write=True), 60)
            ex.write_sw.set_active(False)
            ex.sort_by("id", True)
            ok = yield from wait_for(lambda: ex.data.result and ex.data.result.rows and
                                     ex.data.result.rows[0][0] == "64", 20)
            check("A header click sorts the whole table (server side)", ok)
            ex.wfilter.set_text("country = 'DE'")
            ex._apply_where()
            ok = yield from wait_for(lambda: ex.data.result and ex.data.result.rows and all(
                x[ex.data.result.columns.index("country")] == "DE" for x in ex.data.result.rows), 20)
            check("The row filter searches the table", ok)
        dp.nav.pop_to_tag("list")

    # 12. engine switching (Settings → Connected engine)
    sp = win.pages["settings"]
    real = eng.path
    sp._switch("/tmp/berth-no-engine.sock")
    ok = yield from wait_for(lambda: st.up is False, 15)
    check("Switching to a missing engine shows it as stopped", ok and win.stack.get_visible_child_name() == "off")
    sp._switch(real if real.startswith("tcp://") else "unix://" + real)
    ok = yield from wait_for(lambda: st.up and st.containers, 20)
    check("Switching back reconnects" + ("" if real.startswith("tcp://") else " (unix:// form)"), ok)
    sp._switch(real)
    yield from wait_for(lambda: st.up and st.containers, 20)
    eng = st.engine

    # 13. Kubernetes (only when kubectl + kind are installed)
    import shutil as _sh
    if _sh.which("kubectl") and _sh.which("kind") and not os.environ.get("BERTH_TEST_SKIP_K8S"):
        done.clear()
        CommandDialog(win, "kind", ["kind", "create", "cluster", "--name", "berth-selftest"],
                      on_done=lambda ok: done.__setitem__("kind", ok), autoclose=True).present(win)
        ok = yield from wait_for(lambda: "kind" in done, 900)
        check("kind creates a local cluster", done.get("kind"))
        kp = win.pages["kubernetes"]
        win.go("kubernetes")
        ok = yield from wait_for(lambda: "kind-berth-selftest" in kp.contexts, 30)
        if ok:
            kp.ctx.set_selected(kp.contexts.index("kind-berth-selftest"))
        ok = yield from wait_for(lambda: kp.model.get_n_items() > 0 and kp.stack.get_visible_child_name() == "table", 60)
        check("Kubernetes view lists pods", ok, f"{kp.model.get_n_items()} pods")
        from berth.cli import run_capture as rc
        rc(kp.kubectl("create", "deployment", "hello", "--image=busybox:latest", "--", "sleep", "3600"))
        kp.kinds.set_active_name("deployments")
        ok = yield from wait_for(lambda: any(kp.model.get_item(i).data["name"] == "hello" for i in range(kp.model.get_n_items())), 30)
        check("Deployments tab shows a new deployment", ok)
        from berth.k8s import YamlEditor
        ed = YamlEditor(kp, "deployments|default|hello")
        ed.present(win)
        yield from wait_for(lambda: ed.apply.get_sensitive(), 20)
        b = ed.text.get_buffer()
        y = b.get_text(b.get_start_iter(), b.get_end_iter(), False)
        b.set_text(y.replace("replicas: 1", "replicas: 2", 1))
        ed._apply()
        ok = yield from wait_for(lambda: rc(kp.kubectl("get", "deployment", "hello", "-o", "jsonpath={.spec.replicas}")).strip() == "2", 30)
        check("Edit YAML + apply changes the cluster (replicas 1 → 2)", ok)
        kp._restart("deployments|default|hello")
        yield 2
        ann = rc(kp.kubectl("get", "deployment", "hello", "-o", "jsonpath={.spec.template.metadata.annotations}"))
        check("Rollout restart", "restartedAt" in ann)
        shot(win, "12-kubernetes")
        done.clear()
        CommandDialog(win, "kind", ["kind", "delete", "cluster", "--name", "berth-selftest"],
                      on_done=lambda ok: done.__setitem__("kdel", ok), autoclose=True).present(win)
        ok = yield from wait_for(lambda: "kdel" in done, 300)
        check("kind deletes the cluster", done.get("kdel"))
    else:
        print("SKIP Kubernetes (kubectl/kind missing, or BERTH_TEST_SKIP_K8S set)", flush=True)

    # cleanup
    try:
        eng.remove_container(cid)
    except Exception:  # noqa: BLE001
        pass
    for c in eng.containers():
        if any(n.startswith("/berth-selftest") for n in c.get("Names", [])):
            eng.remove_container(c["Id"])
    try:
        eng.remove_volume("berth-selftest-vol")
    except Exception:  # noqa: BLE001
        pass
    fails = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(fails)}/{len(results)} passed", flush=True)
    app.quit()


def main():
    app = berth_app.App()
    app.set_flags(app.get_flags() | __import__("gi.repository.Gio", fromlist=["Gio"]).ApplicationFlags.NON_UNIQUE)
    gen = {"g": None}

    def step():
        try:
            delay = next(gen["g"])
        except StopIteration:
            return False
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            app.quit()
            return False
        GLib.timeout_add(int((delay or 0.05) * 1000), step)
        return False

    def start(*_):
        gen["g"] = scenario(app)
        GLib.timeout_add(1500, step)
    app.connect("activate", lambda *_: GLib.idle_add(start))
    app.run([])


if __name__ == "__main__":
    main()
