"""PC Town — ดูการทำงานของเครื่องเป็นเมือง pixel art (เปิดผ่าน shortcut, ปิดเองเมื่อปิดแท็บ)"""
import json, os, socket, sys, threading, time, webbrowser

try:
    import psutil
except ImportError:
    psutil = None

LOCK_PORT = 8766
HTTP_PORT_START = 8765
OWN_PIDS = {os.getpid()}
SYSTEM_DENYLIST = {
    "", "system", "registry", "memory compression", "svchost.exe",
    "csrss.exe", "wininit.exe", "services.exe", "lsass.exe",
    "conhost.exe", "winlogon.exe", "dwm.exe", "taskhostw.exe",
    "ntoskrnl.exe", "smss.exe", "fontdrvhost.exe", "audiodg.exe",
}
TOP_N = 6


def should_include(name, pid, ppid, own_pids=OWN_PIDS):
    if pid in own_pids:
        return False
    if not name:
        return False
    if name.lower() in SYSTEM_DENYLIST:
        return False
    if ppid == 4:
        return False
    return True


class Delta:
    """คำนวณ rate ต่อวินาทีจาก counter สะสม (reset-safe)"""
    def __init__(self):
        self.prev = None
        self.at = 0.0
        self.rate = 0.0

    def update(self, value, now):
        if self.prev is not None and now > self.at:
            self.rate = max(0.0, (value - self.prev) / (now - self.at))
        self.prev, self.at = value, now
        return self.rate


class Sampler(threading.Thread):
    daemon = True

    def __init__(self, interval=1.0):
        super().__init__(name="sampler")
        self.interval = interval
        self._lock = threading.Lock()
        self._snap = {}
        self._disk_r = Delta(); self._disk_w = Delta()
        self._net_rx = Delta(); self._net_tx = Delta()

    def top_procs(self):
        out = []
        for p in psutil.process_iter(["pid", "name", "ppid", "cpu_percent", "memory_info"]):
            try:
                i = p.info
                if not should_include(i["name"], i["pid"], i["ppid"]):
                    continue
                out.append(i)
            except Exception:
                continue
        out.sort(key=lambda d: d["cpu_percent"], reverse=True)
        return [{"name": d["name"], "cpu": round(d["cpu_percent"], 1),
                 "memMB": round(d["memory_info"].rss / 2**20, 1) if d.get("memory_info") else 0.0}
                for d in out[:TOP_N]]

    def run(self):
        for p in psutil.process_iter():          # bootstrap per-process cpu_percent
            try:
                p.cpu_percent(None)
            except Exception:
                pass
        psutil.cpu_percent()                     # bootstrap overall
        while True:
            time.sleep(self.interval)
            now = time.time()
            snap = {"cpu": psutil.cpu_percent(),
                    "ram": psutil.virtual_memory().percent}
            try:
                snap["disk_full"] = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\").percent
            except Exception:
                snap["disk_full"] = 0
            dio = psutil.disk_io_counters()
            snap["disk_r"] = self._disk_r.update(dio.read_bytes, now) if dio else 0.0
            snap["disk_w"] = self._disk_w.update(dio.write_bytes, now) if dio else 0.0
            nio = psutil.net_io_counters()
            snap["net_rx"] = self._net_rx.update(nio.bytes_recv, now) if nio else 0.0
            snap["net_tx"] = self._net_tx.update(nio.bytes_sent, now) if nio else 0.0
            b = psutil.sensors_battery()
            snap["battery"] = None if b is None else {"pct": round(b.percent), "plugged": bool(b.power_plugged)}
            snap["procs"] = self.top_procs()
            snap["proc_count"] = len(psutil.pids())
            snap["uptime"] = now - psutil.boot_time()
            snap["temp"] = None
            temps = getattr(psutil, "sensors_temperatures", None)
            if temps:
                try:
                    tmap = temps()
                    first = next(iter(tmap.values()), None)
                    if first:
                        snap["temp"] = round(first[0].current, 1)
                except Exception:
                    pass
            with self._lock:
                self._snap = snap

    def snapshot(self):
        with self._lock:
            return dict(self._snap)


SAMPLER = None
STATE = {"last_ping": 0.0, "first_ping": False}
INDEX_HTML = "PC Town loading"  # Task 3 แทนที่ด้วยหน้าเว็บจริง
HTTP_PORT = HTTP_PORT_START


def pick_http_port():
    for port in range(HTTP_PORT_START, HTTP_PORT_START + 11):
        if port == LOCK_PORT:
            continue                                   # 8766 กันไว้สำหรับ lock เท่านั้น
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            return port
        except OSError:
            pass
        finally:
            s.close()
    raise OSError("หา port ว่างไม่ได้ (8765-8775)")


class LockAnswer(threading.Thread):
    daemon = True

    def __init__(self, sock):
        super().__init__(name="lock")
        self.sock = sock

    def run(self):
        while True:
            try:
                c, _ = self.sock.accept()
                c.sendall(f"{HTTP_PORT}\n".encode())
                c.close()
            except OSError:
                return


def acquire_lock():
    """คืน URL ของ instance เดิมถ้ามี (ไม่ต้องรันซ้ำ) หรือ None ถ้าเราได้เป็น instance แรก"""
    try:
        s = socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=0.5)
    except OSError:
        s = None
    if s:
        try:
            port = int(s.recv(16).decode().strip())
        except Exception:
            port = HTTP_PORT_START
        s.close()
        return f"http://127.0.0.1:{port}/"
    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    ls.bind(("127.0.0.1", LOCK_PORT))
    ls.listen(4)
    LockAnswer(ls).start()
    return None


class Handler(__import__("http.server", fromlist=["BaseHTTPRequestHandler"]).BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, INDEX_HTML, "text/html")
        elif self.path == "/api/status":
            self._send(200, json.dumps(SAMPLER.snapshot() if SAMPLER else {}))
        elif self.path == "/api/ping":
            STATE["last_ping"] = time.time()
            STATE["first_ping"] = True
            self._send(200, '{"ok":true}')
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if self.path == "/api/shutdown":
            self._send(200, '{"ok":true}')
            threading.Timer(0.3, lambda: os._exit(0)).start()
        else:
            self._send(404, "{}")


def watchdog():
    while True:
        time.sleep(1)
        if STATE["first_ping"] and time.time() - STATE["last_ping"] > 5:
            os._exit(0)


def open_url(url):
    try:
        os.startfile(url)  # เปิด default browser บน Windows
    except Exception:
        try:
            webbrowser.open(url)
        except Exception:
            d = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "PC_Town")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "last_url.txt"), "w", encoding="utf-8") as f:
                f.write(url)


def main():
    global SAMPLER, HTTP_PORT
    if psutil is None:
        try:
            import tkinter
            from tkinter import messagebox
            tkinter.Tk().withdraw()
            messagebox.showerror("PC Town", "ไม่พบ psutil\nรัน: python -m pip install psutil")
        except Exception:
            pass
        sys.exit(1)
    existing = acquire_lock()
    if existing:
        open_url(existing)
        return
    HTTP_PORT = pick_http_port()
    SAMPLER = Sampler()
    SAMPLER.start()
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    threading.Thread(target=watchdog, daemon=True, name="watchdog").start()
    url = f"http://127.0.0.1:{HTTP_PORT}/"
    if os.environ.get("PC_TOWN_NO_BROWSER") != "1":
        threading.Timer(0.5, open_url, [url]).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
