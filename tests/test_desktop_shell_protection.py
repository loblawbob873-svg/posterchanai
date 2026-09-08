"""Exact native surface registration, including hotplug and older compositor recovery."""
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def run_js(code):
    result=subprocess.run(['node','-e',code],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


def test_wayfire_registration_validates_ids_and_carries_process_owner():
    result=run_js('''
const assert=require('assert');
const {WayfireWM}=require(%s);
(async()=>{
 const wm=new WayfireWM('/unused');const calls=[];
 wm._send=async(method,data)=>{calls.push({method,data});return {result:'ok'};};
 await wm.protectShellViews([14,28,14]);await wm.protectShellViews([]);
 for(const invalid of [[0],[-1],[NaN],[Infinity],[1.5],['14'],[0x100000000],Array(65).fill(1),null])
   await assert.rejects(wm.protectShellViews(invalid));
 assert.equal(calls.length,2);assert.equal(calls[0].data.pid,process.pid);
 console.log(JSON.stringify(calls));
})().catch(e=>{console.error(e);process.exit(1)});
''' % json.dumps(str(ROOT/'desktop/wm-wayfire.js')))
    assert result[0]['method']=='posterchan-shell/set-views'
    assert result[0]['data']['ids']==[14,28]
    assert result[1]['data']['ids']==[]


def test_reconcile_registers_only_exact_shells_on_each_output_and_survives_old_plugin():
    source=(ROOT/'desktop/main.js').read_text()
    function=source[source.index('async function reconcileShellDisplays()'):source.index('function scheduleDisplayReconcile()')]
    result=run_js('''
const vm=require('vm'),assert=require('assert');
(async()=>{
 let outputs=[{output:'left'},{output:'right'}],fail=false,sunk=0;
 const registered=[],warnings=[];
 const browser=id=>({webContents:{id},isDestroyed:()=>false,isVisible:()=>true,destroy(){this.dead=true}});
 const primary=browser(1),secondary=browser(2);
 const records=new Map([['left',{browser:primary,conId:14}],['right',{browser:secondary,conId:28}]]);
 const backend={available:()=>true,outputs:async()=>outputs,workspaces:async()=>[],
  // Ordinary app and popup have the same process: neither may become protected.
  windows:async()=>[{id:14,pid:123},{id:28,pid:123},{id:35,pid:123},{id:36,pid:123}],
  protectShellViews:async ids=>{registered.push(ids);if(fail)throw Error('method unavailable');}};
 const ctx={SHELL_MODE:true,wm:()=>backend,_displayReconcile:null,_displayReconcileTimer:null,
  _shellSurfaces:records,_shellScopes:new Map(),shellDisplays:{plan:x=>x,needsPlacement:()=>false},
  win:primary,process:{pid:123},sinkShellSurfaces:()=>sunk++,
  console:{warn:(...args)=>warnings.push(args)},setTimeout:()=>{throw Error('unexpected retry')},clearTimeout(){}};
 vm.createContext(ctx);vm.runInContext(%s,ctx);
 await ctx.reconcileShellDisplays();
 outputs=[{output:'left'}];await ctx.reconcileShellDisplays();
 fail=true;await ctx.reconcileShellDisplays();
 assert.equal(secondary.dead,true);assert.equal(sunk,3);assert.equal(warnings.length,1);
 console.log(JSON.stringify(registered));
})().catch(e=>{console.error(e);process.exit(1)});
''' % json.dumps(function))
    assert result==[[14,28],[14],[14]]
