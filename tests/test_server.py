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
