"""PC Town — ดูการทำงานของเครื่องเป็นเมือง pixel art (เปิดผ่าน shortcut, ปิดเองเมื่อปิดแท็บ)"""
import json, os, socket, sys, threading, time, webbrowser
from collections import deque

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


def select_procs(items, pinned, hidden, top_n=TOP_N):
    """items: list[dict{name,pid,cpu,memMB}] — pinned เข้า slot ก่อน (เรียง cpu),
    ตัด hidden, เติมด้วย cpu เรียง desc, ไม่เกิน top_n"""
    hs = {h.lower() for h in hidden}
    ps = {p.lower() for p in pinned}
    items = [i for i in items if i["name"].lower() not in hs]
    first = sorted([i for i in items if i["name"].lower() in ps], key=lambda d: -d["cpu"])
    rest = sorted([i for i in items if i["name"].lower() not in ps], key=lambda d: -d["cpu"])
    return (first + rest)[:top_n]


def verify_kill(procs, name, pid):
    """ตรวจสองชั้นก่อน kill: pid ต้องปรากฏใน procs ล่าสุด และ name ตรงกัน"""
    for p in procs:
        if p["pid"] == pid:
            return "ok" if p["name"].lower() == str(name).lower() else "mismatch"
    return "gone"


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
        self.pinned = set()
        self.hidden = set()
        self.history = {}        # name.lower() -> deque(maxlen=30) of cpu
        self.last_all = []       # items ของ tick ล่าสุด (ใช้ตรวจ pin "ยังมีชีวิต")

    def top_procs(self):
        items = []
        for p in psutil.process_iter(["pid", "name", "ppid", "cpu_percent", "memory_info"]):
            try:
                i = p.info
                if not should_include(i["name"], i["pid"], i["ppid"]):
                    continue
                items.append({"name": i["name"], "pid": i["pid"],
                              "cpu": round(i["cpu_percent"], 1),
                              "memMB": round(i["memory_info"].rss / 2**20, 1) if i.get("memory_info") else 0.0})
            except Exception:
                continue
        self.last_all = items
        chosen = select_procs(items, self.pinned, self.hidden)
        alive = {d["name"].lower() for d in chosen}
        for d in chosen:
            dq = self.history.setdefault(d["name"].lower(), deque(maxlen=30))
            dq.append(d["cpu"])
            d["hist"] = list(dq)
        if len(self.history) > 60:   # memory guard
            self.history = {k: v for k, v in self.history.items() if k in alive}
        return chosen

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
            snap["pinned"] = sorted(self.pinned)
            snap["hidden"] = sorted(self.hidden)
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
html,body{margin:0;height:100%;overflow:hidden;background:#86c76c;font-family:'Segoe UI',monospace}
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
#dead{position:fixed;inset:0;display:none;place-items:center;font-size:22px;color:#4a3220;background:#c9e8b8}
#menu{position:fixed;z-index:10;width:230px;background:#fffbe7;border:3px solid #4a3220;border-radius:8px;
      padding:10px;font-size:13px;color:#4a3220;box-shadow:4px 4px 0 #4a322044}
#menu h3{margin:0 0 4px;font-size:14px} #menu .row{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
#menu button,#hiddenBtn button{border:2px solid #4a3220;border-radius:5px;background:#ffe9a8;padding:3px 7px;cursor:pointer;font-size:12px}
#menu button.on{background:#7ee081} #menu button.danger{background:#ff8f8f}
#menu canvas{display:block;margin-top:6px;background:#f6efe0;border:2px solid #4a322066;border-radius:4px}
#hiddenBtn{position:fixed;left:10px;bottom:78px;z-index:9;background:#c7ceea;border:3px solid #4a3220;
           border-radius:6px;padding:4px 10px;cursor:pointer;font-weight:bold}
#toast{position:fixed;left:50%;bottom:110px;transform:translateX(-50%);background:#4a3220ee;color:#fff;
       padding:6px 14px;border-radius:6px;font-size:13px;z-index:12}
</style></head><body>
<canvas id="cv"></canvas>
<div id="tip"></div>
<div id="hud"></div>
<div id="small"></div>
<button id="offBtn">ปิดแอป</button>
<div id="menu" hidden></div>
<button id="hiddenBtn" hidden></button>
<div id="toast" hidden></div>
<div id="dead">ปิดเรียบร้อย — ปิดแท็บนี้ได้เลยค่ะ 🏙️</div>
<script>
"use strict";
const cv = document.getElementById('cv'), ctx = cv.getContext('2d');
const W = 960, H = 540, TILE = 32, U = 3;          // U = 1 หน่วย sprite
let scale = 1, offX = 0, offY = 0, dpr = 1;

function resize(){
  dpr = window.devicePixelRatio || 1;              // คมทุก Windows scale 125/150%
  cv.width  = Math.round(innerWidth * dpr);
  cv.height = Math.round(innerHeight * dpr);
  scale = Math.min(cv.width / W, cv.height / H);
  offX = (cv.width - W * scale) / 2;
  offY = (cv.height - H * scale) / 2;
  ctx.imageSmoothingEnabled = false;
}
addEventListener('resize', resize);
resize();

function px(x, y, w, h, color){
  ctx.fillStyle = color;
  ctx.fillRect(Math.round(x), Math.round(y), w, h);
}
function rr(x,y,w,h,r,fill,stroke){
  ctx.beginPath(); ctx.roundRect(x,y,w,h,r);
  if(fill){ctx.fillStyle=fill;ctx.fill();}
  if(stroke){ctx.strokeStyle=stroke;ctx.lineWidth=2;ctx.stroke();}
}

