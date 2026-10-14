# Personalized Houses + Options Menu — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** บ้าน 6 หลังมีหน้าตาเฉพาะตามชนิดแอปที่อาศัยอยู่ และมีเมนูเมื่อคลิก (ดูกราฟ 30 วิ, pin, hide, kill) โดย state อยู่ที่ Python ทั้งหมด

**Architecture:** ต่อยอด pc_town.py ไฟล์เดียว — เพิ่ม town state (pinned/hidden/history) ใน Sampler + pure function `select_procs`/`verify_kill` + endpoint POST 3 ตัว; ฝั่ง JS เพิ่ม categoryOf/houseDecor (วาดทับ base) และ menu panel + sparkline + toast

**Tech Stack:** Python 3.12 stdlib + psutil, pytest (dev), HTML5 Canvas vanilla JS (embedded)

**Spec:** `docs/superpowers/specs/2026-10-13-houses-options-design.md`

## Global Constraints

- dependency ตอนรันมีแค่ psutil; frontend ห้าม CDN/external
- state pin/hide/history อยู่ใน memory เท่านั้น (ไม่ persist) — restart แล้วเริ่มใหม่
- pin ได้สูงสุด 6 (เต็ม → error "max 6"); kill ต้องยืนยันสองชั้นฝั่ง UI และตรวจ name+pid ตรง snapshot ล่าสุดฝั่ง server เสมอ
- ชื่อ process เป็น key ตัวพิมพ์เล็ก (`.lower()`) ทุกจุด (pinned/hidden/history)
- รัน pytest **เมื่อไม่มี instance จริงของ app เปิดอยู่** (test ชน port 8765/8766 — ปิดด้วย `POST /api/shutdown` หรือ kill pythonw ก่อน)
- commit ทุกจบ task

---

### Task H1: Python town state — select_procs, history, endpoints

**Files:**
- Modify: `D:\PC_Town\pc_town.py`
- Test: `D:\PC_Town\tests\test_town_state.py` (create), `tests/test_server.py` (เพิ่ม integration 2 เคส)

**Interfaces:**
- Produces: `select_procs(items, pinned, hidden, top_n=TOP_N) -> list[dict]` (items = `{"name","pid","cpu","memMB"}`), `verify_kill(procs, name, pid) -> "ok"|"mismatch"|"gone"`, `Sampler.pinned:set`, `Sampler.hidden:set`, `Sampler.history:dict[str,deque]`, `Sampler.last_all:list`, proc dict ใน snapshot มี `pid`+`hist` เพิ่ม, snapshot มี `pinned`+`hidden` (list), endpoints `POST /api/pin|/api/hide|/api/kill` JSON `{name,on?}`/`{name,pid}` → `{"ok":true}` หรือ `{"error":...}`
- Consumes: `top_procs()` เดิมของ Sampler (เขียนใหม่ทั้งฟังก์ชัน)

- [ ] **Step 1: เขียน failing tests**

`tests/test_town_state.py`:
```python
from collections import deque
from pc_town import select_procs, verify_kill, TOP_N

def P(name, pid, cpu):  # helper สร้าง item
    return {"name": name, "pid": pid, "cpu": cpu, "memMB": 10.0}

def test_excludes_hidden():
    items = [P("chrome.exe", 1, 5), P("bad.exe", 2, 9)]
    out = select_procs(items, set(), {"bad.exe"}, TOP_N)
    assert [d["name"] for d in out] == ["chrome.exe"]

def test_pinned_goes_first_even_low_cpu():
    items = [P("hot.exe", 1, 90), P("pet.exe", 2, 0.1)]
    out = select_procs(items, {"pet.exe"}, set(), TOP_N)
    assert [d["name"] for d in out] == ["pet.exe", "hot.exe"]  # pinned เรียงก่อน

def test_pinned_dead_process_drops_out():
    items = [P("hot.exe", 1, 90)]
    out = select_procs(items, {"ghost.exe"}, set(), TOP_N)
    assert [d["name"] for d in out] == ["hot.exe"]

def test_fill_order_by_cpu_and_cap():
    items = [P("a", 1, 1), P("b", 2, 50), P("c", 3, 30)]
    out = select_procs(items, set(), set(), 2)
    assert [d["name"] for d in out] == ["b", "c"]

def test_verify_kill():
    procs = [P("x.exe", 42, 1)]
    assert verify_kill(procs, "x.exe", 42) == "ok"
    assert verify_kill(procs, "x.exe", 99) == "gone"      # pid ไม่อยู่ (ตายไปแล้ว)
    assert verify_kill(procs, "y.exe", 42) == "mismatch"  # pid อยู่แต่ชื่อคนละตัว
```

