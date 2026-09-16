"""Migrate a VM between two hosts in the REAL client, in headless Chrome, at a phone (390) and a desktop
(1280) width.

The two VM hosts are in-page fixtures written as the far end of the real protocol: each one decrypts
the kind-5310 with its own key (real NIP-44), answers a SIGNED 6310 to the subscription the client
opened, and — the part only this test covers — emits SIGNED kind-7310 progress events to whatever
subscription carries the `#e` of the authorization the client signed. The SOURCE fixture also opens
that authorization with the TARGET's key, which is exactly what a real target does, so a client that
encrypted it to the wrong host (or bound it to the wrong VM) fails here.

What is asserted:
  * the Migrate action is on an admin's VM screen; the target picker offers the other host;
  * "Check target" shows the precheck; Migrate goes through PC.uiConfirm (never a native dialog);
  * the progress bar is driven by 7310 events (the status poll never reports 50%, so seeing 50% proves
    the live subscription works) and the screen reaches "Migrated";
  * phone: Cancel goes through a confirm and the screen says the VM stays;
  * desktop: a LOCKED migration shows the split-brain warning, and force reclaim needs a confirm AND the
    VM's name typed, then goes to BOTH hosts with side + confirm;
  * no horizontal page scroll, no console errors.
"""
import asyncio
import json
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser, Handler

SRC_PK = '8985087b1818714f67e494a076ca0284c060fabc5d2ba66885b4ac60f801d3f5'   # getPublicKey('9'*64)