/* ================== แผนที่เมือง (top-down RPG) ================== */
const HOUSE_X = [72, 200, 360, 520, 680, 816];      // แกนถนนขึ้นบ้านแต่ละหลัง
const MAIN_Y = 272, STREET_Y = 392, PLAZA_Y = 344;
const PATHS = [
  [0, MAIN_Y-8, W, 16],                              // ถนนสายหลักบน
  ...HOUSE_X.map(x => [x-8, 156, 16, MAIN_Y-148]),   // ซอยขึ้นบ้าน
  [352, MAIN_Y, 16, STREET_Y-MAIN_Y+8],              // แยกกลาง (บ้าน3 → จัตุรัส→ถนนล่าง)
  [360, PLAZA_Y-8, 112, 16],                         // ทางเข้าน้ำพุ
  [464, PLAZA_Y, 16, STREET_Y-PLAZA_Y],
  [72, STREET_Y-8, 608, 16],                         // ถนนสายล่าง
  [672, MAIN_Y, 16, STREET_Y-MAIN_Y+8],              // แยกขวา
  [816, MAIN_Y-8, 56, 16], [864, MAIN_Y, 16, 40],    // ทางเข้าโรงงาน
];
const WATER = [48, 432, 120, 72];
function inRect(x, y, r, [rx,ry,rw,rh]){ return x>=rx-r && x<=rx+rw+r && y>=ry-r && y<=ry+rh+r; }
const inPaths = (x,y,r=6) => PATHS.some(a => inRect(x,y,r,a));
const inWater = (x,y,r=0) => inRect(x,y,r,WATER);
const HOUSE_R = HOUSE_X.map(x => [x-48, 96, 96, 60]);   // ผนังบ้าน
const inHouse = (x,y,r=6) => HOUSE_R.concat([[856,96,96,60]]).some(a => inRect(x,y,r,a));

/* graph ให้ตัวละครเดิน */
const NODES = {
  m0:[8,MAIN_Y], m1:[72,MAIN_Y], m2:[200,MAIN_Y], m3:[360,MAIN_Y], m4:[520,MAIN_Y],
  m5:[680,MAIN_Y], m6:[816,MAIN_Y], m7:[952,MAIN_Y],
  h0:[72,160], h1:[200,160], h2:[360,160], h3:[520,160], h4:[680,160], h5:[816,160],
  j1:[360,PLAZA_Y], p:[472,PLAZA_Y], f1:[472,STREET_Y], s1:[360,STREET_Y],
  s2:[520,STREET_Y], e0:[72,STREET_Y], eR:[680,STREET_Y], fk:[872,MAIN_Y], fd:[872,MAIN_Y+28],
};
const EDGES = [
  ['m0','m1'],['m1','m2'],['m2','m3'],['m3','m4'],['m4','m5'],['m5','m6'],['m6','m7'],
  ['h0','m1'],['h1','m2'],['h2','m3'],['h3','m4'],['h4','m5'],['h5','m6'],
  ['m3','j1'],['j1','p'],['p','f1'],['j1','s1'],
  ['e0','s1'],['s1','f1'],['f1','s2'],['s2','eR'],['eR','m5'],['m6','fk'],['fk','fd'],
];
const ADJ = {};
for(const [a,b] of EDGES){ (ADJ[a]=ADJ[a]||[]).push(b); (ADJ[b]=ADJ[b]||[]).push(a); }
const HOME = ['h0','h1','h2','h3','h4','h5'];

/* ================== sprites (chibi มุมมองบน) ================== */
const HEADS = {
  f: ["..hhhh..",".hhhhhh.",".hssssh.",".sesses."],
  s: ["..hhhh..",".hhhhhh.",".hssssh.",".ssesss."],
  b: ["..hhhh..",".hhhhhh.",".hhhhhh.",".hhhhhh."],
};
const TORSO = { stand:["..bbbb..","..bbbb..","..bbbb.."], a:["..bbbb..","s.bbbb.s","..bbbb.."], b:["..bbbb..",".bbbbbb.","..bbbb.."] };
const LEGS  = { stand:["..pppp..","..p..p..","..f..f.."], a:["..pppp..","..p..p..",".f....f."], b:["..pppp..",".pp.pp..","..f..f.."], run:["..pppp..",".p....p.","f......f"] };
const SHIRTS = ['#ff8f8f','#8fd0ff','#b3e88f','#ffd98f','#d0a3ff','#8ffff2','#cfd8dc'];
const HAIRS  = ['#5d4635','#8a5a35','#3b2a1a','#c96f3b','#4a6fa5','#7a4a8a','#8a8a8a'];
function drawFrame(head, torso, legs, x, y, i, flip, blink){
  const rows = HEADS[head].concat(TORSO[torso], LEGS[legs]);
  const pal = {h:HAIRS[i], s:'#ffdcc3', e:'#3b2a1a', b:SHIRTS[i], p:'#4a5568', f:'#3b2a1a'};
  for(let r=0;r<rows.length;r++)
    for(let c=0;c<8;c++){
      let ch = rows[r][c];
      if(ch==='.'||ch===' ') continue;
      if(blink && ch==='e') ch='s';                 // กะพริบตา = ปิดตา
      const cx = flip ? 7-c : c;
      px(x + cx*U, y + r*U, U, U, pal[ch]);
    }
}

