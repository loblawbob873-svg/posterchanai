import json,subprocess
from pathlib import Path
APP=(Path(__file__).resolve().parents[2]/'static/js/client/app.js').read_text()
SRC=APP[APP.index('  function renderAdmin(opts)'):APP.index('  /* Name the half that is being waited on.')]

def test_unavailable_signer_shows_recovery_and_retry_opens_admin():
    js='''
const vm=require('node:vm'),assert=require('node:assert/strict');
const feed={innerHTML:'',style:{}},retry={},host={style:{}};let attempts=0,opened=0;
const ctx={IS_ADMIN:true,VIEW:'admin',ME:{pubkey:'owner'},_aiAuth:null,
 $:id=>id==='#feed'?feed:retry,document:{getElementById:()=>host},enc:s=>s,
 _adminFrame:()=>opened++,ensureAiSession:async opts=>{attempts++;if(attempts===1)throw new Error('signEvent failed (0 bytes): Could not establish connection. Receiving end does not exist.');assert.equal(opts.force,true);return {is_admin:true};}};
vm.createContext(ctx);vm.runInContext(SRC,ctx);
(async()=>{ctx.renderAdmin();await new Promise(r=>setImmediate(r));assert.match(feed.innerHTML,/Signer extension unavailable/);assert.match(feed.innerHTML,/Keep your existing pairing/);assert.equal(opened,0);retry.onclick();await new Promise(r=>setImmediate(r));assert.equal(opened,1);assert.equal(attempts,2);})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('vm.runInContext(SRC,ctx)','vm.runInContext('+json.dumps(SRC)+',ctx)')
    subprocess.run(['node'],input=js,text=True,capture_output=True,check=True,timeout=10)
