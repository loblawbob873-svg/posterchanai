"""desktop/server.js — the bridge between System Settings and the root helper, RUN under node.

The bridge is the last thing between a renderer and a NOPASSWD sudo rule, so what it must never do is
build a command out of what it was sent: every call has to be exactly `sudo -n /usr/local/bin/pc-server
<one listed verb>`, an unknown AI feature must be refused before anything runs, and sudo's refusal must come
back as a sentence rather than as "a password is required" to somebody who has no Unix password.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_every_call_is_one_listed_verb_of_the_helper_and_nothing_else():
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const calls=[];let deny=false,helperThere=true,listening=true,statusJson=null;
const execFile=(bin,args,opts,cb)=>{
  calls.push([bin,...args]);
  assert.equal(opts.env.LC_ALL,'C'); assert.ok(opts.timeout>0);
  if(deny)return cb(new Error('exit 1'),'','sudo: a password is required\n');
  if(args[2]==='status')return cb(null,statusJson||JSON.stringify({code:true,configured:true,venv:true,
    enabled:'enabled',active:'active',port:3051,relayPort:3052,nostrOnly:true,postgres:{slot:'18',active:'active'},
    features:{ai:false},job:{running:false,verb:'',rc:'',started:'',finished:''}}),'');
  if(args[2]==='job')return cb(null,'{"running":true,"verb":"enable","rc":""}\n','');
  if(args[2]==='logs')return cb(null,'journal\n','');
  if(args[2]==='install-ai'&&args[3]==='music')return cb(new Error('exit 1'),'','pc-server: install "AI chat + images" first — music runs on its torch stack\n');
  cb(null,'started\n','');
};
const http={get:(o,res)=>{const h={};const req={on:(ev,fn)=>{h[ev]=fn;return req},destroy(){}};
  setTimeout(()=>{ if(listening){ assert.equal(o.host,'127.0.0.1');assert.equal(o.path,'/client/config');
    res({statusCode:200,resume(){}}); } else h.error(new Error('ECONNREFUSED')); },1);return req;}};
const osm={networkInterfaces:()=>({lo:[{family:'IPv4',address:'127.0.0.1',internal:true}],
  wlan0:[{family:'IPv4',address:'192.168.1.5',internal:false},{family:'IPv6',address:'fe80::1',internal:false}]})};
const fsm={existsSync:p=>{assert.equal(p,'/usr/local/bin/pc-server');return helperThere;}};
const mods={child_process:{execFile},fs:fsm,http,os:osm};
const context={module:{exports:{}},require:n=>{assert.ok(n in mods,'unexpected require '+n);return mods[n]},
  process:{platform:'linux',env:{}},JSON,Promise,Error,Number,String,Object,setTimeout};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const api=context.module.exports;
(async()=>{
  let s=await api.status();
  assert.equal(s.available,true); assert.equal(s.reachable,true);
  assert.equal(s.adminUrl,'http://127.0.0.1:3051/admin'); assert.equal(s.relayUrl,'ws://127.0.0.1:3052');
  assert.deepEqual([...s.lanUrls],['http://192.168.1.5:3051']); assert.deepEqual([...s.lanRelayUrls],['ws://192.168.1.5:3052']);
  listening=false; s=await api.status(); assert.equal(s.reachable,false,'active is not the same as answering');
  statusJson=JSON.stringify({configured:false,active:'inactive',port:3051,relayPort:3052,job:{}});
  const before=calls.length; s=await api.status(); assert.equal(s.reachable,false);
  statusJson=null;
  for(const f of ['enable','disable','restart','logs','jobLog'])await api[f]();
  const job=await api.job(); assert.equal(job.running,true);
  await api.installAi('ai'); await api.installAi('searxng');
  const n=calls.length;
  for(const bad of ['rm -rf /','ai;id','','../ai','AI',null,undefined]){
    await assert.rejects(api.installAi(bad),/unknown AI feature/);
  }
  assert.equal(calls.length,n,'a refused feature must not reach sudo');
  await assert.rejects(api.installAi('music'),/AI chat \+ images/);
  deny=true;
  await assert.rejects(api.enable(),/Only an administrator/);
  await assert.rejects(api.status(),/Only an administrator/);
  deny=false;
  const verbs=new Set(['status','enable','disable','restart','logs','job','job-log','install-ai ai','install-ai searxng','install-ai music']);
  for(const c of calls){
    assert.equal(c[0],'sudo'); assert.equal(c[1],'-n'); assert.equal(c[2],'/usr/local/bin/pc-server');
    assert.ok(verbs.has(c.slice(3).join(' ')),'unlisted helper call: '+c.join(' '));
  }
  helperThere=false;
  s=await api.status(); assert.equal(s.available,false); assert.match(s.reason,/not installed/);
  const m=calls.length;
  await assert.rejects(api.enable(),/not installed/); await assert.rejects(api.installAi('ai'),/not installed/);
  assert.equal(calls.length,m,'no helper, no sudo');
})().catch(e=>{console.error(e);process.exit(1)});
'''
    run = subprocess.run(['node', '-e', script, str(ROOT / 'desktop/server.js')],
                         capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr


def test_the_ipc_handlers_are_guarded_and_the_preload_takes_no_command():
    main = (ROOT / 'desktop/main.js').read_text()
    for name in ['status', 'enable', 'disable', 'restart', 'logs', 'job', 'job-log', 'install-ai']:
        line = next(l for l in main.splitlines() if f"ipcMain.handle('pc:server:{name}'" in l)
        assert 'fsGuard(e); return pcServer.' in line, line
    preload = (ROOT / 'desktop/preload.js').read_text()
    block = preload[preload.index("exposeInMainWorld('pcServer'"):]
    block = block[:block.index('});')]
    assert 'pc:server:install-ai' in block and 'String(feature' in block
    assert 'exec' not in block and 'spawn' not in block
