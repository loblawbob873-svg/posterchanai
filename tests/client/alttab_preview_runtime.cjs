'use strict';
/* Runs the SHIPPED `pc:wm:preview` handler (desktop/main.js) against a fake window manager with
 * windows on TWO monitors. Reported: "alt+tab communities, social, git showed no preview … email
 * showed no preview either" — Alt+Tab lists every monitor's windows, and the handler looked for the
 * window only among the ASKING monitor's, so every card from the other screen was blank. */
const fs=require('fs'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'..','..','desktop','main.js'),'utf8');
const start=src.indexOf("ipcMain.handle('pc:wm:preview', async (e, id) => {");
if(start<0) throw new Error('no pc:wm:preview handler');
let depth=0,i=src.indexOf('{',start),end=-1;
for(;i<src.length;i++){ if(src[i]==='{')depth++; else if(src[i]==='}'){ depth--; if(!depth){ end=i; break; } } }
const body=src.slice(src.indexOf('async (e, id) =>',start), end+1);
const thumb=src.slice(src.indexOf('function previewThumb('), src.indexOf('ipcMain.handle(\'pc:wm:preview\''));
function ok(n,v){ if(!v) throw new Error(n); console.log('  ok   '+n); }
const rows=[{id:1,workspace:'DP-1',rect:{x:0,y:0,width:800,height:600}},{id:2,workspace:'DP-2',rect:{x:3840,y:0,width:800,height:600}}];
let captured=[];
const env={
  fsGuard(){}, wm(){ return { windows:async()=>rows, captureView:async id=>{ captured.push(id); return 'data:image/png;base64,BIG'+id; } }; },
  scopedWindows(e,all){ return all.filter(r=>r.workspace===e.out); },
  require(m){ if(m==='electron') return { nativeImage:{ createFromDataURL:u=>({ isEmpty:()=>false, getSize:()=>({width:2000,height:1500}),
      resize:o=>({ toJPEG:q=>Buffer.from('small'+o.width) }), toJPEG:()=>Buffer.from('full') }) } }; return require(m); },
};
const handler=new Function(...Object.keys(env), thumb+'\nreturn ('+body+');')(...Object.values(env));
(async()=>{
  const here=await handler({out:'DP-1'},1), there=await handler({out:'DP-1'},2);
  ok('a window on the asking monitor gets a picture', here && here.startsWith('data:image/jpeg;base64,'));
  ok('a window on the OTHER monitor gets a picture too', there && there.startsWith('data:image/jpeg;base64,'));
  ok('the picture is a thumbnail, not the full-size capture',
     Buffer.from(there.split(',')[1],'base64').toString()==='small560');
  ok('an unknown window gets nothing', (await handler({out:'DP-1'},99))==='');
  console.log('OK alttab preview');
})().catch(e=>{ console.error(e); process.exit(1); });