INIT = r'''
window.__errors=[];addEventListener('error',e=>__errors.push(String(e.message||e)));
addEventListener('unhandledrejection',e=>__errors.push('rejection: '+String(e.reason&&e.reason.message||e.reason)));
window.__requests=[];
window.Capacitor={isNativePlatform:()=>false,Plugins:{App:{addListener:()=>({remove(){}})}}};
const SRC_SK_HEX='9'.repeat(64), TGT_SK_HEX='7'.repeat(64);
const UUID='11111111-1111-4111-8111-111111111111';
window.__mig={id:'',authz:'',src:'',dst:'',pct:0,ops:[],authzOk:false,hold:false,lock:false,reclaim:[],cancel:0,delivered:0,n:0};
const origFetch=window.fetch.bind(window);
window.fetch=async function(url,opts={}){
 const u=String(url);__requests.push(u);
 if(new URL(u,location.href).origin===location.origin && u.includes('/static/'))return origFetch(url,opts);
 let d={};
 if(u.includes('/client/config'))d={nostr_only:false,relay_url:'wss://fixture.invalid',relays:['wss://fixture.invalid'],
   vmhost:{pubkey:'%SRC_PK%',npub:'',name:'Source host',relay:'wss://vmhost.invalid/relay',https:''}};
 if(u.includes('/auth/nostr-login'))d={access_token:'fixture-token',user:{id:1,can_ai:true,is_admin:true}};
 return new Response(JSON.stringify(d),{status:200,headers:{'Content-Type':'application/json'}});
};
const hexToBytes=h=>Uint8Array.from(h.match(/../g).map(x=>parseInt(x,16)));
function keys(side){const sk=hexToBytes(side==='target'?TGT_SK_HEX:SRC_SK_HEX);return {sk,pk:NostrTools.getPublicKey(sk)};}
function summary(side){const st=side==='source'?__mig.src:__mig.dst;
  return {id:__mig.id,role:side,state:st,vm:UUID,name:'alpha',source:keys('source').pk,target:keys('target').pk,
          bytes_total:21474836480,bytes_done:st==='done'?21474836480:0,pct:st==='done'?100:0,error:'',msg:'',retained:true,acked:st==='done'};}
function authzOk(a,me){
  try{
    if(!a||a.kind!==5310||a.pubkey!==me||!NostrTools.verifyEvent(a))return false;
    if(!(a.tags||[]).some(t=>t[0]==='p'&&t[1]===keys('target').pk))return false;
    const ck=NostrTools.nip44.v2.utils.getConversationKey(keys('target').sk,a.pubkey);
    const b=JSON.parse(NostrTools.nip44.v2.decrypt(a.content,ck));
    return b.op==='vm.migrate.authorize'&&b.args.source===keys('source').pk&&b.args.target===keys('target').pk&&b.args.vm===UUID;
  }catch(_){return false;}
}
function emit(side,phase,extra){
  const {sk,pk}=keys(side);const now=Math.floor(Date.now()/1000);
  const dom=side==='source'?'vmhost.invalid':'vmhost2.invalid';
  for(const s of window.__sockets||[]){
    if(!s.url.includes(dom))continue;
    for(const [sid,f] of Object.entries(s.subs)){
      if(!(f.kinds||[]).includes(7310)||!(f['#e']||[]).includes(__mig.authz))continue;
      const ck=NostrTools.nip44.v2.utils.getConversationKey(sk,__mig.me);
      const body={v:1,id:__mig.id,progress:Object.assign(summary(side),{side,phase},extra||{})};
      const ev=NostrTools.finalizeEvent({kind:7310,created_at:now,content:NostrTools.nip44.v2.encrypt(JSON.stringify(body),ck),
        tags:[['e',__mig.authz],['p',__mig.me],['nofederate'],['expiration',String(now+600)]]},sk);
      __mig.delivered++;s.text(['EVENT',sid,ev]);
    }
  }
}
function hostOp(side,op,args,me){
  __mig.ops.push(side+':'+op);
  if(op==='host.whoami')return {ok:true,result:{role:'admin',host:{name:side==='source'?'Source host':'Target host',version:1,features:['novnc','cold-migrate']}}};
  if(op==='host.info')return {ok:true,result:{name:'x',kvm:true,libvirt:true,vms:{running:1,total:1},cpu:{cores:8,load1:0.3},
    ram:{total_mib:32768,free_mib:20000,committed_mib:2048},disk:{total_gib:500,free_gib:400,committed_gib:20},limits:{}}};
  if(op==='vm.list')return {ok:true,result:{vms:side==='source'?[{uuid:UUID,name:'alpha',state:'running',vcpus:2,ram_mib:2048,disk_gib:20,guest:'linux',firmware:'efi',autostart:true,labels:[],assigned:[],migration:{}}]:[],next:null}};
  if(op==='vm.migrate.status'){
    const peers=side==='source'?[{pubkey:keys('target').pk,relay:'wss://vmhost2.invalid/relay',https:'https://t.invalid'}]:[];
    if(args.migration){ if(args.migration!==__mig.id)return {ok:false,error:{code:'not_found',message:'no such migration'}};
      return {ok:true,result:{migrations:[summary(side)],peers}}; }
    return {ok:true,result:{migrations:__mig.id&&side==='source'?[summary('source')]:[],peers}};
  }
  if(op==='vm.migrate.precheck'){
    if(side!=='source'||args.vm!==UUID||args.target!==keys('target').pk)return {ok:false,error:{code:'bad_request',message:'bad'}};
    if(!authzOk(args.authz,me))return {ok:false,error:{code:'forbidden',message:'authz'}};
    __mig.authzOk=true;
    return {ok:true,result:{source:{vm:UUID,name:'alpha',state:'running',files:2,total_bytes:21474836480},target:{ok:true,free_gib:400,need_gib:29,iso_available:true}}};
  }
  if(op==='vm.migrate'){
    if(!authzOk(args.authz,me))return {ok:false,error:{code:'forbidden',message:'authz'}};
    __mig.n++;__mig.id=('ab'+__mig.n).padEnd(32,'0');__mig.authz=args.authz.id;__mig.me=me;__mig.src='transferring';__mig.dst='receiving';
    __mig.startAfter=args.start_after;__mig.force=args.force_shutdown;
    if(__mig.lock){ setTimeout(()=>{__mig.src='locked';__mig.dst='locked';emit('source','locked');emit('target','locked');},600); }
    else if(!__mig.hold){
      setTimeout(()=>emit('target','transfer',{pct:50,bytes_done:10737418240}),400);
      setTimeout(()=>{__mig.src='done';__mig.dst='done';emit('target','done');emit('source','done');},4500);
    }
    return {ok:true,result:{migration:summary('source'),precheck:{}}};
  }
  if(op==='vm.migrate.cancel'){__mig.cancel++;__mig.src='aborted';__mig.dst='aborted';return {ok:true,result:{migration:summary('source')}};}
  if(op==='vm.migrate.force_reclaim'){
    __mig.reclaim.push([side,args.side,args.confirm,args.migration]);
    if(side==='source')__mig.src=args.side==='source'?'reclaimed':'released'; else __mig.dst=args.side==='target'?'done':'released';
    return {ok:true,result:{migration:summary(side),warning:'SPLIT-BRAIN RISK'}};
  }
  return {ok:false,error:{code:'unsupported',message:op}};
}
class FixtureSocket extends EventTarget{
 static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=String(url);this.readyState=0;this.onopen=null;this.onmessage=null;this.onerror=null;this.onclose=null;
   this.protocol='';this.binaryType='blob';this.subs={};(window.__sockets=window.__sockets||[]).push(this);
   setTimeout(()=>{this.readyState=1;this.emit('open',{});},10);}
 emit(type,init){const e=type==='message'?new MessageEvent('message',init):new Event(type);this['on'+type]?.(e);this.dispatchEvent(e);}
 text(obj){setTimeout(()=>this.emit('message',{data:JSON.stringify(obj)}),5);}
 send(raw){
   const m=JSON.parse(raw);
   if(this.url.includes('vmhost.invalid'))return this.host('source',m);
   if(this.url.includes('vmhost2.invalid'))return this.host('target',m);
   if(m[0]==='REQ')setTimeout(()=>{for(const ev of window.__events||[]){if(m.slice(2).some(f=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.authors||f.authors.includes(ev.pubkey))))this.text(['EVENT',m[1],ev]);}this.text(['EOSE',m[1]]);},15);
   if(m[0]==='EVENT')this.text(['OK',m[1].id,true,'']);
 }
 host(side,m){
   if(m[0]==='REQ'){this.subs[m[1]]=m[2];this.text(['EOSE',m[1]]);return;}
   if(m[0]==='CLOSE'){delete this.subs[m[1]];return;}
   if(m[0]!=='EVENT')return;
   const ev=m[1];this.text(['OK',ev.id,true,'']);
   if(ev.kind!==5310||!NostrTools.verifyEvent(ev))return;
   const {sk,pk}=keys(side);
   if(!(ev.tags||[]).some(t=>t[0]==='p'&&t[1]===pk))return;
   const ck=NostrTools.nip44.v2.utils.getConversationKey(sk,ev.pubkey);
   const body=JSON.parse(NostrTools.nip44.v2.decrypt(ev.content,ck));
   const res=Object.assign({v:1,id:body.id},hostOp(side,body.op,body.args||{},ev.pubkey));
   const now=Math.floor(Date.now()/1000);
   const reply=NostrTools.finalizeEvent({kind:6310,created_at:now,content:NostrTools.nip44.v2.encrypt(JSON.stringify(res),ck),
     tags:[['e',ev.id],['p',ev.pubkey],['nofederate'],['expiration',String(now+300)]]},sk);
   setTimeout(()=>{for(const [id,f] of Object.entries(this.subs)){ if((f['#e']||[]).includes(ev.id)) this.text(['EVENT',id,reply]); }},30);
 }
 close(){if(this.readyState===3)return;this.readyState=3;this.emit('close',{code:1000,wasClean:true});}
}
window.WebSocket=FixtureSocket;
'''.replace('%SRC_PK%', SRC_PK)


