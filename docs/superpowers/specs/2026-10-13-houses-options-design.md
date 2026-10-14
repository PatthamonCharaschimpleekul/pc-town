# Personalized Houses + Options Menu — Design Spec

วันที่: 2026-10-13 (อนุมัติด้วยปากเปล่า "ok")
สถานะ: รอ user review ฉบับเขียน

ฟีเจอร์ต่อจาก PC Town (ดู `2026-10-13-pc-town-design.md` + Amendment v2)

## ปัญหา / เป้าหมาย

บ้านทั้ง 6 หลังเหมือนกันหมดและกดอะไรไม่ได้ ผู้ใช้ต้องการ:
1. บ้านมีหน้าตา**เฉพาะตัวตามชนิดแอป**ที่อาศัยอยู่ (บราวเซอร์/เพลง/เกม/dev/แชท/อื่น)
2. **คลิก** บ้านหรือตัวละคร → เมนู option: ดูรายละเอียด+กราฟ 30 วิ, ปักหมุด (pin), ซ่อน (hide), ปิดแอป (kill จริง พร้อมกดยืนยันสองชั้น)

## สถาปัตยกรรม (ตัดสินใจแล้ว: สมองอยู่ฝั่ง Python)

State ทั้งหมดอยู่ใน `Sampler` ฝั่ง Python; browser เป็นตัววาด/ส่งคำสั่ง

### Python

- `self.pinned: set[str]`, `self.hidden: set[str]` (key = ชื่อ process ตัวพิมพ์เล็ก), `self.history: dict[str, collections.deque]` deque maxlen 30 เก็บ `(cpu, memMB)` ต่อวินาทีเฉพาะ procs ที่กำลังแสดง
- `select_procs(all_infos, pinned, hidden, top_n)` (แยกเป็น pure function เพื่อทดสอบ):
  1. ตัดชื่อที่อยู่ใน `hidden`
  2. รายการ `pinned` ที่ยังทำงานอยู่ (หา pid จาก all_infos) เข้า slot ก่อน เรียงตาม CPU
  3. เติม procs ที่เหลือตาม CPU% จนครบ top_n
- `top_procs()` เรียก `select_procs` แล้ว append `hist` ต่อ proc = list ของ cpu 30 ค่าล่าสุด (สั้นได้ถ้าเพิ่งโผล่)
- snapshot JSON เพิ่ม keys: `pinned` (list ชื่อ), `hidden` (list ชื่อ) — proc เดิมมี `hist` เพิ่ม
- Endpoints ใหม่ (`POST`, JSON body):
  - `/api/pin {name, on}` — on จริงและ pinned ครบ 6 แล้ว → คืน `{"error":"max 6"}`; on=จริงแต่ชื่อไม่อยู่ใน snapshot ล่าสุด → `{"error":"gone"}`
  - `/api/hide {name, on}` — ไม่มีเพดาน
  - `/api/kill {name, pid}` — **ต้องเช็คสองชั้น**: pid ยังอยู่ใน snapshot ล่าสุด และ name ตรงกัน → `psutil.Process(pid).terminate()`; `AccessDenied` → `{"error":"access denied"}`; pid หาย → `{"error":"gone"}`
  - ทุก endpoint คืน `{"ok":true}` เมื่อสำเร็จ
- pin/hide/history เป็น memory เท่านั้น — restart เมืองเริ่มใหม่ (YAGNI: ไม่เก็บไฟล์)

### Frontend (JS ใน INDEX_HTML)

**หมวดบ้าน** `categoryOf(name)`: regex ลำดับแรกที่ถูก
- browser: `chrome|msedge|firefox|opera|brave|vivaldi`
- media: `spotify|vlc|itunes|foobar|aimp|wmplayer|audacity|deezer`
- game: `steam|epic|riot|wegame|armoury|minecraft|game|xbox|playgame`
- dev: `code|pycharm|idea|node|python|git|docker|java|electron|cursor`
- chat: `line|discord|telegram|weixin|wechat|messenger|teams|slack|zoom`
- อื่น → `other`

หน้าตาต่ําแหน่ง slot (วาด per-frame ทับ base ที่ bake ไว้ เพราะชนิดเปลี่ยนได้):
- browser: จานดาวเทียมบนหลังคา + ช่องหน้าต่างสีฟ้าเข้มขึ้น
- media: ลำโพงสี่เหลี่ยมสองข้างประตู + โน้ต `♪` ลอยเอื่อย (อนิเมะสดใส)
- game: หลังคาเปลี่ยนเป็นยอดสามเหลี่ยมสีเข้ม + ป้าย 🎮 หน้าบ้าน
- dev: ปล่องเล็ก + ไฟหน้าต่างกะพริบสลับ (โทนเขียว editor)
- chat: เสาอากาศ + ซองจดหมาย `✉` ลอยขึ้นเป็นครั้งคราว
- other: แบบเดิม
- slot ว่าง (ไม่มี proc): หลังคามืด, ประตูเปิด, ไม่มีป้ายชื่อ

