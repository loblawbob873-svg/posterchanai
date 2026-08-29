'use strict';
const fs=require('fs'),path=require('path');
const src=fs.readFileSync(path.resolve(__dirname,'../../static/js/client/os.js'),'utf8');
const start=src.indexOf('  function taskbarMove(w){');
const end=src.indexOf('  function nativeTaskbarMove(row)',start);
if(start<0||end<0)throw new Error('taskbarMove implementation missing');
const listeners={};
global.document={
  addEventListener(k,f){(listeners[k]||(listeners[k]=[])).push(f);},
  removeEventListener(k,f){listeners[k]=(listeners[k]||[]).filter(x=>x!==f);},
};
let _natFocusHold=false,focusHeld=false;
const gestures=[];
const focusWin=()=>{focusHeld=_natFocusHold;};
const _natGesture=(_w,on)=>{gestures.push(on);if(!on)_natFocusHold=false;};
const zf=()=>1,vwL=()=>1200,vhL=()=>800,TASKBAR=48,MIN_W=320,MIN_H=220;
const keepFrameReachable=()=>{},unsnap=()=>{},snapTo=()=>{};
eval(src.slice(start,end)+'\nglobalThis.__taskbarMove=taskbarMove;');
const classes=new Set();
const w={native:41,min:false,snap:null,el:{offsetWidth:400,offsetHeight:300,
  style:{left:'20px',top:'30px'},classList:{add:(...x)=>x.forEach(v=>classes.add(v)),
  remove:(...x)=>x.forEach(v=>classes.delete(v))}}};
__taskbarMove(w);
setTimeout(()=>{
  if(!focusHeld)throw new Error('native compositor focus happened before gesture hold');
  const move=(listeners.pointermove||[])[0],place=(listeners.pointerdown||[])[0];
  if(!move||!place)throw new Error('pointer gesture was not armed');
  move({clientX:700,clientY:400});
  place({preventDefault(){},stopPropagation(){}});
  if(w.el.style.left!=='500px'||w.el.style.top!=='376px')throw new Error('pointer did not move frame');
  if(String(gestures)!=='true,false')throw new Error('native gesture did not commit: '+gestures);
  if(_natFocusHold)throw new Error('native focus hold was not released');
  console.log('native taskbar Move holds');
},5);