/* ================== ฉากหลัง bake ครั้งเดียว ================== */
let bg;
function bakeBG(){
  bg = document.createElement('canvas'); bg.width=W; bg.height=H;
  const g = bg.getContext('2d');
  // หญ้าลายตาราง
  for(let ty=0;ty<H/TILE;ty++)for(let tx=0;tx<W/TILE;tx++){
    g.fillStyle = (tx+ty)%2 ? '#8fce74' : '#86c76c';
    g.fillRect(tx*TILE, ty*TILE, TILE, TILE);
  }
  // ถนนดิน + ขอบ + กรวด
  for(const [x,y,w,h] of PATHS){
    g.fillStyle='#c9a876'; g.fillRect(x-2,y-2,w+4,h+4);
    g.fillStyle='#e2c493'; g.fillRect(x,y,w,h);
    for(let s=0;s<w*h/900;s++){
      const rx=x+((x*7+y*13+s*37)%(w-4))+2, ry=y+((x*11+y*5+s*23)%(h-4))+2;
      g.fillStyle='#d4b07d'; g.fillRect(rx,ry,2,2);
    }
  }
  // สระน้ำ
  const [wx,wy,ww,wh]=WATER;
  g.fillStyle='#5aa3cf'; g.fillRect(wx-3,wy-3,ww+6,wh+6);
  g.fillStyle='#7ec8f2'; g.fillRect(wx,wy,ww,wh);
  g.fillStyle='#a8dcff'; g.fillRect(wx+10,wy+12,26,4); g.fillRect(wx+58,wy+40,30,4);
  // ต้นไม้ (ไม่ทับทาง/น้ำ/บ้าน)
  const trees=[[30,204],[140,196],[262,204],[610,204],[744,204],[904,206],[770,468],[238,464],[566,462],[934,452],[156,340],[300,336]];
  for(const [x,y] of trees){
    if(inPaths(x,y,14)||inWater(x,y,10)||inHouse(x,y,10)) continue;
    g.fillStyle='#3f3f3f44'; g.beginPath(); g.ellipse(x+16,y+30,14,5,0,0,7); g.fill();
    g.fillStyle='#8a5a35'; g.fillRect(x+13,y+18,6,14);
    g.fillStyle='#4fa855'; g.beginPath(); g.arc(x+16,y+12,14,0,7); g.fill();
    g.fillStyle='#66c16b'; g.beginPath(); g.arc(x+9,y+6,9,0,7); g.arc(x+22,y+8,8,0,7); g.fill();
    g.fillStyle='#8fd896'; g.fillRect(x+13,y+2,6,4);
  }
  // ดอกไม้ประปราย
  const fl=[[52,332],[122,352],[284,332],[436,432],[560,332],[620,448],[760,352],[880,352],[500,468],[384,332]];
  for(const [x,y] of fl){
    if(inPaths(x,y,10)||inWater(x,y,4)||inHouse(x,y,6)) continue;
    g.fillStyle=['#ff8f8f','#ffd98f','#d0a3ff','#8fd0ff'][(x+y)%4];
    g.fillRect(x,y,4,4); g.fillRect(x+6,y+3,4,4);
  }
  // บ้าน 6 หลัง + ของประกอบรอบตัวบ้าน
  HOUSE_X.forEach((cx,i)=>g_house(g, cx-48, i));
  // โรงงานขวาบน
  g.fillStyle='#7d7490'; g.fillRect(856,104,96,52);
  g.fillStyle='#9a93a8'; g.fillRect(860,108,88,44);
  g.fillStyle='#565163'; [868,900,930].forEach(x=>g.fillRect(x,116,18,20));
  g.fillStyle='#6e6780'; g.fillRect(920,72,12,32); g.fillStyle='#565163'; g.fillRect(918,68,16,6);
  g.fillStyle='#3f3f3f55'; g.fillRect(856,156,96,5);
  // ป้ายหน้าบ้าน (⌂) + mailbox
  HOUSE_X.forEach(cx=>{
    g.fillStyle='#f6efe0'; g.strokeStyle='#4a3220'; g.lineWidth=2;
    g.beginPath(); g.roundRect(cx-11,166,22,16,3); g.fill(); g.stroke();
    g.fillStyle='#4a3220'; g.font='11px monospace'; g.textAlign='center'; g.fillText('⌂',cx,178);
    g.fillStyle='#8a5a35'; g.fillRect(cx+22,150,3,12);            // ตู้จดหมาย
    g.fillStyle='#5d7fbf'; g.fillRect(cx+18,144,11,8);
  });
  // รั้วกระดานระหว่างบ้าน +พุ่มไม้
  for(let i=0;i<5;i++){
    const gx0 = HOUSE_X[i]+52, gx1 = HOUSE_X[i+1]-58;
    if(gx1-gx0 < 10) continue;
    g.fillStyle='#f6efe0';
    g.fillRect(gx0,138,gx1-gx0,3); g.fillRect(gx0,146,gx1-gx0,3);
    for(let x=gx0;x<gx1;x+=11) g.fillRect(x,132,4,20);
    g.fillStyle='#4fa855';
    g.beginPath(); g.arc(gx0-8,146,7,0,7); g.fill();
  }
  // เสาไฟตามถนน
  for(const [lx,ly] of [[140,250],[420,250],[640,250],[240,370],[600,370],[472,330]]){
    g.fillStyle='#5d5d6d'; g.fillRect(lx,ly,3,16);
    g.fillStyle='#8a8a9a'; g.fillRect(lx-3,ly-2,9,4);
    g.fillStyle='#ffe9a8'; g.fillRect(lx-1,ly+1,5,4);
  }
  // เสาไฟฟ้า + สายไฟ ขอบบน
  for(const px0 of [60,480,900]){ g.fillStyle='#7a5a3a'; g.fillRect(px0,10,3,34); g.fillRect(px0-5,12,13,3); }
  g.fillStyle='rgba(60,60,60,0.45)';
  g.fillRect(60,16,840,1); g.fillRect(60,21,840,1);
  // ทางม้าลาย (จุดข้าม 2 แห่งบนถนนใหญ่ + 1 บนถนนล่าง)
  g.fillStyle='#f4f1de';
  for(const cx0 of [252,612]) for(let k=0;k<4;k++) g.fillRect(cx0+k*8, MAIN_Y-8, 5, 16);
  for(let k=0;k<3;k++) g.fillRect(404+k*8, STREET_Y-8, 5, 16);
  // ป้ายบอกทางไปโรงงาน
  g.fillStyle='#8a5a35'; g.fillRect(838,246,3,18);
  g.fillStyle='#f6efe0'; g.fillRect(830,238,22,10);
  g.fillStyle='#4a3220'; g.font='8px monospace'; g.textAlign='center'; g.fillText('⚒→',841,246);
  // สระน้ำ: ริมหิน + ผักตบชวา +บัว
  g.fillStyle='#9aa3ad';
  for(const [sx,sy] of [[44,440],[44,468],[44,496],[168,436],[172,462],[168,492],[60,428],[96,428],[132,428]]) g.fillRect(sx,sy,6,6);
  g.fillStyle='#3f8f3f';
  for(const rx2 of [50,58,160,166]) for(let k=0;k<3;k++){ g.fillRect(rx2,418+k*4,2,12); }
  g.fillStyle='#57b06a';
  for(const [lx0,ly0] of [[70,455],[120,478],[96,438]]) { g.beginPath(); g.arc(lx0,ly0,7,0.4,6.1); g.fill(); }
  g.fillStyle='#ff9ecb'; g.fillRect(118,472,5,5);                 // ดอกบัว
  // ม้านั่งริมสระ + กระถางดอกไม้จัตุรัส
  g.fillStyle='#8a5a35'; g.fillRect(186,448,30,5); g.fillRect(186,440,30,3); g.fillRect(188,453,4,8); g.fillRect(210,453,4,8);
  for(const [bx,by] of [[430,412],[516,412],[430,314],[516,314]]){
    g.fillStyle='#a05a2c'; g.fillRect(bx,by,12,8);
    g.fillStyle=['#ff8f8f','#ffd98f','#d0a3ff','#8fd0ff'][(bx+by)%4];
    g.fillRect(bx+1,by-4,3,4); g.fillRect(bx+5,by-6,3,6); g.fillRect(bx+9,by-4,3,4);
  }
  // กรวดรอบลานน้ำพุ
  g.fillStyle='#b8bec9';
  for(const [sx,sy] of [[440,360],[500,360],[440,326],[500,326]]) g.fillRect(sx,sy,14,8);
  // รถจอดข้างบ้าน 2 คัน (ของประกอบ นิ่ง)
  for(const [vx,vy,vd] of [[152,300,1],[600,152,-1]]){
    g.fillStyle='#e46b6b'; g.fillRect(vx,vy+4,26,10);
    g.fillStyle='#bfe8f7'; g.fillRect(vx+(vd>0?5:13),vy,10,6);
    g.fillStyle='#3b2a1a'; g.fillRect(vx+3,vy+13,6,4); g.fillRect(vx+17,vy+13,6,4);
  }
}
function g_house(g, x, i){
  const c = HOUSE_COLORS[i];
  g.fillStyle='#3f3f3f55'; g.fillRect(x+4,156,88,5);            // เงา
  g.fillStyle='#fffaf3'; g.fillRect(x,96,96,60);
  g.fillStyle=c; g.fillRect(x+4,100,88,52);
  g.fillStyle='#5d4635'; g.fillRect(x+38,124,20,32);            // ประตู (ตรงซอยพอดี)
  g.fillStyle='#bfe8f7'; g.fillRect(x+10,110,16,16); g.fillRect(x+70,110,16,16);
  g.fillStyle='#e46b6b'; g.fillRect(x-6,72,108,16);             // หลังคา 2 ชั้น
  g.fillStyle='#d95c5c'; g.fillRect(x+6,56,84,18);
  g.fillStyle='#c74e4e'; g.fillRect(x+30,48,36,10);             // ยอดจั่ว
  g.fillStyle='#f6efe0'; g.fillRect(x+44,80,10,8);              // หน้าต่างพัดลม
}
const HOUSE_COLORS = ['#ffb3ba','#b5ead7','#c7ceea','#ffe5b4','#d4a5ff','#a0e7e3'];
bakeBG();

