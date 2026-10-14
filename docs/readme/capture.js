/* บันทึกภาพหน้าจอ PC Town แบบต่อเนื่องผ่าน Chrome DevTools Protocol
   ใช้งาน:
     1) รันแอป:   set PC_TOWN_NO_BROWSER=1 && pythonw pc_town.py
     2) เปิด Edge headless:
        "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" ^
          --headless=new --remote-debugging-port=9222 --window-size=1280,720 ^
          "http://127.0.0.1:8765/"
     3) node docs/readme/capture.js   -> ได้ JPEG frame ใน docs/readme/frames/
     4) แปลงเป็น GIF:
        ffmpeg -y -framerate 15 -i docs/readme/frames/f%03d.jpeg ^
          -vf "fps=10,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse" ^
          docs/readme/demo.gif
*/
const fs = require('fs'), path = require('path');

const PORT = 9222, DURATION_MS = 12000, OUT = path.join(__dirname, 'frames');
fs.mkdirSync(OUT, { recursive: true });

async function main() {
  const tabs = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
  const page = tabs.find(t => t.url.includes('127.0.0.1:8765'));
  if (!page) throw new Error('ไม่เจอแท็บ PC Town ใน Edge headless');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0, n = 0;
  const send = (method, params = {}) =>
    ws.send(JSON.stringify({ id: ++id, method, params }));

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.method === 'Page.screencastFrame') {
      const f = path.join(OUT, `f${String(n++).padStart(3, '0')}.jpeg`);
      fs.writeFileSync(f, Buffer.from(msg.params.data, 'base64'));
      send('Page.screencastFrameAck', { sessionId: msg.params.sessionId });
    }
  };
  await new Promise(r => ws.onopen = r);
  send('Page.enable');
  send('Page.bringToFront');
  send('Page.startScreencast', { format: 'jpeg', quality: 85, maxWidth: 1280, everyNthFrame: 2 });
  await new Promise(r => setTimeout(r, DURATION_MS));
  send('Page.stopScreencast');
  await new Promise(r => setTimeout(r, 500));
  ws.close();
  console.log(`captured ${n} frames -> ${OUT}`);
}
main().catch(e => { console.error(e.message); process.exit(1); });