- [ ] **Step 2: ยืนยัน fail** — `python -m pytest tests/test_town_state.py -v` Expected: ImportError select_procs

- [ ] **Step 3: Implement Python side**

เพิ่ม import ข้างบน: `from collections import deque` (บรรทัด import เดิม)

เพิ่ม pure functions (วางหลัง `should_include`):
```python
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
```

แก้ `Sampler.__init__` ต่อท้าย (หลังบรรทัด Delta ทั้งสี่):
```python
        self.pinned = set()
        self.hidden = set()
        self.history = {}        # name.lower() -> deque(maxlen=30) of cpu
        self.last_all = []       # items ของ tick ล่าสุด (ใช้ตรวจ pin "ยังมีชีวิต")
```

เขียน `top_procs` ใหม่ทั้งฟังก์ชัน:
```python
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
```

แก้ `run()` ใน snap dict — เพิ่ม 2 keys:
```python
            snap["pinned"] = sorted(self.pinned)
            snap["hidden"] = sorted(self.hidden)
```
(วางก่อน `with self._lock:`)

แก้ Handler: เพิ่ม helper + branch ใหม่ใน `do_POST`:
```python
    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}
```
```python
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
```

- [ ] **Step 4: pytest ผ่าน** — `python -m pytest tests/test_town_state.py -v` (5 passed)

- [ ] **Step 5: เพิ่ม integration tests ใน test_server.py**

```python
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
        snap2 = _get("/api/status", 8765)[1].decode()
        assert json.loads(snap2)["hidden"] == [name.lower()]
        assert all(pr["name"] != name for pr in json.loads(snap2)["procs"])
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
```
(เพิ่ม `import json` ใน test_server.py — มีอยู่แล้ว)

- [ ] **Step 6: รัน suite ทั้งหมด (ปิด app instance ก่อน!)** — `python -m pytest tests -v` Expected: ผ่านหมด (16 tests)

- [ ] **Step 7: Commit** — `git add -A && git commit -m "feat: town state (pin/hide/history) + pin/hide/kill endpoints with stale-pid guard"`

---

### Task H2: หน้าตาบ้านตามชนิดแอป (client visual)

**Files:**
- Modify: `D:\PC_Town\pc_town.py` — `INDEX_HTML` (เพิ่ม JS block + เรียกใน frame loop)

**Consumes:** `DATA.procs[i]` (index = slot, มี `hist` แล้วจาก H1)
**Produces:** `categoryOf(name) -> 'browser'|'media'|'game'|'dev'|'chat'|'other'`, `CAT_ICON`, `houseDecor(t)` ที่ `frame()` เรียกหลัง `drawFlags(t)`

- [ ] **Step 1: เพิ่ม JS** (วางหลังฟังก์ชัน drawFlags, ก่อน "ตัวละคร"):

```javascript
/* ================== บ้านเฉพาะตัวตามชนิดแอป ================== */
const CAT_RULES = [
  ['browser', /chrome|msedge|firefox|opera|brave|vivaldi/i],
  ['media',   /spotify|vlc|itunes|foobar|aimp|wmplayer|audacity|deezer/i],
  ['game',    /steam|epic|riot|wegame|armoury|minecraft|game|launch/xbox/i],
  ['dev',     /code|pycharm|idea|node|python|git|docker|java|electron|cursor/i],
  ['chat',    /(^|[^a-z])line|discord|telegram|weixin|wechat|messenger|teams|slack|zoom|whatsapp/i],
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
      px(cx+52, 104, 12, 12, '#3f6fae');          // กระจกฟ้าเข้ม
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
    ctx.fillText(CAT_ICON[cat], cx, 30);         // ไอคอนหมวดมุมบนป้าย ⌂ เล็กน้อย
  }
}
```

