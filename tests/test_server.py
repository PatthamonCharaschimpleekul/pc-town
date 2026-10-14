import json, os, subprocess, sys, time, urllib.request
from pathlib import Path

APP = str(Path(__file__).resolve().parents[1] / "pc_town.py")
ENV = dict(os.environ, PC_TOWN_NO_BROWSER="1")


def _spawn():
    return subprocess.Popen([sys.executable, APP], env=ENV)


def _get(path, port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
        return r.status, r.read()


def _wait_port(timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s, body = _get("/api/status", 8765)
            if s == 200 and json.loads(body).get("cpu") is not None:
                return 8765, json.loads(body)
        except Exception:
            pass
        time.sleep(0.3)
    raise AssertionError("server did not come up")


def test_status_endpoint():
    p = _spawn()
    try:
        port, snap = _wait_port()
        for k in ("cpu", "ram", "disk_full", "disk_r", "disk_w", "net_rx", "net_tx",
                  "battery", "procs", "proc_count", "uptime", "temp"):
            assert k in snap, k
        assert isinstance(snap["procs"], list)
    finally:
        p.terminate()


def test_ping_and_shutdown():
    p = _spawn()
    try:
        port, _ = _wait_port()
        s, body = _get("/api/ping", port)
        assert s == 200
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", method="POST")
        urllib.request.urlopen(req, timeout=3).read()
        assert p.wait(timeout=5) is not None  # process ตายเอง
    finally:
        p.terminate()


def test_single_instance_lock():
    p1 = _spawn()
    try:
        port, _ = _wait_port()
        p2 = subprocess.run([sys.executable, APP], env=ENV, timeout=15)
        assert p2.returncode == 0            # instance สอง exit ทันที ไม่รัน server ซ้ำ
    finally:
        p1.terminate()


def test_hide_and_unhide_via_api():
    p = _spawn()
    try:
        _, snap = _wait_port()
        name = snap["procs"][0]["name"]
        req = lambda body: urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:8765/api/hide", json.dumps(body).encode(),
            {"Content-Type": "application/json"}), timeout=3).read().decode()
        assert '"ok"' in req({"name": name, "on": True})
        time.sleep(1.5)
        snap2 = json.loads(_get("/api/status", 8765)[1])
        assert snap2["hidden"] == [name.lower()]
        assert all(pr["name"] != name for pr in snap2["procs"])
        req({"name": name, "on": False})
    finally:
        p.terminate()


def test_kill_endpoint_rejects_stale_pid():
    p = _spawn()
    try:
        _wait_port()
        body = json.dumps({"name": "explorer.exe", "pid": 999999}).encode()
        r = urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:8765/api/kill", body,
            {"Content-Type": "application/json"}), timeout=3).read()
        assert json.loads(r)["error"] == "gone"   # ไม่ได้ kill อะไรจริง
    finally:
        p.terminate()
