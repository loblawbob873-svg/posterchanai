/* Local performance readings for PosterChanOS Task Manager and desktop widgets. */
'use strict';
const fs = require('fs');
const os = require('os');
const posterfetch = require('./posterfetch.js');

let last = null;
let processLast = new Map(), processAt = 0;
const read = p => fs.readFileSync(p, 'utf8');
function counters(){
  const c=(read('/proc/stat').match(/^cpu\s+(.+)$/m)||[])[1]||'';
  const n=c.trim().split(/\s+/).map(Number), idle=(n[3]||0)+(n[4]||0), total=n.reduce((a,b)=>a+b,0);
  let rx=0,tx=0;
  for(const line of read('/proc/net/dev').split('\n').slice(2)){ const m=/^\s*([^:]+):\s*(\d+)(?:\s+\d+){7}\s+(\d+)/.exec(line); if(!m||m[1].trim()==='lo')continue;rx+=Number(m[2]);tx+=Number(m[3]); }
  return {at:Date.now(),idle,total,rx,tx};
}
function memory(){
  const m={}; for(const x of read('/proc/meminfo').matchAll(/^(\w+):\s+(\d+)/gm))m[x[1]]=Number(x[2])*1024;
  const total=m.MemTotal||0, available=m.MemAvailable||0;
  return {total,used:Math.max(0,total-available),percent:total?Math.round((total-available)*100/total):0};
}
function processes(){
  const uid=process.getuid?process.getuid():null, clk=100;
  const out=[];
  for(const name of fs.readdirSync('/proc')){
    if(!/^\d+$/.test(name))continue;
    try{
      const stat=read('/proc/'+name+'/stat'), close=stat.lastIndexOf(')'), tail=stat.slice(close+2).split(' ');
      const status=read('/proc/'+name+'/status'), u=Number((/^Uid:\s+(\d+)/m.exec(status)||[])[1]);
      if(uid!==null&&u!==uid)continue;
      const rss=Number((/^VmRSS:\s+(\d+)/m.exec(status)||[])[1]||0)*1024;
      const cmd=read('/proc/'+name+'/cmdline').replace(/\0/g,' ').trim();
      out.push({pid:Number(name),name:stat.slice(stat.indexOf('(')+1,close),cmd,rss,
                cpuSeconds:((Number(tail[11])||0)+(Number(tail[12])||0))/clk});
    }catch(_){}
  }
  const now=Date.now(), dt=processAt?Math.max(.001,(now-processAt)/1000):0;
  const cores=Math.max(1,os.cpus().length), next=new Map();
  for(const p of out){
    const before=processLast.get(p.pid); next.set(p.pid,p.cpuSeconds);
    p.cpu=before!==undefined&&dt?Math.max(0,Math.min(100,(p.cpuSeconds-before)*100/dt/cores)):0;
  }
  processLast=next; processAt=now;
  return out.sort((a,b)=>b.cpu-a.cpu||b.rss-a.rss).slice(0,300);
}
function snapshot(withProcesses){
  const cur=counters(), prev=last; last=cur;
  const dt=prev?Math.max(.001,(cur.at-prev.at)/1000):0;
  const dtotal=prev?cur.total-prev.total:0, didle=prev?cur.idle-prev.idle:0;
  return {at:cur.at,cpu:{percent:dtotal?Math.max(0,Math.min(100,Math.round((dtotal-didle)*100/dtotal))):0,
                         cores:os.cpus().length},memory:memory(),network:{rx:dt?(cur.rx-prev.rx)/dt:0,tx:dt?(cur.tx-prev.tx)/dt:0},
          gpu:posterfetch.gpu(),uptime:os.uptime(),processes:withProcesses?processes():[]};
}
function end(pid){
  const n=Number(pid); if(!Number.isInteger(n)||n<=1)throw new Error('invalid process');
  const status=read('/proc/'+n+'/status'), uid=Number((/^Uid:\s+(\d+)/m.exec(status)||[])[1]);
  if(process.getuid&&uid!==process.getuid())throw new Error('can only end your own processes');
  process.kill(n,'SIGTERM'); return {ok:true,pid:n};
}
module.exports={snapshot,end};