/* ================== ฟонтาน + ของเคลื่อนไหวฉาก ================== */
function drawFountain(t){
  const x=452,y=328;
  px(x,y,40,32,'#c9cdd4'); px(x+2,y+2,36,28,'#aab2bd');
  px(x+5,y+5,30,22,'#7ec8f2');
  const jet = 10 + Math.sin(t*3)*3;                              // น้ำพุเต้น
  px(x+18,y-jet+8,4,jet+4,'#a8dcff');
  if(Math.floor(t*6)%2){ px(x+12,y-2,3,3,'#cfeaff'); px(x+25,y-1,3,3,'#cfeaff'); }
  px(x,y,40,3,'#dfe5ec');
}

/* ================== บรรยากาศเคลื่อนไหว ================== */
const shades = [{x:120,y:200,w:170,h:56,v:13},{x:600,y:320,w:210,h:66,v:10},{x:380,y:450,w:150,h:48,v:15}];
function drawShades(dt){
  for(const s of shades){
    s.x += s.v*dt; if(s.x > W+s.w) s.x = -s.w;
    ctx.fillStyle='rgba(46,80,40,0.10)';
    ctx.beginPath(); ctx.ellipse(s.x, s.y, s.w/2, s.h/2, 0, 0, 7); ctx.fill();
  }
}
let birds=[], birdT=2;
function updateBirds(dt){
  birdT -= dt;
  if(birdT<=0){ birdT = 6+Math.random()*8;
    birds.push({x:-30, y:56+Math.random()*80, v:55+Math.random()*40, ph:Math.random()*6}); }
  for(const b of birds) b.x += b.v*dt;
  birds = birds.filter(b => b.x < W+40);
}
function drawBirds(t){
  ctx.strokeStyle='#4a3220bb'; ctx.lineWidth=2;
  for(const b of birds){
    const w = Math.sin(t*8+b.ph)*3;
    ctx.beginPath();
    ctx.moveTo(b.x-6,b.y); ctx.quadraticCurveTo(b.x-3,b.y-4+w,b.x,b.y);
    ctx.quadraticCurveTo(b.x+3,b.y-4+w,b.x+6,b.y); ctx.stroke();
  }
}
const flies = [{fx:430,fy:420,o:0,c:'#ff8f8f'},{fx:512,fy:424,o:2.3,c:'#ffd98f'},{fx:252,fy:250,o:4.1,c:'#d0a3ff'}];
function drawButterflies(t){
  for(const f of flies){
    const x=f.fx+Math.sin(t*0.8+f.o)*22, y=f.fy+Math.sin(t*2.3+f.o)*8;
    const w=2+Math.sin(t*10+f.o)*1.6;
    ctx.fillStyle=f.c;
    ctx.fillRect(x-w,y-2,w,4); ctx.fillRect(x+1,y-2,w,4);
    ctx.fillStyle='#4a3220'; ctx.fillRect(x-1,y-3,2,6);
  }
}
function drawRipples(t){
  for(let k=0;k<3;k++){
    const ph=(t*0.5+k*0.33)%1;
    ctx.strokeStyle=`rgba(255,255,255,${(0.55*(1-ph)).toFixed(2)})`; ctx.lineWidth=1;
    ctx.beginPath(); ctx.ellipse(84+k*32, 452+k*16, 5+ph*15, 2.5+ph*7, 0, 0, 7); ctx.stroke();
  }
}
const tufts = [[36,300],[270,320],[452,300],[600,320],[736,300],[868,420],[28,410],[932,330]];
function drawTufts(t){
  ctx.strokeStyle='#5aa84f'; ctx.lineWidth=2;
  tufts.forEach(([x,y],i)=>{
    const s = Math.sin(t*2+i)*1.6;
    ctx.beginPath();
    ctx.moveTo(x,y+8); ctx.lineTo(x-2+s,y);
    ctx.moveTo(x+4,y+8); ctx.lineTo(x+5+s,y+1); ctx.stroke();
  });
}
function drawFlags(t){
  HOUSE_X.forEach((x,i)=>{
    const fx0=x+34, fy0=44;
    ctx.fillStyle='#7a5a3a'; ctx.fillRect(fx0,fy0-14,2,16);
    const wv=Math.sin(t*4+i)*2;
    ctx.fillStyle=SHIRTS[i];
    ctx.beginPath(); ctx.moveTo(fx0+2,fy0-14); ctx.lineTo(fx0+16,fy0-11+wv); ctx.lineTo(fx0+2,fy0-7);
    ctx.closePath(); ctx.fill();
  });
}

