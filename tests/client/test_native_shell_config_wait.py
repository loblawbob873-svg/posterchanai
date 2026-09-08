"""Local shell startup must not depend on the instance config transport.

Executes the shipped boot prefix with a held fetch; full bundled DOM rendering is
covered separately. These cases also preserve fresh-config and window ownership.
"""
from pathlib import Path
import subprocess
import os

ROOT = Path(os.environ.get("PC_SHELL_SOURCE_ROOT", Path(__file__).resolve().parents[2]))


def test_native_shell_paints_before_config_and_preserves_fresh_config():
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('static/js/client/app.js','utf8');
const begin=source.indexOf('  async function boot(){');
const end=source.indexOf('    // Custom branding',begin);
const boot=source.slice(begin,end)+'\n}';
async function scenario({native=true,bundled=true,asWindow=false,cached={relay_url:'wss://cached'},reject=false}={}){
 let release,restores=[],gates=[],timers=new Map(),nextTimer=0;
 const response=new Promise((yes,no)=>release=()=>reject?no(Error('offline')):yes({ok:true,json:async()=>({relay_url:'wss://fresh',registration_enabled:false})}));
 const ctx={CFG:null,BUNDLED:bundled,window:null,PCOS:{restore(){restores.push(ctx.CFG);}},
  PCOSWin:{isWindow:()=>asWindow,adopt(){}},_standalone:()=>false,_cfgCached:()=>cached,
  _cfgCache:()=>{},applyInstanceGating(){gates.push(ctx.CFG);if(ctx.CFG?.nostr_only)ctx.PC_NOSTR_ONLY=true;},_wireOsLogo(){},
  fetch:()=>response,setTimeout(fn){const id=++nextTimer;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},console};
 ctx.window=ctx;if(native)ctx.pcShell={};
 vm.createContext(ctx);vm.runInContext(boot+'; globalThis.run=boot;',ctx);
 const pending=ctx.run();await Promise.resolve();await Promise.resolve();
 assert.equal(restores.length,native&&bundled&&!asWindow?1:0,'shell must paint while config is still pending');
 if(restores.length)assert.equal(restores[0].relay_url,cached?.relay_url);
 release();await pending;
 assert.equal(restores.length,asWindow?0:1,'config settlement must not rebuild an already usable menu');
 assert.equal(ctx.CFG.relay_url,reject?cached?.relay_url:'wss://fresh');
 if(!reject){assert.equal(ctx.CFG.registration_enabled,false,'fresh policy must still replace cache');assert.equal(!!ctx.PC_NOSTR_ONLY,false,'cached capability flag must not override fresh config');}
 assert.equal(timers.size,0);
}
(async()=>{
 await scenario();await scenario({cached:{relay_url:'wss://cached',nostr_only:true}});await scenario({cached:null});await scenario({reject:true});
 await scenario({native:false});await scenario({bundled:false});await scenario({asWindow:true});
 console.log('native cached/fresh/offline, web, Android, and owned window cases passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_popup_identity_survives_client_history_cleanup():
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('static/js/client/os.js','utf8');
const start=source.indexOf("  let _popupIdentity = '';");
const marker=source.slice(start,source.indexOf('\n  const MIN_WIDTH',start));
const begin=source.indexOf('  function popupKind(){');
const kind=source.slice(begin,source.indexOf('\n  function enter(){',begin));
for(const initial of ['start','noti','net','tray','compose','']){
 const classes=new Set();const ctx={URLSearchParams,window:{location:{search:initial?'?pcpopup='+initial:''}},document:{documentElement:{classList:{add:c=>classes.add(c)}}}};
 vm.createContext(ctx);vm.runInContext(marker+kind+'; globalThis.kind=popupKind;',ctx);
 assert.equal(ctx.kind(),initial);
 ctx.window.location.search='';
 assert.equal(ctx.kind(),initial,'history cleanup must not remove native popup ownership');
 assert.equal(classes.has('pc-popup-boot'),!!initial);
}
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_late_popup_restore_and_compositor_detection_keep_search_input():
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('static/js/client/os.js','utf8');
const begin=source.indexOf('  function renderStartPopup(){');
const body=source.slice(begin,source.indexOf('\n  /* THE NOTIFICATION CENTRE',begin));
const scanStart=source.indexOf('  function _scanStartMachineApps(');
const scan=source.slice(scanStart,source.indexOf('  function toggleStart(',scanStart));
let detectReady,menu=null,toggles=0,refreshes=[];
const input={value:'',selectionStart:0,selectionEnd:0},host={isConnected:true};
const ctx={window:null,document:{body:{classList:{add(){}}},addEventListener(){}},
 PCOSShell:{detect:()=>new Promise(r=>detectReady=r),available:()=>false},popupHost:()=>host,
 _menuInPopup:false,root:null,startOpen:false,
 $:(selector)=>selector==='#os-startmenu'?menu:selector==='#os-q'?input:null,
 _repaintStart:q=>refreshes.push(q),toggleStart(){toggles++;menu={};},console};
ctx.window=ctx;vm.createContext(ctx);vm.runInContext(scan+body+'; globalThis.render=renderStartPopup;',ctx);
(async()=>{
 ctx.render();assert.equal(toggles,1);
 input.value='firefox';input.selectionStart=2;input.selectionEnd=5;
 ctx.document.activeElement=input;
 detectReady(true);await new Promise(setImmediate);
 ctx.render();
 assert.equal(toggles,1,'late completion must not rebuild Start');
 assert.deepEqual(refreshes,['firefox','firefox']);
 assert.equal(ctx.document.activeElement,input);assert.equal(input.value,'firefox');
 assert.equal(input.selectionStart,2);assert.equal(input.selectionEnd,5);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_local_app_scan_waits_for_detection_and_ignores_closed_menu():
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('static/js/client/os.js','utf8');
const begin=source.indexOf('  function _scanStartMachineApps(');
const scan=source.slice(begin,source.indexOf('  function toggleStart(',begin));
let available=false,calls=0,finish;
const q={value:'fi'},painted=[];
const ctx={window:null,PCOSShell:{available:()=>available,allApps(force){assert.equal(force,true);calls++;return new Promise(r=>finish=r);}},
 $:()=>q,_machineApps:null};
ctx.window=ctx;vm.createContext(ctx);vm.runInContext(scan+';globalThis.scan=_scanStartMachineApps;',ctx);
(async()=>{
 const menu={isConnected:true};ctx.scan(menu,x=>painted.push(x));assert.equal(calls,0);
 available=true;ctx.scan(menu,x=>painted.push(x));ctx.scan(menu,x=>painted.push(x));assert.equal(calls,1);
 q.value='firefox';const list=[{id:'firefox.desktop',name:'Firefox'}];finish(list);await new Promise(setImmediate);
 assert.equal(ctx._machineApps,list);assert.deepEqual(painted,['firefox']);
 const closed={isConnected:true};ctx.scan(closed,x=>painted.push(x));assert.equal(calls,2);closed.isConnected=false;
 finish([{id:'obsolete.desktop'}]);await new Promise(setImmediate);
 assert.equal(ctx._machineApps,list,'a closed menu must not replace the current machine app list');
 assert.deepEqual(painted,['firefox']);
 const reopened={isConnected:true};ctx.scan(reopened,x=>painted.push(x));assert.equal(calls,3,'new opening must rescan');
 finish(list);await new Promise(setImmediate);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
