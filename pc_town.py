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
            snap["proc_count"] = psutil.process_count()
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