/* ================== บ้านเฉพาะตัวตามชนิดแอป ================== */
const CAT_RULES = [
  ['browser', /chrome|msedge|firefox|opera|brave|vivaldi/i],
  ['media',   /spotify|vlc|itunes|foobar|aimp|wmplayer|audacity|deezer/i],
  ['game',    /steam|epic|riot|wegame|armoury|minecraft|game|xbox|playgame/i],
  ['dev',     /code|pycharm|idea|node|python|git|docker|java|electron|cursor/i],
  ['chat',    /discord|telegram|weixin|wechat|messenger|teams|slack|zoom|whatsapp|line\.exe|lineweb/i],
];
const CAT_ICON = {browser:'\u{1F310}', media:'\u{1F3B5}', game:'\u{1F3AE}', dev:'\u{1F6E0}', chat:'\u{1F4AC}', other:'⚙'};
function categoryOf(name){
  for(const [c, re] of CAT_RULES) if(re.test(name)) return c;
  return 'other';
}
function houseDecor(t){
  const procs = DATA.procs || [];
  for(let i=0; i<6; i++){
    const cx = HOUSE_X[i];
    if(i >= procs.length){                      // slot ว่าง: บ้านมืด ประตูเปิด
      ctx.fillStyle = 'rgba(28,30,46,0.42)'; ctx.fillRect(cx-54, 44, 108, 116);
      ctx.fillStyle = '#241c12'; ctx.fillRect(cx-10, 126, 20, 30);
      continue;
    }
    const cat = categoryOf(procs[i].name);
    ctx.textAlign = 'center';
    if(cat === 'browser'){                      // จานดาวเทียม
      ctx.fillStyle = '#cfd8dc'; ctx.beginPath(); ctx.arc(cx+70, 56, 9, Math.PI, 0); ctx.fill();
      px(cx+69, 56, 2, 14, '#8a8a9a'); px(cx+64, 50, 10, 2, '#8a8a9a');
      px(cx+52, 104, 12, 12, '#3f6fae');
    }
    if(cat === 'media'){                         // ลำโพง + โน้ตลอย
      px(cx-58, 138, 10, 18, '#5d4635'); px(cx-56, 141, 6, 5, '#cfd8dc'); px(cx-56, 148, 6, 4, '#cfd8dc');
      px(cx+48, 138, 10, 18, '#5d4635'); px(cx+50, 141, 6, 5, '#cfd8dc'); px(cx+50, 148, 6, 4, '#cfd8dc');
      ctx.fillStyle = '#4a3220'; ctx.font = '13px monospace';
      ctx.fillText('♪', cx-26, 116 - ((t*14 + i*7) % 26));
      ctx.fillText('♫', cx+26, 112 - ((t*11 + i*13) % 30));
    }
    if(cat === 'game'){                          // ยอดหลังคาสามเหลี่ยมเข้ม + ป้ายจอย
      ctx.fillStyle = 'rgba(90,60,130,0.75)';
      ctx.beginPath(); ctx.moveTo(cx-52, 72); ctx.lineTo(cx, 38); ctx.lineTo(cx+52, 72); ctx.closePath(); ctx.fill();
      ctx.font = '11px monospace'; ctx.fillText('\u{1F3AE}', cx, 28);
    }
    if(cat === 'dev'){                           // ปล่องเล็ก + ไฟหน้าต่างเขียวกะพริบ
      px(cx-34, 46, 8, 24, '#6e6780'); px(cx-36, 44, 12, 4, '#565163');
      for(let k=0;k<2;k++){
        const on = (Math.floor(t*2) + k + i) % 3;
        px(cx-38+k*56, 110, 16, 16, on ? '#7ee081' : '#3d5c40');
      }
    }
    if(cat === 'chat'){                          // เสาอากาศ + ซองจดหมายลอย
      px(cx+40, 38, 2, 20, '#8a8a9a'); px(cx+36, 36, 10, 2, '#8a8a9a');
      const ay = 110 - ((t*10 + i*5) % 20);
      ctx.fillStyle = '#ffffff'; ctx.fillRect(cx+30, ay, 11, 8);
      ctx.strokeStyle = '#4a3220'; ctx.lineWidth = 1;
      ctx.strokeRect(cx+30.5, ay+.5, 10, 7);
      ctx.beginPath(); ctx.moveTo(cx+31, ay+1); ctx.lineTo(cx+35.5, ay+5); ctx.lineTo(cx+40, ay+1); ctx.stroke();
    }
    ctx.font = '12px monospace';
    ctx.fillText(CAT_ICON[cat], cx-56, 60);     // ไอคอนหมวดมุมซ้ายบ้าน
  }
}

