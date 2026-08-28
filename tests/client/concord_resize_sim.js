'use strict';
const fs=require('fs');
const path=require('path');
const source=fs.readFileSync(path.resolve(__dirname,'../../static/js/client/os.js'),'utf8');
const start=source.indexOf('  function startResize(w, ev){');
const end=source.indexOf('\n  // ---- desktop, taskbar',start);
if(start<0||end<0)throw new Error('resize implementation missing');

const listeners={};
global.document={
  addEventListener:(name,fn)=>{listeners[name]=fn;},
  removeEventListener:(name,fn)=>{if(listeners[name]===fn)delete listeners[name];},
};
global.window={addEventListener(){},removeEventListener(){}};
global.requestAnimationFrame=fn=>{fn();return 1;};
global.cancelAnimationFrame=()=>{};
const MIN_W=420,MIN_H=260,TASKBAR=48;
const vwL=()=>1200,vhL=()=>800,zf=()=>1,nativeWins=()=>[];
const keepFrameReachable=w=>{w.reachable=true;};
const _natGesture=(w,on)=>{w.gestures.push(on);};
let restores=0;
const toggleMax=w=>{
  restores++;
  w.max=false;w.snap=null;
  Object.assign(w.el.style,{left:'100px',top:'80px',width:'700px',height:'500px'});
  w.el.offsetWidth=700;w.el.offsetHeight=500;
};
eval(source.slice(start,end)+'\nglobalThis.__startResize=startResize;');

const element={style:{left:'0px',top:'0px',width:'1200px',height:'752px'},offsetWidth:1200,
  offsetHeight:752,setPointerCapture(){this.captured=true;},hasPointerCapture(){return true;},
  releasePointerCapture(){this.released=true;},addEventListener(){},removeEventListener(){},
  classList:{remove(){}}};
const win={max:true,snap:'max',el:element,gestures:[]};
let prevented=false;
__startResize(win,{clientX:800,clientY:580,pointerId:4,buttons:1,pointerType:'mouse',preventDefault(){prevented=true;}});
if(restores!==1||win.max||win.snap)throw new Error('maximised Concord grip did not restore floating state');
if(!prevented||!element.captured||typeof listeners.pointermove!=='function')throw new Error('restored grip did not continue the resize gesture');
listeners.pointermove({clientX:900,clientY:630,pointerType:'mouse',buttons:1});
if(element.style.width!=='800px'||element.style.height!=='550px')throw new Error('same grip gesture did not resize restored Concord window');
listeners.pointerup({});
if(!element.released||!win.reachable||win.gestures.join(',')!=='true,false')throw new Error('restored resize did not commit cleanly');
console.log('concord maximised resize flow ok');