**เมนู panel** (div ลอย `#menu`):
- เปิดเมื่อคลิกในบ้าน/ตัวละคร slot ใด; มี: ชื่อ, ไอคอนหมวด, CPU/RAM ปัจจุบัน, sparkline แท่ง CPU 30 วิ (canvas เล็ก, วาดจาก `hist`), ปุ่ม 📌 pin (toggle, เต็ม 6 → toast), 👁 hide, ☠ ปิดแอป → ปุ่มเปลี่ยนเป็น "แน่ไหม? กดอีกที" (reset ใน 3 วิ), ✖ ปิด panel
- ข้อมูลใน panel refresh ทุก poll
- กระดุม `#hiddenBtn` มุมล่างซ้าย "ซ่อนไว้ (n)" → รายชื่อที่ซ่อน + unhide แต่ละตัว
- toast เล็กมุมล่างกลาง ( reuse สไตล์ HUD) ข้อความ error จาก API เช่น "max 6", "access denied"
- คลิกที่ว่าง = ปิด panel; Esc = ปิด

## Data flow

```
click slot i → panel เปิด (จาก DATA.procs[i] ล่าสุด)
กดปุ่ม → POST /api/{pin|hide|kill} → Sampler เปลี่ยน state → 1 วิหลัง /api/status สะท้อนผล → panel/เมือง redraw
```

## Error handling

- kill เป้าหายระหว่างทาง → `{"error":"gone"}` → toast "แอปปิดไปแล้ว"
- AccessDenied → toast "ปิดไม่ได้ (สิทธิ์ไม่พอ)"
- ชื่อ proc มี `\\` หรือตัวอักษรแปลก → ใช้เป็น JSON key ปกติ (ไม่ใช้ path) — endpoint รับจาก body เท่านั้น
- panel เปิดอยู่แต่ slot ว่างลง (proc ตาย) → ปิด panel อัตโนมัติ

## Testing

unit (pytest, `tests/test_town_state.py`):
1. `select_procs` ตัด hidden, เรียง pinned ก่อน, เติมตาม CPU, ไม่เกิน top_n
2. `select_procs` เมื่อ pinned หมดสภาพ (process ตาย) → slot เติมด้วยตัวอื่น
3. history ring buffer: ค่าเกิน 30 → ตัดหัว, proc หาย → key ยังเก็บไว้ ≤ N ตัว (guard เมม)

manual (ทดสอบด้วยสายตา/ของจริง):
4. เปิดแอปแต่ละหมวด → บ้านเปลี่ยนหน้าตาถูก (ทดสอบด้วย msedge/chrome/spotify/code/line ฯลฯ เท่าที่มีจริงในเครื่อง; Armoury Crate → game, Weixin → chat, RadeonSoftware → other ตาม rule)
5. pin บ้าน python (รัน burn) → หยุด burn ชั่วคราว (python ยังทำงาน) → อยู่นิ่ง idle ช่องเดิม ได้; ถ้า python **จบการทำงานจริง** → no process to pin → slot ว่างแล้วถูกเติมด้วยโปรเซสถัดไป (พฤติกรรมตั้งใจ)
6. hide → บ้าน slot นั้นถูก dim (เงามืด+ประตูเปิด เหมือนไม่มีคนอยู่ — ตัดสินใจแก้จาก "สวน" เพื่อความง่ายและตรงกับกรณี slot ว่าง), unhide จากปุ่มกลับมา
7. kill notepad ที่เปิดไว้ → ต้องกดยืนยัน 2 ครั้ง → notepad หายจริง
8. graph 30 วิเคลื่อนตามจริง
9. suite เดิม (test_core/test_server) ยังผ่าน

## ขอบเขตที่ไม่ทำ (YAGNI)

- ไม่ persist pin/hide ข้ามรอบเปิด-ปิด
- ไม่เปิด window ใหม่/bring to front, ไม่ set priority — kill อย่างเดียว
- ไม่มีเสียง, ไม่เปลี่ยนธีมเมืองทั้งใบตามหมวด, slot มีเท่าเดิม (6)