/* ================== ตัวละคร ================== */
let dust = [];
class Char {
  constructor(slot){
    this.slot = slot; this.home = HOME[slot];
    this.node = this.home; const [x,y] = NODES[this.home];
    this.x = x; this.y = y; this.dest = this.home;
    this.speed = 0; this.targetSpeed = 0;
    this.face = 'f'; this.flip = false; this.frameT = 0;
    this.idle = false; this.active = false; this.zT = 0;
    this.name = ''; this.pendingName = ''; this.nameAlpha = 0;
    this.cpu = 0; this.memMB = 0; this.size = 1;
  }
  setProc(p){
    this.active = !!p;
    const name = p ? p.name : '';
    if(name !== this.pendingName && name !== this.name){
      if(this.name && name) this.pendingName = name;             // ค่อยๆ fade
      else { this.name = name; this.pendingName=''; this.nameAlpha = 1; }
    }
    if(!this.name && name){ this.name = name; this.nameAlpha = 1; }
    this.cpu = p ? p.cpu : 0;
    this.memMB = p ? p.memMB : 0;
    this.size = this.memMB > 800 ? 1.2 : this.memMB > 300 ? 1.1 : 1;
    const c = this.cpu;
    this.idle = !p || c < 1;
    this.targetSpeed = c > 20 ? 150 : c > 5 ? 75 : c > 1 ? 35 : 0;
    if(this.idle) this.dest = this.node;
    else if(this.node === this.dest) this.pickNext();
  }
  pickNext(){
    const opts = ADJ[this.node].filter(n => n !== this.dest || this.node === this.home);
    let next;
    if(Math.random() < 0.25) next = this.home;                    // ชอบกลับบ้าน
    else next = (opts.length ? opts : ADJ[this.node])[Math.floor(Math.random()*(opts.length||ADJ[this.node].length))];
    this.dest = next;
  }
  update(dt){
    this.speed += (this.targetSpeed - this.speed) * Math.min(1, dt * 2.5);   // lerp — ไม่กระตุก
    if(!this.active || this.idle){ this.zT += dt; return; }
    if(this.node === this.dest) this.pickNext();
    const [tx,ty] = NODES[this.dest];
    const dx = tx - this.x, dy = ty - this.y;
    const dist = Math.abs(dx) + Math.abs(dy);
    if(dist > 0.5 && this.speed > 1){
      if(Math.abs(dx) >= Math.abs(dy)){ this.face='s'; this.flip = dx < 0; }
      else { this.face = dy > 0 ? 'f' : 'b'; }
      const step = Math.min(dist, this.speed * dt);
      if(dist > 0){ this.x += dx/dist*step; this.y += dy/dist*step; }
      if(Math.abs(dx) < 1 && Math.abs(dy) < 1){ this.x=tx; this.y=ty; this.node=this.dest; }
      if(this.speed > 110 && Math.random() < dt*14)             // ฝุ่นตอนวิ่ง
        dust.push({x:this.x - Math.sign(dx)*10, y:this.y+2, a:1, r:2});
    }
    this.frameT += dt * (this.speed / 26 + 0.4);
    if(this.pendingName){
      this.nameAlpha -= dt*2;
      if(this.nameAlpha<=0){ this.name=this.pendingName; this.pendingName=''; this.nameAlpha=0; }
    } else if(this.active && this.name && this.nameAlpha < 1)
      this.nameAlpha = Math.min(1, this.nameAlpha + dt*2);
  }
  draw(t){
    if(!this.active) return;
    const bob = (!this.idle && this.speed > 2) ? Math.round(Math.sin(this.frameT*6)) : 0;
    const sw = 15*this.size, sh = 5*this.size;
    ctx.fillStyle='#3f3f3f55';
    ctx.beginPath(); ctx.ellipse(this.x, this.y+30, sw, sh, 0, 0, 7); ctx.fill();
    const moving = !this.idle && this.speed > 2;
    const wf = this.speed > 110 ? 'run' : (moving ? (Math.floor(this.frameT)%2 ? 'a' : 'b') : 'stand');
    const ty = this.y - (10*U) + bob - (this.size>1 ? 4 : 0);
    const blink = ((t*1000 + this.slot*777) % 3800) < 160;
    if(this.memMB > 300 && this.face==='s'){                      // เป้หลังคนถือของเยอะ
      const bx = this.flip ? this.x+15 : this.x-19;
      ctx.fillStyle='#b5651d'; ctx.fillRect(bx, this.y-16, 6, 10);
      ctx.fillStyle='#8a4a15'; ctx.fillRect(bx, this.y-16, 6, 3);
    }
    drawFrame(this.face, moving ? (wf==='run'?'stand':wf) : 'stand', wf,
              this.x - 4*U*this.size, ty, this.slot, this.flip, blink);
    if(this.cpu > 20){                                            // เหงื่อตอนงานหนัก
      const dy = (t*3+this.slot)%1*4;
      ctx.fillStyle='#7ec8f2'; ctx.fillRect(this.x+15, this.y-26+dy, 3, 4);
    }
    if(this.idle && this.zT % 2.4 < 1.2){                        // ฟอง z
      ctx.fillStyle='#ffffffcc'; ctx.font='12px monospace'; ctx.textAlign='left';
      ctx.fillText('z', this.x+14, this.y-28-(this.zT%1.2)*8);
    }
    if(this.name){
      ctx.globalAlpha = Math.max(0, Math.min(1, this.nameAlpha));
      const label = this.name.replace(/\.exe$/i,'').slice(0,14);
      ctx.font='bold 10px monospace';
      const tw = ctx.measureText(label).width;
      rr(this.x-tw/2-3, this.y+34, tw+6, 13, 3, '#fffbe7dd', null);
      ctx.fillStyle='#4a3220'; ctx.textAlign='center';
      ctx.fillText(label, this.x, this.y+44);
      ctx.globalAlpha = 1;
    }
  }
}
const chars = HOME.map((_, i) => new Char(i));

/* ================== polling ================== */
let DATA = {procs: []};
async function poll(){
  try{
    const r = await fetch('/api/status'); DATA = await r.json();
    fetch('/api/ping').catch(()=>{});
    refreshMenu();
  }catch(e){}
  setTimeout(poll, 1000);
}
poll();
function applyData(){
  (DATA.procs || []).forEach((p, i) => chars[i] && chars[i].setProc(p));
  for(let i = (DATA.procs||[]).length; i < chars.length; i++) chars[i].setProc(null);
}

/* ================== tooltip ================== */
const tip = document.getElementById('tip');
addEventListener('mousemove', e => {
  const wx = (e.clientX * dpr - offX) / scale, wy = (e.clientY * dpr - offY) / scale;
  const hit = chars.find(c => c.active && Math.abs(wx-c.x) < 16*c.size && wy > c.y-34 && wy < c.y+10);
  if(hit){
    tip.style.display='block';
    tip.style.left=(e.clientX+14)+'px'; tip.style.top=(e.clientY-10)+'px';
    tip.innerHTML = `<b>${hit.name}</b><br>CPU ${hit.cpu}% · RAM ${hit.memMB} MB`;
  } else tip.style.display='none';
});

/* ================== HUD meters ================== */
function fmtRate(bps){
  if(bps >= 1e6) return (bps/1e6).toFixed(1) + ' MB/s';
  if(bps >= 1e3) return Math.round(bps/1e3) + ' KB/s';
  return Math.round(bps || 0) + ' B/s';
}
const HUD_IDS = [['cpu','CPU'],['ram','RAM'],['disk','DISK'],['net','NET'],['bat','BATTERY']];
document.getElementById('hud').innerHTML = HUD_IDS.map(([id,lbl]) =>
  `<div class="m"><b id="lbl-${id}">${lbl}</b><div class="bar"><i id="bar-${id}"></i></div></div>`).join('');
