// DOM shim เพื่อรัน JS ของหน้าเว็บใน node
function fakeEl(){ 
  const el = { style:{}, innerHTML:'',
    getContext: () => new Proxy({ measureText: () => ({width:50}),
      canvas: {width:960,height:540} },
      { get(t,k){ if(k in t) return t[k]; return (...a)=>undefined; },
        set(t,k,v){ t[k]=v; return true; } }),
    onclick:null, };
  return el;
}
const els = {};
global.document = {
  getElementById: id => (els[id] = els[id] || fakeEl()),
  createElement: () => fakeEl(),
  querySelectorAll: () => [],
};
global.window = global;
global.devicePixelRatio = 1; global.innerWidth = 1280; global.innerHeight = 720;
let rafCb = null;
global.requestAnimationFrame = cb => { rafCb = cb; };
const STATUS = { cpu:30, ram:55, disk_full:70, disk_r:5e6, disk_w:3e6, net_rx:9e5, net_tx:4e5,
  battery:{pct:80,plugged:true}, proc_count:300, uptime:9000, temp:60,
  procs:[
    {name:'chrome.exe',cpu:35,memMB:900},{name:'Code.exe',cpu:12,memMB:350},
    {name:'Discord.exe',cpu:3,memMB:120},{name:'spotify.exe',cpu:0.2,memMB:80},
    {name:'explorer.exe',cpu:1.5,memMB:40},{name:'weixin.exe',cpu:0,memMB:300}] };
global.fetch = async url => ({ json: async () => STATUS });
global.addEventListener = (ev,fn) => { if(ev==='resize') fn(); };
const fs = require('fs');
eval(fs.readFileSync('D:/PC_Town/_app.js','utf8'));
// pump: รอ poll() async เสร็จ แล้วขับ frame หลายรอบ
setTimeout(() => {
  try{
    let t = 0;
    for(let i=0;i<10;i++){ t += 16; const cb = rafCb; rafCb = null; cb(t); }
    console.log('RAN 10 FRAMES WITHOUT ERROR');
  }catch(e){ console.log('RUNTIME ERROR:', e.message, '\nstack:', e.stack.split('\n').slice(0,4).join('\n')); }
}, 50);
