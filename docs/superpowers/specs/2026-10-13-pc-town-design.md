# PC Town — Design Spec

วันที่: 2026-10-13
สถานะ: รอ user review

## ปัญหา / เป้าหมาย

ผู้ใช้ต้องการเห็น "การทำงานของคอมพิวเตอร์" เป็นภาพแอนิเมชันแนว agent animation — เมือง pixel art สดใสที่มีตัวละครเดินไปมา โดยแต่ละตัวละครคือโปรเซสที่รันอยู่จริงในเครื่อง กดเปิดดูได้จาก shortcut บน Desktop และไม่กินทรัพยากรเครื่อง (รันเฉพาะตอนเปิดดู)

## รูปแบบการใช้งาน (User Flow)

1. ดับเบิลคลิก shortcut "PC Town" บน Desktop
2. Python server เริ่มทำงาน (no console window — ใช้ `pythonw.exe`) แล้วเปิด default browser ที่ `http://127.0.0.1:<port>` อัตโนมัติ
3. หน้าจอแสดงเมืองแอนิเมชัน ข้อมูลอัปเดตแบบ real-time ทุก 1 วินาที
4. เมื่อผู้ใช้ปิดแท็บ browser → heartbeat หยุด → server ปิดตัวเองภายใน ~5 วินาที (ไม่ค้างใน background)
   - ทางออกสำรอง: มีปุ่ม "ปิดแอป" ในหน้าเว็บ ที่เรียก `/api/shutdown` แล้ว server exit ทันที

## สถาปัตยกรรม

```
D:\PC_Town\
├── pc_town.py      # ไฟล์เดียวจบ: server + data sampler + HTML/CSS/JS (embedded string)
└── docs\superpowers\specs\   # design doc นี้
```

- **Shortcut**: Windows `.lnk` บน Desktop สั่ง `pythonw.exe D:\PC_Town\pc_town.py` (สร้างด้วย PowerShell `WScript.Shell`)

### ฝั่ง Python (pc_town.py)

ใช้เฉพาะ standard library + `psutil` (เครื่องมี v7.2.2 แล้ว):

- **Single-instance lock**: ก่อน start server ให้ลอง connect เข้า port lock (`127.0.0.1:8766`) — ถ้า connect ได้ = มี instance อยู่แล้ว → **ไม่ต้องรัน server/sampler ใหม่** แค่ `webbrowser.open(url)` ของเดิมแล้ว exit (แก้ปัญหา double-click ซ้ำหลายแท็บ/ Sampler ซ้อน) — instance แรก bind port 8766 เป็น lock marker ตลอดอายุ
- **Sampler thread**: ลูปทุก 1 วินาที อ่านค่าจาก psutil แล้วเก็บใน dict กลาง (มี lock) — endpoint ไม่ต้อง block:
  - `psutil.cpu_percent()` → ภาพรวม CPU
  - `psutil.virtual_memory()` → RAM (%)
  - `psutil.disk_io_counters()` → คำนวณอัตรา read/write ต่อวินาที (delta) + `disk_usage(C:)` สำหรับ % เต็ม
  - `psutil.net_io_counters()` → คำนวณอัตรา send/recv ต่อวินาที (delta)
  - `psutil.sensors_battery()` → ระดับแบต + เสียบชาร์จอยู่ไหม (ถ้าเครื่องไม่มีแบต → `null`, ฝั่ง UI แสดง "AC" หรือซ่อน)
  - `psutil.process_iter(['pid','name','cpu_percent','memory_info'])` → top **6** โปรเซสตาม CPU% พร้อม **ตัวกรอง user-process**:
    - ตัดที่ name ว่าง/`System`/`Idle`/`Registry`/`svchost`/`csrss`/`wininit`/`services.exe`/`lsass`/`conhost`/`Memory Compression` และรายการที่เป็น kernel process (ppid=4 ยกเว้น names ที่รู้จัก)
    - ตัดตัวเอง (`pythonw.exe` ที่รัน pc_town) ออก
    - ถ้ากรองแล้วเหลือน้อยกว่า 6 → ใช้ตัวที่มี (ไม่ดัน system process กลับมา)
  - uptime (`psutil.boot_time()`), จำนวน process ทั้งหมด, อุณหภูมิ (`psutil.sensors_temperatures()` ถ้ามี — ไม่มีก็ไม่แสดง)
  - `cpu_percent()` ต่อ process ต้องมี round แรกเป็น bootstrap (ค่า 0) ก่อนเริ่มเก็บจริง