function setMeter(id, pct, text){
  const bar = document.getElementById('bar-'+id), lbl = document.getElementById('lbl-'+id);
  bar.style.width = Math.max(0, Math.min(100, pct)) + '%';
  bar.className = pct > 80 ? 'hot' : pct > 60 ? 'warn' : '';
  lbl.textContent = text;
}
function fmtUptime(sec){
  const h = Math.floor(sec/3600), m = Math.floor(sec%3600/60);
  return h >= 24 ? Math.floor(h/24)+' วัน '+(h%24)+' ชม.' : h+' ชม. '+m+' น.';
}
function drawHud(){
  const d = DATA;
  if(d.cpu === undefined) return;
  setMeter('cpu', d.cpu, `CPU ${Math.round(d.cpu)}%`);
  setMeter('ram', d.ram, `RAM ${Math.round(d.ram)}%`);
  setMeter('disk', Math.min(100, ((d.disk_r||0)+(d.disk_w||0)) / 2e7 * 100),
           `DISK ↓${fmtRate(d.disk_r)} ↑${fmtRate(d.disk_w)}`);
  setMeter('net', Math.min(100, ((d.net_rx||0)+(d.net_tx||0)) / 2e6 * 100),
           `NET ↓${fmtRate(d.net_rx)} ↑${fmtRate(d.net_tx)}`);
  if(d.battery) setMeter('bat', d.battery.pct, `BATT ${d.battery.pct}%${d.battery.plugged ? ' ⚡' : ''}`);
  else setMeter('bat', 100, 'AC ⚡ (ไม่มีแบต)');
  document.getElementById('small').innerHTML =
      `เปิดมา ${fmtUptime(d.uptime||0)} · processes ${d.proc_count||'-'}` +
      (d.temp ? ` · ${d.temp}°C` : '');
}

/* ================== ควันโรงงาน (Disk) + ผู้มาเยือน (Network) ================== */
let smokeP = [];
let visitors = [], visT = 4;
const EDGE_NODES = ['m0','m7'];
function updateEnv(dt){
  for(const d of dust){ d.a -= dt*2; d.r += dt*6; }
  dust = dust.filter(d => d.a > 0);
  const d = DATA;
  const diskAct = (d.disk_r||0) + (d.disk_w||0);
  if(diskAct > 2e5 && smokeP.length < 40 && Math.random() < Math.min(0.4, diskAct/1.5e7)){
    smokeP.push({x:926+Math.random()*6-3, y:70, vy:-(10+Math.random()*12), r:3, a:0.85});
  }
  for(const s of smokeP){ s.y += s.vy*dt; s.x += 7*dt; s.r += 6*dt; s.a -= 0.4*dt; }
  smokeP = smokeP.filter(s => s.a > 0);
  const netAct = (d.net_rx||0) + (d.net_tx||0);
  visT -= dt * Math.min(2, 0.3 + netAct/1e6);
  if(visT <= 0 && netAct > 2e5 && visitors.length < 6){
    visT = 3;
    const entry = EDGE_NODES[Math.floor(Math.random()*2)];
    const [x,y] = NODES[entry];
    visitors.push({x, y, node:entry, dest:'', hops:0, face:'s', flip:entry==='m7', ft:Math.random()*9});
  }
  for(const v of visitors){
    if(!v.dest){
      const opts = ADJ[v.node];
      v.dest = opts[Math.floor(Math.random()*opts.length)];
    }
    const [tx,ty] = NODES[v.dest];
    const dx = tx-v.x, dy = ty-v.y, dist = Math.abs(dx)+Math.abs(dy);
    if(dist < 1){ v.x=tx; v.y=ty; v.node=v.dest; v.dest=''; v.hops++;
      if(EDGE_NODES.includes(v.node) && v.hops >= 2) v.done = true;
    } else {
      if(Math.abs(dx)>=Math.abs(dy)){ v.face='s'; v.flip = dx<0; } else v.face = dy>0?'f':'b';
      v.x += dx/dist*55*dt; v.y += dy/dist*55*dt; v.ft += dt*2.5;
    }
  }
  visitors = visitors.filter(v => !v.done);
}
function drawEnvFx(){
  for(const s of smokeP){
    ctx.fillStyle = `rgba(120,116,128,${(s.a*0.6).toFixed(2)})`;
    ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, 7); ctx.fill();
  }
  for(const v of visitors){
    ctx.fillStyle='#3f3f3f55';
    ctx.beginPath(); ctx.ellipse(v.x, v.y+30, 13, 4, 0, 0, 7); ctx.fill();
    drawFrame(v.face, 'stand', v.dest ? (Math.floor(v.ft)%2?'a':'b') : 'stand',
              v.x - 4*U, v.y - 10*U, 6, v.flip, false);
  }
}

/* ================== main loop ================== */
let last = 0;
function frame(ms){
  const dt = Math.min(0.05, (ms-last)/1000); last = ms;
  const t = ms/1000;
  ctx.setTransform(1,0,0,1,0,0);
  ctx.fillStyle='#2c3a24'; ctx.fillRect(0,0,cv.width,cv.height);   // frame รอบจอ (ตอน aspect ไม่ตรง)
  ctx.setTransform(scale,0,0,scale,offX,offY);
  ctx.drawImage(bg, 0, 0);
  drawShades(dt); drawRipples(t); drawTufts(t); drawFlags(t);
  houseDecor(t);
  drawFountain(t);
  applyData();
  updateEnv(dt); updateBirds(dt);
  const order = chars.filter(c=>c.active).sort((a,b)=>a.y-b.y);
  for(const c of order){ c.update(dt); }
  for(const d of dust){ ctx.fillStyle=`rgba(180,150,100,${d.a.toFixed(2)})`; ctx.beginPath(); ctx.arc(d.x,d.y,d.r,0,7); ctx.fill(); }
  for(const c of order){ c.draw(t); }
  drawEnvFx();
  drawButterflies(t); drawBirds(t);
  drawHud();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

/* ================== เมนูบ้าน (detail / pin / hide / kill) ================== */
const menuEl = document.getElementById('menu');
const hiddenBtn = document.getElementById('hiddenBtn');
const toastEl = document.getElementById('toast');
let menuSlot = -1, killArm = 0, killTimer = null, toastTimer = null;

function toast(msg){
  toastEl.textContent = msg; toastEl.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => toastEl.hidden = true, 2600);
}
async function apiPost(path, body){
  try{
    const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                                 body: JSON.stringify(body)});
    return await r.json();
  }catch(e){ return {error:'server ปิดไปแล้ว'}; }
}
function slotAt(wx, wy){                    // คลิกบ้านหรือตัวละคร → slot ไหน
  for(let i=0;i<6;i++){
    const cx = HOUSE_X[i];
    if(Math.abs(wx-cx) < 58 && wy > 36 && wy < 186) return i;
    const c = chars[i];
    if(c && c.active && Math.abs(wx-c.x) < 18 && wy > c.y-34 && wy < c.y+10) return i;
  }
  return -1;
}
addEventListener('click', e => {
  if(menuEl.contains(e.target) || e.target === hiddenBtn || hiddenBtn.contains(e.target)) return;
  const wx = (e.clientX*dpr - offX)/scale, wy = (e.clientY*dpr - offY)/scale;
  const i = slotAt(wx, wy);
  if(i >= 0) openMenu(i, e); else closeMenu();
});
addEventListener('keydown', e => { if(e.key === 'Escape') closeMenu(); });