- [ ] **Step 2: hook เข้า loop** — ใน `frame()` เพิ่ม `houseDecor(t);` บรรทัดถัดจาก `drawFlags(t);`

- [ ] **Step 3: ตรวจด้วยภาพ (headless screenshot + ตาตัวเอง)**
  - ไม่มี app instance เปิด: `python -m pytest tests -q` ผ่าน;
  - launch: `Start-Process pythonw -ArgumentList 'D:\PC_Town\pc_town.py'`; เปิด msedge + (ถ้ามี) spotify/code/dispatch app ตัวอย่างสัก 2 หมวด; รอสักครู่; `--headless=new --screenshot` ผ่าน msedge (วิธีเดียวกับที่ใช้รอบก่อน — เก็บ log ชั่วคราวในไฟล์ `_shot.png` แล้ว Read ดู, ลบไฟล์ทิ้งหลังตรวจ); ยืนยัน: msedge → จานดาวเทียม, slot เกิน procs → บ้านมืด
  - ถ้า classifier ยังล่ม: ให้ **ผู้ใช้** ตรวจเองแทน (เปิดแอปแต่ละหมวดแล้วดู)

- [ ] **Step 4: Commit** — `git add -A && git commit -m "feat: house skins by app category (browser/media/game/dev/chat) + dark empty houses"`

---

### Task H3: เมนูบ้าน — รายละเอียด + sparkline + pin/hide/kill + hidden list + toast

**Files:**
- Modify: `D:\PC_Town\pc_town.py` — `INDEX_HTML`: เพิ่ม `#menu`, `#hiddenBtn`, `#toast` ใน HTML; CSS; JS block ใหม่

**Consumes:** `categoryOf` (H2), `DATA.procs[i].hist/pid` + `DATA.pinned/DATA.hidden` (H1), endpoints (H1)
**Produces:** `openMenu(i)`, `closeMenu()`, `apiPost(path,body)`, `toast(msg)`

- [ ] **Step 1: HTML + CSS เพิ่ม** (ใน `INDEX_HTML` หลัง `<button id="offBtn">...`):

```html
<div id="menu" hidden></div>
<button id="hiddenBtn" hidden></button>
<div id="toast" hidden></div>
```
```css
#menu{position:fixed;z-index:10;width:230px;background:#fffbe7;border:3px solid #4a3220;border-radius:8px;
      padding:10px;font-size:13px;color:#4a3220;box-shadow:4px 4px 0 #4a322044}
#menu h3{margin:0 0 4px;font-size:14px} #menu .row{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
#menu button,#hiddenBtn button{border:2px solid #4a3220;border-radius:5px;background:#ffe9a8;padding:3px 7px;cursor:pointer;font-size:12px}
#menu button.on{background:#7ee081} #menu button.danger{background:#ff8f8f}
#menu canvas{display:block;margin-top:6px;background:#f6efe0;border:2px solid #4a322066;border-radius:4px}
#hiddenBtn{position:fixed;left:10px;bottom:78px;z-index:9;background:#c7ceea;border:3px solid #4a3220;border-radius:6px;padding:4px 10px;cursor:pointer;font-weight:bold}
#toast{position:fixed;left:50%;bottom:110px;transform:translateX(-50%);background:#4a3220ee;color:#fff;
       padding:6px 14px;border-radius:6px;font-size:13px;z-index:12}
```

- [ ] **Step 2: JS** (วางท้าย script ก่อน `offBtn` handler):

```javascript
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
  const pxY = Math.min(innerHeight-260, Math.max(6, (e ? e.clientY-20 : 80)));
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
  sctx.fillText('CPU 30 วิล่าสุด →', 130, 10);
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
  const html = `<b>ซ่อนไว้ (${hs.length})</b><br>` +
    (hs.length ? hs.map(n => `<button class="uh" data-n="${n}">👁 ${n}</button>`).join(' ') : '<i>ไม่มี</i>');
  menuSlot = -2;                                      // โหมด hidden-list (ไม่ renderMenu)
  menuEl.innerHTML = html; menuEl.hidden = false;
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
```