- **HTTP server** (`http.server.ThreadingHTTPServer` บน 127.0.0.1 เท่านั้น):
  - `GET /` → คืนหน้า HTML (string ที่ embed ไว้ในไฟล์)
  - `GET /api/status` → JSON snapshot ล่าสุด (จาก sampler)
  - `GET /api/ping` → heartbeat; sampler หลักตรวจว่าถ้าไม่มี ping เกิน 5 วิ → `os._exit(0)`
  - `POST /api/shutdown` → `os._exit(0)`
- **Port**: เริ่มที่ 8765 ถ้าถูกใช้ → +1 จนได้ (สูงสุด 8775) แล้ว `webbrowser.open(url)`
- **Error handling**:
  - psutil ไม่มี → try/except ImportError แล้วเขียน msg บอกวิธี `pip install psutil` ลงหน้า HTML อย่างน้อยก็ error page
  - browser ไม่เปิด → fallback: `os.startfile(url)`; ถ้ายัง fail ให้เขียน URL ไป `%LOCALAPPDATA%\PC_Town\last_url.txt`

### ฝั่งหน้าเว็บ (embedded ใน pc_town.py)

- **HTML5 Canvas + JavaScript ล้วน** (no libraries, no CDN — offline ได้ 100%)
- `requestAnimationFrame` ~60fps สำหรับวาด; `fetch('/api/status')` ทุก 1s สำหรับข้อมูล
- **HiDPI/Windows scaling (125%/150%)**: ตั้ง `canvas.width/height = CSS size × window.devicePixelRatio` แล้ว `ctx.scale(dpr, dpr)` + วาด pixel art บน offscreen canvas ย่อส่วนแบบ `imageSmoothingEnabled = false` → ขอบ pixel คมตลอด ไม่เบลอ
- **Smooth animation (กันกระตุกเพราะข้อมูลมาทีละ 1 วิ)**: ค่าที่มาจาก API (CPU% → target speed, ตำแหน่งเป้าหมาย) จะถูก **lerp/exponential smoothing** ในฝั่ง JS ทุก frame — ตัวละครเปลี่ยนความเร็วค่อยๆ ใน ~0.5–1 s, ตำแหน่งเดินต่อเนื่องแบบ frame-based ไม่กระโดดเมื่อ data patch เข้ามา
- โครง canvas 2 ชั้นแนวคิด: background (ตึก ถนน ต้นไม้ เมฆ — วาดครั้งเดียว/scroll ช้าๆ) + ตัวละคร + HUD

## Amendment v2 (2026-10-13, อนุมัติด้วยปากเปล่าหลังเห็นภาพจริงรอบแรก)

เปลี่ยนฉากจาก "ถนน side-view" เป็น **เมือง RPG top-down** (สไตล์ Pokémon/RPG Maker) ตาม feedback ผู้ใช้:
- แผนที่ tile 32px: หญ้าลายตาราง, ถนนดินต่อกันเป็นโครง graph, สระน้ำมุมล่างซ้าย, จัตุรัส มีน้ำพุ, ต้นไม้/ดอกไม้ประปราย
- บ้าน 6 หลังเรียงด้านบน มีป้าย "⌂" หน้าบ้าน; โรงงานขวาบนมีทางเดินเข้า
- ตัวละครเดิน **ตามโครง graph ของถนน** (เลือกต่อ node → node ไม่ทะลุหญ้า/น้ำ) หันหน้า/หลัง/ซ้าย/ขวา ตามทิศที่เดิน มีเงาใต้เท้า
- แก้ปัญหาเดิม: เส้นประถนนไม่แทงตัวละครอีก เพราะไม่มีถนนแบบ side-view แล้ว
- พฤติกรรมอื่น (ความเร็วตาม CPU%, fade ชื่อ, tooltip, lerp, HUD, ควัน=Disk, รถ→เปลี่ยนเป็น NPC เดินข้ามเมืองเป็นช่วงๆ=Network) คงเดิม — activity ของ network แสดงด้วย NPC ผู้เยี่ยมชมที่โผล่มาเดินบนถนนเป็นครั้งคราวเมื่อ net สูง

## การ map ข้อมูล → แอนิเมชัน (Core Design Decision)

### ตัวละคร (6 ตัว = top 6 processes)

- ตัวละคร 1 ตัวผูกกับ 1 **slot** (มี "บ้าน" ประจำ 6 หลังตามถนน) — ชื่อ process แสดงใต้ตัวละคร
-เมื่อ top 6 เปลี่ยนตัว → slot เดิมเรียกชื่อใหม่ (fade สั้นๆ) ไม่กระโดดตำแหน่งเพื่อไม่ให้มึน
- **ความเร็วการเคลื่อนที่ = CPU% ของ process**:
  | CPU% | พฤติกรรม |
  |---|---|
  | > 20% | วิ่ง (speed สูง, ฝุ่นตลบหลังเท้า) |
  | 5–20% | เดินเร็ว |
  | 1–5% | เดินช้า สลับหยุดเป็นพักๆ |
  | < 1% | ยืน idle, มีฟอง "z" ลอยเป็นระยะ |
