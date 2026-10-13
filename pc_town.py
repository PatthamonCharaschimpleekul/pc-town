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
    "", "system", "idle", "system idle process", "registry", "memory compression", "svchost.exe",
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
INDEX_HTML = r"""<!doctype html>
<html lang="th"><head><meta charset="utf-8"><title>PC Town</title>
<style>
html,body{margin:0;height:100%;overflow:hidden;background:#8ecfe8;font-family:'Segoe UI',monospace}
#cv{display:block;width:100vw;height:100vh;image-rendering:pixelated}
#tip{position:fixed;display:none;background:#fffbe7;border:3px solid #4a3220;border-radius:4px;
     padding:6px 10px;font-size:13px;pointer-events:none;z-index:9;box-shadow:3px 3px 0 #4a322055}
#offBtn{position:fixed;top:10px;right:10px;z-index:9;background:#ff8f8f;border:3px solid #4a3220;
        border-radius:6px;font-size:14px;padding:4px 12px;cursor:pointer;font-weight:bold}
#hud{position:fixed;left:0;right:0;bottom:0;display:flex;gap:14px;justify-content:center;
     align-items:flex-end;padding:8px 12px;background:#4a3220d9;color:#fff;font-size:12px;z-index:8}
.m{width:120px}.m b{display:block;font-size:11px;letter-spacing:.5px}
.bar{height:12px;background:#00000055;border:2px solid #fff;border-radius:3px;overflow:hidden}
.bar i{display:block;height:100%;width:0%;background:#7ee081;transition:width .4s}
.bar i.warn{background:#ffd35c}.bar i.hot{background:#ff7a5c}
#small{position:fixed;right:12px;bottom:78px;color:#fff;font-size:11px;text-align:right;z-index:8;text-shadow:1px 1px 0 #0006}
#dead{position:fixed;inset:0;display:none;place-items:center;font-size:22px;color:#4a3220;background:#8ecfe8}
</style></head><body>
<canvas id="cv"></canvas>
<div id="tip"></div>
<div id="hud"></div>
<div id="small"></div>
<button id="offBtn">ปิดแอป</button>
<div id="dead">ปิดเรียบร้อย — ปิดแท็บนี้ได้เลยค่ะ 🏙️</div>
<script>
"use strict";
const cv = document.getElementById('cv'), ctx = cv.getContext('2d');
const WORLD = {W: 960, H: 460};
let scale = 1, offX = 0, offY = 0, dpr = 1;

function resize(){
  dpr = window.devicePixelRatio || 1;              // คมทุก Windows scale 125/150%
  cv.width  = Math.round(innerWidth * dpr);
  cv.height = Math.round(innerHeight * dpr);
  scale = Math.min(cv.width / WORLD.W, cv.height / WORLD.H);
  offX = (cv.width - WORLD.W * scale) / 2;
  offY = (cv.height - WORLD.H * scale) / 2;
  ctx.imageSmoothingEnabled = false;               // pixel art ไม่เบลอ
}
addEventListener('resize', resize);
resize();

function px(x, y, w, h, color){
  ctx.fillStyle = color;
  ctx.fillRect(Math.round(x), Math.round(y), w, h);
}

/* ---------- ฉากหลัง ---------- */
const ROAD_Y = 300, ROAD_H = 70, LANE_Y = ROAD_Y + ROAD_H/2;
const HOUSES = [70, 200, 330, 460, 590, 700];      // slot x (ซ้ายบนของบ้าน)
const HOUSE_COLORS = ['#ffb3ba','#b5ead7','#c7ceea','#ffe5b4','#d4a5ff','#a0e7e3'];
let clouds = [{x:100,y:60,s:1},{x:420,y:90,s:.7},{x:760,y:50,s:.9}];

function drawSky(t){
  px(0,0,WORLD.W,WORLD.H,'#bfe8f7');               // ฟ้าอ่อนสดใส
  px(0,0,WORLD.W,180,'#cdeefb');
  for(const c of clouds){
    c.x += 0.08 * c.s;
    if(c.x > WORLD.W + 60) c.x = -80;
    ctx.fillStyle = '#ffffff';
    for(const [ox,oy,w,h] of [[0,0,46,14],[10,-8,26,12],[34,-3,18,10]]){
      ctx.fillRect(c.x+ox, c.y+oy, w*c.s+8, h*c.s);
    }
  }
}
function drawGround(){
  px(0,240,WORLD.W,60,'#9ede8a');                  // สนามหญ้าหลังถนน
  px(0,ROAD_Y,WORLD.W,ROAD_H,'#6b6b7b');           // ถนน
  px(0,ROAD_Y,WORLD.W,4,'#8a8a9a');
  px(0,ROAD_Y+ROAD_H-4,WORLD.W,4,'#55555f');
  ctx.fillStyle = '#f4f1de';                        // เส้นประกลางถนน
  for(let x=0;x<WORLD.W;x+=64) ctx.fillRect(x,LANE_Y-2,32,4);
  px(0,ROAD_Y+ROAD_H,WORLD.W,WORLD.H,'#7ccb6f');    // หญ้าหน้าถนน
}
function drawHouse(x, color, label){
  px(x,200,90,44,'#fffaf3');
  px(x+4,204,82,36,color);                          // ผนังโทนพาสเทล
  px(x-4,186,98,18,'#e46b6b'); px(x+6,176,78,12,'#e46b6b'); px(x+16,168,58,10,'#d95c5c'); // หลังคา
  px(x+34,216,22,28,'#5d4635');                     // ประตู
  px(x+12,212,14,14,'#bfe8f7'); px(x+64,212,14,14,'#bfe8f7'); // หน้าต่าง
  if(label){
    ctx.fillStyle='#4a3220'; ctx.font='10px monospace'; ctx.textAlign='center';
    ctx.fillText(label, x+45, 258);
  }
}
function drawFactory(){
  px(830,170,110,74,'#9a93a8'); px(838,178,26,24,'#565163'); px(872,178,26,24,'#565163');
  px(906,178,26,24,'#565163'); px(844,140,18,30,'#6e6780');   // ปล่อง
  ctx.fillStyle='#4a3220'; ctx.font='10px monospace'; ctx.textAlign='center';
  ctx.fillText('Factory', 885, 258);
}
function drawTree(x){
  px(x+10,214,10,30,'#8a5a35');
  ctx.fillStyle='#4fa855';
  ctx.beginPath(); ctx.arc(x+15,204,20,0,7); ctx.fill();
  ctx.fillStyle='#66c16b';
  ctx.beginPath(); ctx.arc(x+9,196,12,0,7); ctx.arc(x+22,198,11,0,7); ctx.fill();
}

function drawScene(t){
  ctx.setTransform(1,0,0,1,0,0);
  ctx.fillStyle='#8ecfe8'; ctx.fillRect(0,0,cv.width,cv.height);
  ctx.setTransform(scale,0,0,scale,offX,offY);
  drawSky(t);
  drawGround();
  HOUSES.forEach((x,i)=>drawHouse(x, HOUSE_COLORS[i], '')); // label = ชื่อ process (Task 4)
  drawTree(160); drawTree(540); drawTree(780);
  drawFactory();
}

/* ---------- sprites (pixel array: h=hair s=skin e=eye b=shirt p=pants f=foot) ---------- */
const U = 4; // 1 pixel unit = 4 world px
const FRAMES = {
  stand: [
    "..hhhh..", ".hhhhhh.", ".hssssh.", ".sesese.", ".ssssss.",
    "..bbbb..", "..bbbb..", "..bbbb..",
    "..pppp..", "..p..p..", "..f..f..",
  ],
  walkA: [
    "..hhhh..", ".hhhhhh.", ".hssssh.", ".sesese.", ".ssssss.",
    "..bbbb..", ".bbbbbb.", "..bbbb..",
    "..ppp...", ".p...p..", "f.....f.",
  ],
  walkB: [
    "..hhhh..", ".hhhhhh.", ".hssssh.", ".sesese.", ".ssssss.",
    "..bbbb..", ".bbbbbb.", "..bbbb..",
    "...ppp..", "..p...p.", ".f.....f",
  ],
  run: [
    ".hhhhh..", "shhhhh.s", "ssssssss", ".esese..", ".ssss...",
    "..bbbb..", ".bbbbbb.", "bbbbbb..",
    "..pppp..", "p......p", "ff....ff",
  ],
};
const SHIRTS = ['#ff8f8f','#8fd0ff','#b3e88f','#ffd98f','#d0a3ff','#8ffff2'];
const HAIRS  = ['#5d4635','#8a5a35','#3b2a1a','#c96f3b','#4a6fa5','#7a4a8a'];
function drawSprite(frameName, x, y, i, dir){
  const rows = FRAMES[frameName];
  const pal = {h:HAIRS[i], s:'#ffdcc3', e:'#3b2a1a', b:SHIRTS[i], p:'#4a5568', f:'#3b2a1a'};
  for(let r=0;r<rows.length;r++)
    for(let c=0;c<8;c++){
      const ch = rows[r][c];
      if(ch==='.'||ch===' ') continue;
      const cx = dir>0 ? c : 7-c;
      px(x + cx*U, y + r*U, U, U, pal[ch]);
    }
}

/* ---------- characters ---------- */
const SLOT_HOME = HOUSES.map(x => x + 45);  // หน้าบ้านแต่ละหลัง
class Char {
  constructor(slot){
    this.slot = slot;
    this.x = SLOT_HOME[slot]; this.target = this.x;
    this.speed = 0; this.targetSpeed = 0;
    this.dir = 1; this.frameT = 0; this.idle = false; this.zT = 0;
    this.name = ''; this.pendingName = ''; this.nameAlpha = 0;
    this.cpu = 0; this.memMB = 0; this.size = 1;
  }
  setProc(p){
    const name = p ? p.name : '';
    if(name !== this.pendingName && name !== this.name){
      if(this.name && name) this.pendingName = name;     // ค่อยๆ fade
      else { this.name = name; this.pendingName = ''; this.nameAlpha = 1; }
    }
    if(!this.name && name){ this.name = name; this.nameAlpha = 1; }
    this.cpu = p ? p.cpu : 0;
    this.memMB = p ? p.memMB : 0;
    this.size = this.memMB > 800 ? 1.3 : this.memMB > 300 ? 1.15 : 1;  // RAM เยอะ→ใหญ่ขึ้น
    const c = this.cpu;
    this.idle = c < 1;
    this.targetSpeed = c > 20 ? 150 : c > 5 ? 70 : c > 1 ? 30 : 0;     // world px/s
    if(this.idle) this.target = this.x;
  }
  update(dt){
    this.speed += (this.targetSpeed - this.speed) * Math.min(1, dt * 2.5); // exponential smoothing
    if(!this.idle && this.x === this.target && this.speed < 1)
      this.pickTarget();
    if(!this.idle && this.speed > 1){
      const d = this.target - this.x;
      this.dir = d >= 0 ? 1 : -1;
      const step = Math.min(Math.abs(d), this.speed * dt);
      this.x += Math.sign(d) * step;
      if(Math.abs(this.target - this.x) < 0.5) this.x = this.target;
    }
    this.frameT += dt * (this.speed / 40 + 0.5);
    if(this.pendingName){
      this.nameAlpha -= dt * 2;
      if(this.nameAlpha <= 0){ this.name = this.pendingName; this.pendingName = ''; this.nameAlpha = 0; }
    } else if(this.name && this.nameAlpha < 1) this.nameAlpha = Math.min(1, this.nameAlpha + dt * 2);
    if(this.idle){ this.zT += dt; } else this.zT = 0;
  }
  pickTarget(){
    const home = SLOT_HOME[this.slot];
    if(Math.random() < 0.35){ // กลับบ้าน
      this.target = home;
    } else {
      this.target = Math.max(20, Math.min(WORLD.W - 30,
                    home + (Math.random() * 260 - 130)));
    }
  }
  draw(t){
    const feetY = ROAD_Y + 58;
    let frameName = 'stand';
    if(this.speed > 110) frameName = 'run';
    else if(this.speed > 2) frameName = (Math.floor(this.frameT * 2) % 2) ? 'walkA' : 'walkB';
    ctx.save();
    if(this.size > 1){ const cx = this.x; ctx.translate(cx, feetY); ctx.scale(this.size, this.size); ctx.translate(-cx, -feetY); }
    drawSprite(frameName, this.x - 16, feetY - FRAMES.stand.length * U, this.slot, this.dir);
    ctx.restore();
    if(this.idle && this.zT % 2.4 < 1.2){                    // ฟอง z ตอนหลับ
      ctx.fillStyle = '#ffffffcc'; ctx.font = '12px monospace'; ctx.textAlign = 'left';
      ctx.fillText('z', this.x + 20, feetY - 52 - (this.zT % 1.2) * 8);
    }
    if(this.name){
      ctx.globalAlpha = Math.max(0, Math.min(1, this.nameAlpha));
      ctx.fillStyle = '#4a3220'; ctx.font = 'bold 10px monospace'; ctx.textAlign = 'center';
      const label = this.name.replace(/\.exe$/i, '').slice(0, 14);
      ctx.fillText(label, this.x, feetY + 16);
      ctx.globalAlpha = 1;
    }
  }
}
const chars = SLOT_HOME.map((_, i) => new Char(i));

/* ---------- polling ---------- */
let DATA = {procs: []};
async function poll(){
  try{
    const r = await fetch('/api/status'); DATA = await r.json();
    fetch('/api/ping').catch(()=>{});
  }catch(e){}
  setTimeout(poll, 1000);
}
poll();

function applyData(){
  (DATA.procs || []).forEach((p, i) => chars[i] && chars[i].setProc(p));
  for(let i = (DATA.procs||[]).length; i < chars.length; i++) chars[i].setProc(null);
}

/* ---------- tooltip (hover) ---------- */
const tip = document.getElementById('tip');
addEventListener('mousemove', e => {
  const wx = (e.clientX * dpr - offX) / scale, wy = (e.clientY * dpr - offY) / scale;
  const hit = chars.find(c => c.name && Math.abs(wx - c.x) < 22*c.size && wy > ROAD_Y + 8 && wy < ROAD_Y + 80);
  if(hit){
    tip.style.display = 'block';
    tip.style.left = (e.clientX + 14) + 'px';
    tip.style.top  = (e.clientY - 10) + 'px';
    tip.innerHTML = `<b>${hit.name}</b><br>CPU ${hit.cpu}% · RAM ${hit.memMB} MB`;
  } else tip.style.display = 'none';
});

/* Task 5 จะแทนที่ stub สองฟังก์ชันนี้ */
function updateEnv(dt){}
function drawHud(){}

let last = 0;
function frame(ms){
  const dt = Math.min(0.05, (ms - last)/1000); last = ms;
  drawScene(ms/1000);
  applyData();
  for(const c of chars){ c.update(dt); c.draw(ms/1000); }
  updateEnv(dt); drawHud();   // Task 5
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

document.getElementById('offBtn').onclick = async () => {
  try{ await fetch('/api/shutdown',{method:'POST'}); }catch(e){}
  document.querySelectorAll('body > *:not(#dead)').forEach(el=>el.style.display='none');
  document.getElementById('dead').style.display='grid';
};
</script>
</body></html>"""
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