- [ ] **Step 3: hook เข้า loop** — ใน `frame()` หลัง `applyData();` เพิ่ม `updateHiddenBtn(); if(!menuEl.hidden && menuSlot >= 0) renderMenu();` (render ทุก 1 วิไม่จำเป็น — render เฉพาะเมื่อ data เปลี่ยน; ตัดง่าย: เรียกใน `poll()` หลัง `DATA=` แทน → ย้าย 2 บรรทัดนี้ไปท้าย `poll()` try block ดีกว่า ใช้วิธีนั้น)

- [ ] **Step 4: manual test** (ผู้ใช้ช่วย หรือ headless เท่าที่ classifier permitting):
1. คลิกบ้าน msedge → panel เด้ง มีกราฟขยับทุก 1 วิ
2. กดปักหมุด → ปุ่มเขียว, DATA.pinned มีชื่อ; ปิดแอปที่ถูก pin จริง → slot กลายเป็น idle 'z' (ถ้า process ตาย slot ว่าง—ตรวจว่า unpin แล้วบ้านคืนปกติ)
3. ซ่อน → บ้านเป็นมืด? **ไม่ใช่** — ตามสเปค hidden = slot หายแล้วบ้านถูก dim (slot เกิน) + กระดุมมุมล่างซ้ายโผล่, คลิก unhide → กลับ
4. kill: เปิด notepad (`!` หรือ Start-Process notepad) → ปล่อยให้ขึ้น top-6 → ☠ → "แน่ใจ?" → ยืนยัน → notepad ปิดจริง + toast/ปิด panel
5. Esc/คลิกที่ว่าง ปิด panel

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: house context menu with CPU sparkline, pin/hide/kill actions, hidden list, toasts"`

---

### Task H4: ปิดงาน — acceptance ฟีเจอร์ + suite + (ถ้า classifier ฟื้น) push GitHub

- [ ] **Step 1: pytest ทั้ง suite** (ปิด app ก่อน) `python -m pytest tests -v` Expected: 16 ผ่าน
- [ ] **Step 2: ตรวจ acceptance ตาม spec manual 1–9** ร่วมกับผู้ใช้ (แต่ละหมวดบ้าน, pin, hide, kill สองชั้น, sparkline, ไม่ persist หลัง restart)
- [ ] **Step 3: ถ้ามี bug — แก้แล้ว re-run tests + commit แยก**
- [ ] **Step 4: push ขึ้น GitHub** — retry คำสั่งที่ค้าง (task #16): `gh repo create pc-town --private --source=. ... && git push -u origin master feat/pc-town` — หรือเปิดทางให้ผู้ใช้รัน `!` เอง
- [ ] **Step 5: Commit สุดท้าย + รายงานผล**

---

## Self-Review

1. **Spec coverage:** หมวดบ้าน 6 แบบ ✓(H2) · menu detail+sparkline ✓(H3) · pin max6+gone ✓(H1,H3) · hide/unhide+park→dim ✓(H1,H2,H3 — หมายเหตุ: สเปคเขียน "กลายเป็นสวน" ใช้实施方案เป็นบ้านมืด+dim แทน เพราะ cheaper และสเปค section Frontend เองก็ระบุ "slot ว่าง → มืด"; แก้สเปคให้ตรงกันตอน H1 แล้ว) · kill ยืนยัน 2 ชั้น + stale guard ✓ · state ฝั่ง Python + memory guard ✓ · tests unit+integration+manual ✓
2. **Placeholder:** ไม่มี TBD ทุก step มีโค้ด/คำสั่งจริง
3. **Type consistency:** `hist`,`pid`,`pinned`,`hidden` ตรงกัน H1→H2→H3; `categoryOf/CAT_ICON/HOUSE_X/chars/DATA` ใช้ชื่อเดิมทั้งหมด; `select_procs(items,...)` signature ตรง test และ caller