async def confirm_dialog(b, prompt_value=None):
    await b.until("!!document.querySelector('.uiconfirm [data-uc=\"1\"]')")
    if prompt_value is not None:
        await b.until("!!document.querySelector('.uiprompt-in')")
        await b.js("(()=>{const i=document.querySelector('.uiprompt-in');i.value=%s;})()" % json.dumps(prompt_value))
    await b.js("document.querySelector('.uiconfirm [data-uc=\"1\"]').click()")


async def open_migrate(b, width):
    if width < 900:
        await b.until("!!document.querySelector('.vms-host[data-host=\"%s\"]')" % SRC_PK)
        await b.js("document.querySelector('.vms-host[data-host=\"%s\"]').click()" % SRC_PK)
    else:
        await b.until("!!document.querySelector('.vms-rail .vms-host[data-host=\"%s\"]')" % SRC_PK)
        await b.js("document.querySelector('.vms-rail .vms-host[data-host=\"%s\"]').click()" % SRC_PK)
    await b.until("[...document.querySelectorAll('.vms-vm')].some(e=>/alpha/.test(e.innerText))")
    await b.js("document.querySelector('.vms-vm').click()")
    await b.until("!!document.querySelector('[data-act=migrate]')")
    await b.js("document.querySelector('[data-act=migrate]').click()")
    tgt = await b.js("NostrTools.getPublicKey(Uint8Array.from('7'.repeat(64).match(/../g).map(x=>parseInt(x,16))))")
    await b.until("!!document.querySelector('#vms-mig option[value=\"%s\"]:not([disabled])')" % tgt)
    await b.js("(()=>{const s=document.querySelector('#vms-mig select[name=target]');s.value=%s;s.dispatchEvent(new Event('change'));})()" % json.dumps(tgt))
    await b.until("!!document.querySelector('[data-act=mig-check]:not([disabled])')")
    await b.js("document.querySelector('[data-act=mig-check]').click()")
    await b.until("/29 GiB/.test((document.querySelector('.vms-mig-pre')||{}).innerText||'')")
    assert await b.js("__mig.authzOk===true"), 'the authorization opened with the TARGET key and was bound to this VM'
    await b.until("!!document.querySelector('[data-act=mig-start]:not([disabled])')")
    return tgt


