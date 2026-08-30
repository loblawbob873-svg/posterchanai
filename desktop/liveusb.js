/* Build and write PosterChanOS live media without turning the Settings page into a root shell. */
'use strict';
const { execFile, spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const LSBLK = process.env.PC_LSBLK || 'lsblk';
const SUDO = process.env.PC_SUDO || 'sudo';
let job = { kind:'', running:false, ok:false, message:'', started:0, finished:0, output:'', path:'' };
const STATE_DIR = process.env.PC_LIVEUSB_STATE_DIR || path.join(process.env.XDG_STATE_HOME || path.join(process.env.HOME||'', '.local/state'), 'posterchan');
const STATE_FILE = path.join(STATE_DIR, 'liveusb-job.json');
const LOG_FILE = path.join(STATE_DIR, 'liveusb-job.log');

function save(){try{fs.mkdirSync(STATE_DIR,{recursive:true,mode:0o700});const tmp=STATE_FILE+'.new';
  fs.writeFileSync(tmp,JSON.stringify(job),{mode:0o600});fs.renameSync(tmp,STATE_FILE);}catch(_){}}
function alive(pid){if(!Number.isInteger(+pid)||+pid<2)return false;try{process.kill(+pid,0);return true;}catch(_){return false;}}
function recover(){try{const old=JSON.parse(fs.readFileSync(STATE_FILE,'utf8'));if(!old||!old.kind)return;
  old.running=alive(old.pid);
  if(!old.running&&!old.finished){old.finished=Date.now();old.ok=old.kind==='build'&&!!old.path&&fs.existsSync(old.path);
    old.message=old.ok?'ISO finished':old.kind+' stopped before it finished';}
  job=Object.assign(job,old);
}catch(_){}}

function run(bin,args,ms){ return new Promise((resolve,reject)=>execFile(bin,args,{timeout:ms||8000},(e,out,err)=>{
  if(e)return reject(new Error(String(err||e.message||e).trim().split('\n').pop())); resolve(String(out||''));
})); }
function flatten(rows,out=[]){ for(const r of rows||[]){out.push(r);flatten(r.children,out);} return out; }

async function devices(){
  const raw=await run(LSBLK,['-J','-b','-o','NAME,PATH,TYPE,SIZE,RM,TRAN,MOUNTPOINTS,MODEL']);
  const roots=JSON.parse(raw).blockdevices||[], all=flatten(roots);
  const root=all.find(x=>(x.mountpoints||[]).includes('/'));
  return roots.filter(x=>x.type==='disk' && (Number(x.rm)===1 || x.tran==='usb'))
    .filter(x=>!root || (root.path!==x.path && !flatten([x]).some(y=>y.path===root.path)))
    .map(x=>({path:x.path,name:x.name,size:Number(x.size)||0,model:String(x.model||'').trim(),
      mounted:flatten([x]).some(y=>(y.mountpoints||[]).some(Boolean))}));
}
function launch(kind,bin,args,env,meta){
  recover();
  if(job.running)throw new Error('another LiveUSB job is already running');
  job=Object.assign({kind,running:true,ok:false,message:kind==='build'?'Building ISO…':'Writing USB…',started:Date.now(),finished:0,output:'',path:''},meta||{});
  fs.mkdirSync(STATE_DIR,{recursive:true,mode:0o700});
  const fd=fs.openSync(LOG_FILE,'w',0o600);
  /* The ISO build outlives this Electron renderer and even a package-triggered desktop restart.
   * Pipes die with their parent and can SIGPIPE a healthy mksquashfs; a detached process writing a
   * state-owned log has neither failure mode. */
  const p=spawn(bin,args,{env:Object.assign({},process.env,env||{}),stdio:['ignore',fd,fd],detached:true});
  fs.closeSync(fd);job.pid=p.pid;save();p.unref();
  p.on('error',e=>{job.running=false;job.finished=Date.now();job.message=e.message;save();});
  p.on('close',code=>{job.running=false;job.finished=Date.now();job.ok=code===0;
    job.message=code===0?(kind==='build'?'ISO finished':'USB finished — eject it safely'):(kind+' failed (exit '+code+')');save();});
  return status();
}
function build(outDir,includeHome){
  if(!String(outDir||'').trim())throw new Error('choose an existing output folder');
  const out=path.resolve(String(outDir));
  if(!fs.existsSync(out)||!fs.statSync(out).isDirectory())throw new Error('choose an existing output folder');
  fs.accessSync(out,fs.constants.W_OK);
  const now=new Date(), stamp=String(now.getFullYear())+String(now.getMonth()+1).padStart(2,'0')+String(now.getDate()).padStart(2,'0');
  const image=path.join(out,'posterchan-live-'+stamp+'.iso');
  return launch('build',SUDO,['-n','env','PC_ISO_OUT='+out,'PC_ISO_HOME='+(includeHome?'y':'n'),
    /* Settings builds an artifact for the boot/install/reboot gates and USB writer. Publication is
     * a separate release action; gentoo.sh otherwise defaults a clean image to an immediate upload
     * before any of those runtime gates have exercised it. */
    'PC_ISO_CLEAN='+(includeHome?'n':'y'),'PC_ISO_PUBLISH=n',
    '/usr/bin/gentoo.sh','livecd'],null,{path:image});
}
async function burn(iso,target){
  const image=path.resolve(String(iso||''));
  if(!fs.existsSync(image)||!fs.statSync(image).isFile()||!image.toLowerCase().endsWith('.iso'))throw new Error('choose an ISO file');
  const allowed=(await devices()).find(x=>x.path===target);
  if(!allowed)throw new Error('the selected removable disk is no longer available');
  if(allowed.mounted)throw new Error('eject or unmount every partition on '+target+' first');
  return launch('burn',SUDO,['-n','dd','if='+image,'of='+target,'bs=4M','iflag=fullblock','oflag=direct','conv=fsync','status=progress']);
}
function status(){recover();try{job.output=fs.readFileSync(LOG_FILE,'utf8').slice(-24000);}catch(_){}
  return Object.assign({},job);}
module.exports={devices,build,burn,status,flatten};