function openMenu(i, e){
  menuSlot = i; killArm = 0; clearTimeout(killTimer);
  menuEl.hidden = false;
  const pxX = Math.min(innerWidth-250, Math.max(6, e ? e.clientX+16 : 60));
  const pxY = Math.min(innerHeight-280, Math.max(6, (e ? e.clientY-20 : 80)));
  menuEl.style.left = pxX+'px'; menuEl.style.top = pxY+'px';
  renderMenu();
}
function closeMenu(){ menuEl.hidden = true; menuSlot = -1; }

function renderMenu(){
  const p = (DATA.procs || [])[menuSlot];
  if(!p){ closeMenu(); return; }                    // proc ตาย/หลุด slot → ปิดอัตโนมัติ
  const cat = categoryOf(p.name);
  const isPinned = (DATA.pinned || []).includes(p.name.toLowerCase());
  menuEl.innerHTML = `
    <h3>${CAT_ICON[cat]} ${p.name}</h3>
    <div>CPU <b>${p.cpu}%</b> · RAM <b>${p.memMB} MB</b> · pid ${p.pid}</div>
    <canvas id="spark" width="210" height="44"></canvas>
    <div class="row">
      <button id="mPin" class="${isPinned?'on':''}">📌 ${isPinned?'ถอดหมุด':'ปักหมุด'}</button>
      <button id="mHide">👁 ซ่อนบ้าน</button>
      <button id="mKill" class="danger">☠ ปิดแอป</button>
      <button id="mClose">✖</button>
    </div>`;
  const sc = menuEl.querySelector('#spark'), sctx = sc.getContext('2d');
  const hist = p.hist || [];
  const max = Math.max(5, ...hist);
  hist.forEach((v, k) => {
    const h = Math.round(v / max * 38);
    sctx.fillStyle = v > 20 ? '#ff7a5c' : v > 5 ? '#ffd35c' : '#7ee081';
    sctx.fillRect(2 + k*7, 42 - h, 5, h);
  });
  sctx.fillStyle = '#4a322099'; sctx.font = '9px monospace';
  sctx.fillText('CPU 30 วิล่าสุด →', 128, 10);
  menuEl.querySelector('#mPin').onclick = async () => {
    const r = await apiPost('/api/pin', {name: p.name, on: !isPinned});
    if(r.error) toast({max6:'ปักหมุดได้สูงสุด 6 บ้าน', gone:'โปรเซสนี้ปิดไปแล้ว'}[r.error] || r.error);
    else renderMenu();
  };
  menuEl.querySelector('#mHide').onclick = async () => {
    await apiPost('/api/hide', {name: p.name, on: true});
    closeMenu();
  };
  const killBtn = menuEl.querySelector('#mKill');
  killBtn.onclick = async () => {
    if(killArm < 2){                                  // ยืนยันสองชั้น
      killArm = 2; killBtn.textContent = '⚠ แน่ใจ? กดอีกที';
      clearTimeout(killTimer); killTimer = setTimeout(() => { killArm = 0; if(!menuEl.hidden) renderMenu(); }, 3000);
      return;
    }
    const r = await apiPost('/api/kill', {name: p.name, pid: p.pid});
    if(r.error) toast({gone:'แอปปิดไปแล้ว', mismatch:'ชื่อไม่ตรงโปรเซส (ข้อมูลใหม่กว่านี้)', 'access denied':'ปิดไม่ได้ — สิทธิ์ไม่พอ'}[r.error] || r.error);
    closeMenu();
  };
  menuEl.querySelector('#mClose').onclick = closeMenu;
}

hiddenBtn.onclick = () => {
  const hs = DATA.hidden || [];
  menuSlot = -2;                                      // โหมด hidden-list (renderMenu ไม่ทับ)
  menuEl.innerHTML = `<b>ซ่อนไว้ (${hs.length})</b><br>` +
    (hs.length ? hs.map(n => `<button class="uh" data-n="${n}">👁 ${n}</button>`).join(' ') : '<i>ไม่มี</i>');
  menuEl.hidden = false;
  menuEl.style.left = '10px'; menuEl.style.top = (innerHeight - 230) + 'px';
  menuEl.querySelectorAll('.uh').forEach(b => b.onclick = async () => {
    await apiPost('/api/hide', {name: b.dataset.n, on: false});
  });
};
function updateHiddenBtn(){
  const hs = DATA.hidden || [];
  hiddenBtn.hidden = !hs.length;
  hiddenBtn.textContent = `👁 ซ่อนไว้ (${hs.length})`;
}
function refreshMenu(){
  updateHiddenBtn();
  if(!menuEl.hidden && menuSlot >= 0) renderMenu();
}

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

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def do_POST(self):
        if self.path == "/api/shutdown":
            self._send(200, '{"ok":true}')
            threading.Timer(0.3, lambda: os._exit(0)).start()
        elif self.path in ("/api/pin", "/api/hide"):
            b = self._json_body()
            key = str(b.get("name", "")).lower()
            on = bool(b.get("on", True))
            target = SAMPLER.pinned if self.path == "/api/pin" else SAMPLER.hidden
            if self.path == "/api/pin" and on:
                if len(SAMPLER.pinned) >= TOP_N:
                    self._send(200, '{"error":"max 6"}'); return
                if not any(x["name"].lower() == key for x in SAMPLER.last_all):
                    self._send(200, '{"error":"gone"}'); return
            target.discard(key) if not on else target.add(key)
            self._send(200, '{"ok":true}')
        elif self.path == "/api/kill":
            b = self._json_body()
            procs = SAMPLER.snapshot().get("procs", []) if SAMPLER else []
            res = verify_kill(procs, b.get("name", ""), b.get("pid"))
            if res != "ok":
                self._send(200, json.dumps({"error": res})); return
            try:
                psutil.Process(b["pid"]).terminate()
                self._send(200, '{"ok":true}')
            except Exception as e:
                self._send(200, json.dumps({"error": "access denied" if isinstance(e, psutil.AccessDenied) else "gone"}))
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