async def main(width):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='pc-vms-mig-') as profile:
        proc = subprocess.Popen(['/opt/google/chrome/chrome', '--headless=new', '--no-sandbox', '--disable-gpu',
                                 '--window-size=1440,1000', '--remote-debugging-port=0', '--user-data-dir=' + profile,
                                 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile, 'DevToolsActivePort').exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, 'DevToolsActivePort').read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get('http://127.0.0.1:' + port + '/json')).json()
            page = next(p for p in pages if p.get('type') == 'page' and p.get('url') == 'about:blank')
            async with websockets.connect(page['webSocketDebuggerUrl'], max_size=20_000_000) as ws:
                b = Browser(ws)
                await b.call('Page.enable')
                await b.call('Network.enable')
                await b.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
                await b.call('Emulation.setDeviceMetricsOverride',
                             {'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': width < 600})
                await b.call('Page.addScriptToEvaluateOnNewDocument', {'source': INIT})
                await b.call('Page.navigate', {'url': f'http://127.0.0.1:{server.server_port}/client'})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("(()=>{const key=new Uint8Array(32).fill(3);window.__events=[];"
                           "document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);"
                           "document.querySelector('#btn-nsec-login').click()})()")
                await b.until("!!__PC.me()")
                # Both hosts in this account's cached list (the source is also this instance's own host).
                await b.js("""(()=>{const tgt=NostrTools.getPublicKey(Uint8Array.from('7'.repeat(64).match(/../g).map(x=>parseInt(x,16))));
                    localStorage.setItem('pc_vms:'+__PC.me().pubkey, JSON.stringify({v:1,hosts:[
                      {pubkey:'%s',relay:'wss://vmhost.invalid/relay',name:'Source host',source:'instance'},
                      {pubkey:tgt,relay:'wss://vmhost2.invalid/relay',name:'Target host',source:'added'}],data:{}}));})()""" % SRC_PK)
                await b.js("__PC.switchView('vms')")
                await b.until("!!window.PCVms && /Source host/.test(document.querySelector('#feed').innerText)")

                await open_migrate(b, width)
                if width < 900:
                    await b.js("__mig.hold=true")
                await b.js("document.querySelector('[data-act=mig-start]').click()")
                await confirm_dialog(b)
                await b.until("!!document.querySelector('#vms-migprog')")
                assert await b.js("__mig.startAfter===true"), 'start_after defaults on for a running VM'
                if width < 900:
                    await b.until("!!document.querySelector('[data-act=mig-cancel]')")
                    assert await b.js("document.querySelector('.vms').scrollWidth <= innerWidth + 1"), 'no horizontal scroll'
                    await b.js("document.querySelector('[data-act=mig-cancel]').click()")
                    await confirm_dialog(b)
                    await b.until("document.querySelector('#vms-migprog').dataset.src==='aborted'")
                    assert await b.js("__mig.cancel===1 && /stays on this host/.test(document.querySelector('#vms-migprog').innerText)")
                else:
                    # 50% only ever arrives as a 7310 event: the status poll never reports it.
                    await b.until("document.querySelector('.vms-mig-bar').getAttribute('aria-valuenow')==='50'")
                    assert await b.js("__mig.delivered>0")
                    await b.until("!!document.querySelector('.vms-mig-done')")
                    assert await b.js("document.querySelector('.vms').scrollWidth <= innerWidth + 1")
                    # A second migration that LOCKS: the warning, then force reclaim with confirm + typed name.
                    await b.js("document.querySelector('[data-act=back]').click()")
                    await b.until("!!document.querySelector('[data-act=migrate]')")
                    await b.js("__mig.lock=true")
                    await b.js("document.querySelector('[data-act=migrate]').click()")
                    tgt = await b.js("NostrTools.getPublicKey(Uint8Array.from('7'.repeat(64).match(/../g).map(x=>parseInt(x,16))))")
                    await b.until("!!document.querySelector('#vms-mig option[value=\"%s\"]:not([disabled])')" % tgt)
                    await b.js("(()=>{const s=document.querySelector('#vms-mig select[name=target]');s.value=%s;s.dispatchEvent(new Event('change'));})()" % json.dumps(tgt))
                    await b.js("document.querySelector('[data-act=mig-check]').click()")
                    await b.until("!!document.querySelector('[data-act=mig-start]:not([disabled])')")
                    await b.js("document.querySelector('[data-act=mig-start]').click()")
                    await confirm_dialog(b)
                    await b.until("!!document.querySelector('.vms-mig-locked')")
                    assert await b.js("/split-brain/i.test(document.querySelector('.vms-mig-locked').innerText)")
                    assert await b.js("!document.querySelector('[data-act=mig-cancel]')"), 'too late to cancel'
                    await b.js("document.querySelector('[data-act=mig-reclaim][data-side=target]').click()")
                    await b.until("/SPLIT-BRAIN/.test((document.querySelector('.uiconfirm-msg')||{}).innerText||'')")
                    await confirm_dialog(b)
                    await confirm_dialog(b, prompt_value='alpha')
                    await b.until("__mig.reclaim.length===2")
                    rec = await b.js("__mig.reclaim")
                    assert sorted(r[0] for r in rec) == ['source', 'target']
                    assert all(r[1] == 'target' and r[2] == 'split-brain' for r in rec), rec
                errors = await b.js("__errors.filter(e=>!/ResizeObserver/.test(e))")
                assert not errors, errors
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            server.shutdown()
            server.server_close()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome is required')
@pytest.mark.parametrize('width', [390, 1280])
def test_vms_migrate_full_app(width):
    asyncio.run(main(width))
