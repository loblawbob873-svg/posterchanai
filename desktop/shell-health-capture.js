'use strict';
const fs=require('fs');
const path=require('path');

// An on-demand, private renderer readback for startup health when another window covers it.
// Requests identify an exact owned compositor view and a fresh nonce; no arbitrary paths or IPC
// renderer caller can choose a capture destination. No focus/visibility operations are performed.
function armShellHealthCapture(target,{runtime,pid=process.pid,viewId,interval=250}){
  if(!runtime || !path.isAbsolute(runtime))return ()=>{};
  const directory=path.join(runtime,'posterchan-shell-health-'+pid);
  try{
    fs.mkdirSync(directory,{mode:0o700});
  }catch(e){if(e.code!=='EEXIST')return ()=>{};}
  try{const st=fs.lstatSync(directory);if(!st.isDirectory()||st.isSymbolicLink()||st.uid!==process.getuid()||(st.mode&0o077))return ()=>{};}
  catch(_){return ()=>{};}
  let stopped=false,busy=false;
  const files=new Set();
  const read=(file)=>{const st=fs.lstatSync(file);if(!st.isFile()||st.isSymbolicLink()||st.uid!==process.getuid()||(st.mode&0o077)||st.size>512)throw Error('invalid health request');return JSON.parse(fs.readFileSync(file,'utf8'));};
  const poll=async()=>{
    if(stopped||busy||target.isDestroyed())return;
    const id=Number(viewId());if(!Number.isSafeInteger(id)||id<=0)return;
    const request=path.join(directory,'request-'+id+'.json');
    let value;try{value=read(request);}catch(_){return;}
    if(value.pid!==pid||value.viewId!==id||!/^[0-9a-f]{32}$/.test(value.nonce||''))return;
    const dest=path.join(directory,id+'-'+value.nonce+'.png'),temp=dest+'.tmp';
    if(files.has(dest))return;
    busy=true;files.add(request);files.add(dest);files.add(temp);
    try{
      const image=await target.webContents.capturePage({x:0,y:0,width:96,height:96});
      // Navigation/destruction or a newer request while capture awaited invalidates this response.
      if(stopped||target.isDestroyed()||Number(viewId())!==id||read(request).nonce!==value.nonce)return;
      const png=image.toPNG();
      if(!Buffer.isBuffer(png)||png.length<8||png.length>1024*1024||!png.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])))return;
      fs.writeFileSync(temp,png,{mode:0o600,flag:'wx'});fs.renameSync(temp,dest);
    }catch(_){}finally{busy=false;}
  };
  const timer=setInterval(poll,interval);if(timer.unref)timer.unref();
  const stop=()=>{stopped=true;clearInterval(timer);for(const file of files)try{fs.unlinkSync(file);}catch(_){}try{fs.rmdirSync(directory);}catch(_){};};
  if(typeof target.once==='function')target.once('closed',stop);
  return stop;
}
module.exports={armShellHealthCapture};