- เดินไป-มาระหว่างบ้านกับจุดกลางถนน (สุ่มปลายทางเมื่อถึงที่) — idle ไม่ต้องเดิน
- **Hover ตัวละคร** → tooltip: ชื่อ process, CPU%, RAM (MB)
- **ขนาด/สีเสื้อตัวละคร**: ผูกกับ RAM ของ process นั้น (RAM เยอะ → ไซส์ใหญ่ขึ้น 1 ระดับ) เพื่อให้เห็นความต่างชัดเจน — แต่ห้ามใหญ่เกิน 1.3 เท่ากัน
- Sprite: วาดด้วยตาราง pixel arrays ในโค้ด (chibi หัวโต), 2 เฟรมเดิน + 1 เฟรมวิ่ง + 1 เฟรมยืน, palette สดใส 6 สีแยก 6 slot

### สิ่งแวดล้อม (ค่าระบบภาพรวม)

- **แถบ HUD ล่างจอ**: 6 meter — `CPU`, `RAM`, `DISK` (อัตรา read/write), `NET` (rate), `BATTERY` (หรือ "AC") พร้อมตัวเลข %
- บรรทัดเล็กมุมขวา: uptime, จำนวน process ทั้งหมด, อุณหภูมิ (ถ้ามี)
- **Disk activity** → ควัน/ฝอยออกจากปล่องตึก "Factory" (ตึกมุมขวา) ตามอัตรากับ threshold
- **Network** → รถวิ่งเข้า-ออกเมืองซ้าย/ขวา ความถี่ตาม rate รวม (send+recv)
- เมฆ/ท้องฟ้าเป็นฉากสวยๆ ไม่ผูกกับข้อมูล (ตัด scope ออก: ไม่มี day/night ตามแบต — YAGNI)

## Performance

- Sampler ทำงาน 1 Hz บน thread เดียว, psutil calls ใช้ CPU น้อยมาก (msec)
- Canvas วาด sprite ~6 ตัว + background เรียบง่าย → negligible
- App ตายตัวเองเมื่อไม่เปิดดู → **ไม่มีภาระตอนไม่ได้ใช้เลย**

## Testing

ทดสอบ manual (ไม่มี automated test เพราะเป็น visual app ไฟล์เดียว):

1. รัน `pythonw pc_town.py` → browser เปิด, เมืองแสดง, ตัวละครเดิน
2. รันสคริปต์ burn CPU ชั่วคราว (while True loop 1 เธรด) → ดูตัวละคร slot ของ python เทรดนั้นวิ่งเร็วขึ้น
3. เปิด/ปิดแท็บ → server ปิดตัวเองใน ~5 วิ (ตรวจด้วย Task Manager / `Get-Process pythonw`)
4. กดปุ่ม "ปิดแอป" ในหน้าเว็บ → server ปิดทันที
5. เครื่องตั้งโต๊ะ (ไม่มีแบต) → HUD battery ไม่ crash
6. Port 8765 ถูกจับอยู่ → ยังเปิดได้ (port +1)
7. ดับเบิลคลิก shortcut ซ้ำ 2–3 ครั้งขณะเปิดอยู่ → มี server เดียว, แค่เปิดแท็บเพิ่ม (lock ที่ 8766)
8. ตั้ง Windows display scale ที่ 125%/150% → pixel art ยังคม ไม่เบลอ
9. ดูตัวละครตอน CPU process ค่อยๆ ลด → ความเร็วลดนุ่มนวล ไม่มีกระตุกก้าวเดิน (lerp ทำงาน)
10. เปิด Task Manager เทียบ → ตัวละคร top 6 ตรงกับโปรเซสจริง (ไม่มี svchost/Idle แทรก)

## ขอบเขตที่ไม่ทำ (YAGNI)

- ไม่ทำ desktop overlay โปร่งใส / ไม่ทำระบบ tray icon
- ไม่เก็บประวัติ/กราฟย้อนหลัง (แสดง current state เท่านั้น)
- ไม่ localize หลายภาษา — UI ใช้ไทย/อังกฤษปนกันตามที่ระบุใน HUD
- ไม่มี auth (ผูก 127.0.0.1 เท่านั้น — ไม่ expose network)
