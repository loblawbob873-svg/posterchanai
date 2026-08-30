/* Detached LiveUSB supervisor. Electron may restart while an ISO is packing; this process owns the
 * real child until it exits and atomically records the actual result for the replacement UI. */
'use strict';
const {spawnSync}=require('child_process');
const fs=require('fs');

function write(file,value){const tmp=file+'.'+process.pid+'.new';fs.writeFileSync(tmp,JSON.stringify(value),{mode:0o600});fs.renameSync(tmp,file);}
function read(file){try{return JSON.parse(fs.readFileSync(file,'utf8'));}catch(_){return null;}}
function unlock(file,token){try{if(fs.readFileSync(file,'utf8')===token)fs.unlinkSync(file);}catch(_){}}
function sleep(ms){Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,ms);}

let spec;
try{spec=JSON.parse(Buffer.from(String(process.argv[2]||''),'base64').toString('utf8'));}
catch(_){process.exit(125);}
const fd=fs.openSync(spec.log,'a',0o600);
let code=125,error='';
try{
  /* Parent publishes this supervisor's PID before we can complete. This handshake removes the
   * fast-exit race where the parent used to overwrite our terminal result with running=true. */
  const deadline=Date.now()+5000;
  while(Date.now()<deadline){const s=read(spec.state);let lock='';try{lock=fs.readFileSync(spec.lock,'utf8');}catch(_){}
    if(s&&s.token===spec.token&&s.pid===process.pid&&s.running&&lock===spec.token)break;sleep(10);}
  const claimed=read(spec.state);
  let lock='';try{lock=fs.readFileSync(spec.lock,'utf8');}catch(_){}
  if(!claimed||claimed.token!==spec.token||claimed.pid!==process.pid||!claimed.running||lock!==spec.token)
    throw new Error('LiveUSB supervisor was not claimed');
  const childEnv=Object.assign({},process.env,spec.env||{});
  delete childEnv.ELECTRON_RUN_AS_NODE;
  const r=spawnSync(spec.bin,spec.args,{env:childEnv,stdio:['ignore',fd,fd]});
  code=Number.isInteger(r.status)?r.status:125;
  if(r.error)error=String(r.error.message||r.error);
}catch(e){error=String(e&&e.message||e);}
try{fs.closeSync(fd);}catch(_){}
try{
  const old=read(spec.state)||{};
  if(old.token===spec.token){
    const ok=code===0;
    Object.assign(old,{running:false,launching:false,ok,exitCode:code,finished:Date.now(),error,
      message:ok?(spec.kind==='build'?'ISO finished':'USB finished — eject it safely')
                :(error||spec.kind+' failed (exit '+code+')')});
    write(spec.state,old);
  }
}finally{unlock(spec.lock,spec.token);}
process.exit(code===0?0:1);
