"""Exercise the real main IPC handler, preload role calculation and window adoption."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_reload_context_is_sender_bound_and_never_another_background_writer():
    main = (ROOT/'desktop/main.js').read_text()
    handler = main[main.index("ipcMain.on('pc:window:context'"):main.index('/* ── THE COMPOSITOR', main.index("ipcMain.on('pc:window:context'"))]
    pre = (ROOT/'desktop/preload.js').read_text()
    role = pre[pre.index('const windowContext ='):pre.index('// Clipboard WRITE,', pre.index('const windowContext ='))]
    script = r'''
const assert=require('assert'),vm=require('vm'),fs=require('fs');
let handle;const ipcMain={on:(name,fn)=>{handle=fn}};
const sender={}, other={}, dead={};
const pcAppWindows=new Map([['reservation',{pending:true}],['destroyed',{get webContents(){throw Error('destroyed getter')},isDestroyed:()=>true}],['settings',{webContents:sender,isDestroyed:()=>false}],['mail',{webContents:dead,isDestroyed:()=>true}]]);
const fsGuard=e=>{if(!e.trusted)throw Error('denied')};
vm.runInNewContext(HANDLER,{ipcMain,pcAppWindows,fsGuard});
function context(w,trusted=true){const e={sender:w,trusted};handle(e);return e.returnValue}
assert.deepEqual(context(sender),{role:'app',view:'settings'});
assert.equal(context(other),null);assert.equal(context(dead),null);assert.equal(context(sender,false),null);
function preload(ctx,search='',trusted=true){const sandbox={isOurPage:trusted,ipcRenderer:{sendSync:()=>ctx},location:{search},URLSearchParams,process:{argv:[]}};vm.runInNewContext(ROLE+';globalThis.result={windowContext,backgroundOwner};',sandbox);return sandbox.result}
let restored=preload(context(sender));assert.equal(restored.backgroundOwner,false);
assert.equal(preload(null,'?pcwin=settings').backgroundOwner,false);
assert.equal(preload(context(other)).backgroundOwner,true);
assert.equal(preload(context(sender),'',false).windowContext,null);
function document(context,search){const root={pcShell:{windowContext:context},location:{search},document:{documentElement:{classList:{add(){}}}},localStorage:{getItem(){return null}}};const sandbox={globalThis:root,module:{exports:{}},URLSearchParams};vm.runInNewContext(fs.readFileSync(OSWIN,'utf8'),sandbox);return {root,api:sandbox.module.exports}}
let first=document(null,'?pcwin=settings');assert(first.api.isWindow());
first.root.location.search='';assert(first.api.isWindow());
let reloaded=document(restored.windowContext,'');assert(reloaded.api.isWindow());assert.equal(reloaded.api.viewOf(),'settings');
assert.equal(document(null,'').api.isWindow(),false);
'''
    script=script.replace('HANDLER',json.dumps(handler)).replace('ROLE',json.dumps(role)).replace('OSWIN',json.dumps(str(ROOT/'static/js/client/oswin.js')))
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
