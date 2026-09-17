"""Virtual Machines, end to end in the REAL client in headless Chrome: host card → VM list → power on →
console → a real noVNC RFB handshake draws pixels → Android Back closes the console.

Only the network boundaries are fixtures, and they are written as the far ends of the real protocols:
  * the POOL relay answers REQs/EVENTs like tests/client/test_effects_full_app.py's fixture;
  * the VM HOST relay decrypts each kind-5310 with the host's own key (real NIP-44), runs the op
    against a tiny in-page host, and answers a SIGNED kind-6310 to the subscription the client opened;
  * /ws/vmconsole speaks the console protocol ({t:open} → {t:ok} → {t:go}) and then a minimal RFB 3.8
    server (None security, 64x48 true-colour framebuffer, one raw red rectangle).
Everything in between — app.js routing, the lazy-loaded vms.js/vmrpc.js/vmconsole.js, the vendored
noVNC, the signer and the DOM — is the shipped code. Run at a phone width (390) and a desktop width
(1280), because the two lay the screen out differently.
"""
import asyncio
import tempfile
import threading
import subprocess
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser, Handler

ROOT = Path(__file__).resolve().parents[2]

INIT = r'''
window.__errors=[];addEventListener('error',e=>__errors.push(String(e.message||e)));
addEventListener('unhandledrejection',e=>__errors.push('rejection: '+String(e.reason&&e.reason.message||e.reason)));
window.__back=null;window.__requests=[];
window.Capacitor={isNativePlatform:()=>false,Plugins:{App:{addListener:(name,fn)=>{if(name==='backButton')window.__back=fn;return {remove(){}};}}}};
const HOST_SK_HEX='9'.repeat(64);
window.__vm={state:'shutoff',ops:[],args:{},vncFromClient:0,consoleOpens:0,vcpus:2,snaps:[],hosts:{}};
const origFetch=window.fetch.bind(window);
window.fetch=async function(url,opts={}){
 const u=String(url);__requests.push(u);
 if(new URL(u,location.href).origin===location.origin && u.includes('/static/'))return origFetch(url,opts);
 let d={};
 if(u.includes('/client/config')){
   // Precomputed: /client/config is fetched before the deferred nostr bundle has loaded.
   const hostPk='8985087b1818714f67e494a076ca0284c060fabc5d2ba66885b4ac60f801d3f5';
   d={nostr_only:false,relay_url:'wss://fixture.invalid',relays:['wss://fixture.invalid'],vmhost:{pubkey:hostPk,npub:'',name:'Fixture host',relay:'wss://vmhost.invalid/relay',https:''}};
 }
 if(u.includes('/auth/nostr-login'))d={access_token:'fixture-token',user:{id:1,can_ai:true,is_admin:true}};
 return new Response(JSON.stringify(d),{status:200,headers:{'Content-Type':'application/json'}});
};
const hexToBytes=h=>Uint8Array.from(h.match(/../g).map(x=>parseInt(x,16)));
function hostKeys(){const sk=hexToBytes(HOST_SK_HEX);return {sk,pk:NostrTools.getPublicKey(sk)};}
const HOST_KEYS={'vmhost.invalid':HOST_SK_HEX,'vmhost2.invalid':'7'.repeat(64),'vmhost4.invalid':'6'.repeat(64)};
function hostKeysFor(url){const k=Object.keys(HOST_KEYS).find(x=>url.includes('//'+x));if(!k)return null;const sk=hexToBytes(HOST_KEYS[k]);return {sk,pk:NostrTools.getPublicKey(sk),name:k};}
function vmView(){return {uuid:'11111111-1111-4111-8111-111111111111',name:'alpha',state:__vm.state,vcpus:__vm.vcpus,ram_mib:2048,disk_gib:20,managed:true,guest:'linux',firmware:'efi',autostart:false,labels:[],assigned:[],owner:'',iso:''};}
const FEATURES=['novnc','hardware','snapshots','iso-fetch','iso-upload','access','sessions'];
function hostOp(op,args,who){
  __vm.ops.push(op);__vm.args[op]=args;
  if(who&&who.name!=='vmhost.invalid'){ if(op==='host.whoami')return {ok:true,result:{role:'user',host:{name:who.name==='vmhost2.invalid'?'Second host':'Found host',features:FEATURES}}};
    if(op==='host.info')return {ok:true,result:{name:'x',vms:{running:0,total:0}}}; if(op==='vm.list')return {ok:true,result:{vms:[],next:null}}; return {ok:false,error:{code:'forbidden',message:'no'}}; }
  if(op==='host.whoami')return {ok:true,result:{role:'admin',host:{name:'Fixture host',version:1,features:FEATURES}}};
  if(op==='vm.get')return {ok:true,result:{vm:Object.assign(vmView(),{hardware:{boot:'disk',input:'tablet',nics:1,disks:[{device:'disk',target:'vda'}],media:'',cdrom:false}})}};
  if(op==='vm.update'){ if(__vm.state!=='shutoff')return {ok:false,error:{code:'conflict',message:'shut it down'}}; if(args.vcpus)__vm.vcpus=args.vcpus;
    return {ok:true,result:{vm:Object.assign(vmView(),{hardware:{boot:args.boot||'disk',input:args.input||'tablet',nics:1,disks:[],media:'',cdrom:false}})}}; }
  if(op==='iso.list')return {ok:true,result:{isos:[{id:'debian.iso',name:'debian.iso',size:654311424}],jobs:[],fetch_enabled:true}};
  if(op==='vm.snapshot.list')return {ok:true,result:{vm:args.vm,snapshots:__vm.snaps}};
  if(op==='vm.snapshot.create'){if(__vm.state!=='shutoff')return {ok:false,error:{code:'conflict',message:'shut the VM down to take a snapshot'}};__vm.snaps.push({name:args.name,created:'2026-09-16 12:00 UTC',state:'ok',disks:['vda'],description:''});return {ok:true,result:{vm:args.vm,snapshots:__vm.snaps}};}
  if(op==='host.access.get')return {ok:true,result:{allowed:[],admins:[],admins_editable:false}};
  if(op==='host.info')return {ok:true,result:{name:'Fixture host',kvm:true,libvirt:true,vms:{running:__vm.state==='running'?1:0,total:1},
    cpu:{cores:8,load1:0.3},ram:{total_mib:32768,free_mib:20000,committed_mib:2048},disk:{total_gib:500,free_gib:400,committed_gib:20},
    limits:{max_vcpus:16,max_ram_mib:65536,max_disk_gib:2048,reserve_ram_mib:2048,reserve_disk_gib:20}}};
  if(op==='vm.list')return {ok:true,result:{vms:[vmView()],next:null}};
  if(op==='vm.power'){ if(args.action==='start'){ if(__vm.state==='running')return {ok:false,error:{code:'conflict',message:'already running'}}; __vm.state='running'; }
                       else __vm.state='shutoff'; return {ok:true,result:{vm:vmView(),action:args.action}}; }
  if(op==='console.ticket')return __vm.state==='running'?{ok:true,result:{ws:'/ws/vmconsole',ticket:'fixture-ticket',vnc_password:'pw123456',exp:Math.floor(Date.now()/1000)+60}}:{ok:false,error:{code:'conflict',message:'start it'}};
  return {ok:false,error:{code:'unsupported',message:op}};
}
function u8(...parts){const n=parts.reduce((a,p)=>a+p.length,0),o=new Uint8Array(n);let i=0;for(const p of parts){o.set(p,i);i+=p.length;}return o;}
function be16(n){return [n>>8&255,n&255];} function be32(n){return [n>>>24&255,n>>16&255,n>>8&255,n&255];}
class FixtureSocket extends EventTarget{
 static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=String(url);this.readyState=0;this.onopen=null;this.onmessage=null;this.onerror=null;this.onclose=null;
   this.protocol='';this.binaryType='blob';this.subs={};this.rfb=null;this.rx=new Uint8Array(0);(window.__sockets=window.__sockets||[]).push(this);
   setTimeout(()=>{this.readyState=1;this.emit('open',{});},10);}
 emit(type,init){const e=type==='message'?new MessageEvent('message',init):new Event(type);this['on'+type]?.(e);this.dispatchEvent(e);}
 text(obj){setTimeout(()=>this.emit('message',{data:JSON.stringify(obj)}),5);}
 bin(bytes){const b=bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength);setTimeout(()=>this.emit('message',{data:b}),5);}
 send(raw){
   if(this.url.includes('/ws/vmconsole'))return this.console(raw);
   const m=JSON.parse(raw);
   if(/vmhost\d?\.invalid/.test(this.url))return this.host(m);
   if(m[0]==='REQ')setTimeout(()=>{for(const ev of window.__events||[]){if(m.slice(2).some(f=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.authors||f.authors.includes(ev.pubkey))))this.text(['EVENT',m[1],ev]);}this.text(['EOSE',m[1]]);},15);
   if(m[0]==='EVENT'&&m[1].kind===30078)(window.__docs=window.__docs||[]).push(m[1]);
   if(m[0]==='EVENT')this.text(['OK',m[1].id,true,'']);
 }
 host(m){
   if(m[0]==='REQ'){this.subs[m[1]]=m[2];this.text(['EOSE',m[1]]);return;}
   if(m[0]==='CLOSE'){delete this.subs[m[1]];return;}
   if(m[0]!=='EVENT')return;
   const ev=m[1];this.text(['OK',ev.id,true,'']);
   if(ev.kind!==5310||!NostrTools.verifyEvent(ev))return;
   const who=hostKeysFor(this.url);if(!who)return;           // vmhost3: announced, never answers
   const {sk}=who;
   const ck=NostrTools.nip44.v2.utils.getConversationKey(sk,ev.pubkey);
   const body=JSON.parse(NostrTools.nip44.v2.decrypt(ev.content,ck));
   const res=Object.assign({v:1,id:body.id},hostOp(body.op,body.args||{},who));
   const now=Math.floor(Date.now()/1000);
   const reply=NostrTools.finalizeEvent({kind:6310,created_at:now,content:NostrTools.nip44.v2.encrypt(JSON.stringify(res),ck),
     tags:[['e',ev.id],['p',ev.pubkey],['nofederate'],['expiration',String(now+300)]]},sk);
   setTimeout(()=>{for(const [id,f] of Object.entries(this.subs)){ if((f['#e']||[]).includes(ev.id)) this.text(['EVENT',id,reply]); }},30);
 }
 console(raw){
   if(typeof raw==='string'){
     const m=JSON.parse(raw);
     if(m.t==='open'){__vm.consoleOpens++; if(m.ticket!=='fixture-ticket')return this.text({t:'err',m:'bad ticket'}); return this.text({t:'ok'});}
     if(m.t==='go'){this.rfb='version';this.bin(new TextEncoder().encode('RFB 003.008\n'));}
     return;
   }
   const chunk=new Uint8Array(raw instanceof ArrayBuffer?raw:raw.buffer?raw.buffer.slice(raw.byteOffset,raw.byteOffset+raw.byteLength):raw);
   __vm.vncFromClient+=chunk.length;
   this.rx=u8(this.rx,chunk);
   for(;;){
     const b=this.rx;
     if(this.rfb==='version'){ if(b.length<12)return; this.rx=b.slice(12); this.rfb='sec'; this.bin(new Uint8Array([1,1])); continue; }
     if(this.rfb==='sec'){ if(b.length<1)return; this.rx=b.slice(1); this.rfb='init'; this.bin(new Uint8Array(be32(0))); continue; }
     if(this.rfb==='init'){ if(b.length<1)return; this.rx=b.slice(1); this.rfb='normal';
       const name=new TextEncoder().encode('fixture');
       this.bin(new Uint8Array([...be16(64),...be16(48),32,24,0,1,...be16(255),...be16(255),...be16(255),16,8,0,0,0,0,...be32(name.length),...name])); continue; }
     if(this.rfb!=='normal'||!b.length)return;
     const t=b[0]; let len=0;
     if(t===0)len=20; else if(t===2){ if(b.length<4)return; len=4+4*((b[2]<<8)|b[3]); }
     else if(t===3)len=10; else if(t===4)len=8; else if(t===5)len=6;
     else if(t===6){ if(b.length<8)return; len=8+((b[4]<<24)|(b[5]<<16)|(b[6]<<8)|b[7]); }
     else if(t===150)len=10; else { this.rx=new Uint8Array(0); return; }
     if(b.length<len)return;
     this.rx=b.slice(len);
     if(t===3 && !this.sentFrame){ this.sentFrame=true;
       const px=new Uint8Array(64*48*4); for(let i=0;i<px.length;i+=4){px[i]=255;px[i+1]=0;px[i+2]=0;px[i+3]=0;}
       this.bin(u8(new Uint8Array([0,0,...be16(1),...be16(0),...be16(0),...be16(64),...be16(48),...be32(0)]),px)); }
   }
 }
 close(){if(this.readyState===3)return;this.readyState=3;this.emit('close',{code:1000,wasClean:true});}
}
window.WebSocket=FixtureSocket;
'''


async def main(width):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='pc-vms-full-') as profile:
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
                # The sidebar row and the More sheet entry exist (the phone reaches views through the sheet).
                assert await b.js("!!document.querySelector('.sidebar .nav-item[data-view=vms]')")
                await b.js("__PC.switchView('vms')")
                await b.until("!!window.PCVms && /Fixture host/.test(document.querySelector('#feed').innerText)")
                if width < 900:
                    assert await b.js("!document.querySelector('.vms-wide')"), 'phone layout is a single stack'
                    await b.until("!!document.querySelector('.vms-host[data-host]')")
                    await b.js("document.querySelector('.vms-host[data-host]').click()")
                else:
                    assert await b.js("!!document.querySelector('.vms-wide .vms-rail')"), ('desktop has a host rail', await b.js("document.querySelector('#feed').getBoundingClientRect().width"))
                await b.until("[...document.querySelectorAll('.vms-vm')].some(e=>/alpha/.test(e.innerText))")
                assert await b.js("/RAM MiB/.test(document.querySelector('#feed').innerText)"), 'admin sees capacity'
                assert await b.js("document.querySelector('.vms').scrollWidth <= innerWidth + 1"), 'no horizontal page scroll'
                await b.js("document.querySelector('.vms-vm').click()")
                await b.until("!!document.querySelector('[data-power=start]:not([disabled])')")
                await b.js("document.querySelector('[data-power=start]').click()")
                await b.until("__vm.state==='running' && !!document.querySelector('[data-act=console]:not([disabled])')")
                assert await b.js("/Running/.test(document.querySelector('.vms-vmhead').innerText)")
                await b.js("document.querySelector('[data-act=console]').click()")
                await b.until("!!document.querySelector('#vms-console')")
                await b.until("document.querySelector('#vms-console').dataset.state==='connected'")
                await b.until("""(()=>{const c=document.querySelector('#vms-console .vmc-screen canvas');if(!c||!c.width)return false;
                    const p=c.getContext('2d').getImageData(10,10,1,1).data;return p[0]>200&&p[1]<40&&p[2]<40;})()""")
                assert await b.js("__vm.vncFromClient>0 && __vm.consoleOpens===1")
                assert await b.js("PCVms.consoleOpen()===true")
                assert await b.js("typeof window.__back==='function'"), 'the Android back listener was registered'
                await b.js("window.__back()")
                await b.until("!document.querySelector('#vms-console')")
                assert await b.js("PCVms.consoleOpen()===false && __PC.isView('vms')"), \
                    'Back closes the console and stays on Virtual Machines'
                # ---------------- snapshots are OFFLINE: while it runs, the button is off and says why
                await b.until("!!document.querySelector('[data-act=snap-create]')")
                assert await b.js("document.querySelector('[data-act=snap-create]').disabled && /Shut down to take a snapshot/.test(document.querySelector('.vms-snaps').innerText)")
                # ---------------- phase 2: VM settings (Save last, dirty tracking, a real round trip)
                await b.js("document.querySelector('[data-power=shutdown]').click()")
                await b.until("__vm.state==='shutoff' && !!document.querySelector('[data-act=settings]:not([disabled])')")
                await b.js("document.querySelector('[data-act=settings]').click()")
                await b.until("!!document.querySelector('#vms-settings [name=vcpus]')")
                assert await b.js("document.querySelector('[data-act=settings-save]').disabled"), 'nothing changed → Save disabled'
                ctl = await b.js("(()=>{const f=document.querySelector('#vms-settings');const c=[...f.querySelectorAll('button,input,select')];return c[c.length-1].dataset.act;})()")
                assert ctl == 'settings-save', ('Save must be the last control', ctl)
                await b.js("(()=>{const i=document.querySelector('#vms-settings [name=vcpus]');i.value='4';i.dispatchEvent(new Event('input',{bubbles:true}));})()")
                await b.until("!document.querySelector('[data-act=settings-save]').disabled")
                vis = await b.js("(()=>{const r=document.querySelector('[data-act=settings-save]').getBoundingClientRect();return r.bottom<=innerHeight+1&&r.top>=0;})()")
                assert vis, 'the sticky footer keeps Save on screen'
                await b.js("document.querySelector('[data-act=settings-save]').click()")
                await b.until("__vm.ops.includes('vm.update') && /VM settings saved/.test(document.querySelector('#vms-settings').innerText)")
                assert await b.js("JSON.stringify(__vm.args['vm.update'])==='{\"vm\":\"11111111-1111-4111-8111-111111111111\",\"vcpus\":4}'"), \
                    await b.js("JSON.stringify(__vm.args['vm.update'])")
                assert await b.js("document.querySelector('[data-act=settings-save]').disabled"), 'after a save nothing is dirty'
                await b.js("document.querySelector('[data-act=settings-leave]').click()")
                await b.until("!!document.querySelector('.vms-vmhead')")
                # ---------------- snapshots
                await b.until("!!document.querySelector('[data-act=snap-create]')")
                await b.js("document.querySelector('[data-act=snap-create]').click()")
                await b.until("!!document.querySelector('.uiprompt-in')")
                await b.js("document.querySelector('.uiprompt-in').value='clean';document.querySelector('.uiconfirm [data-uc=\"1\"]').click()")
                await b.until("[...document.querySelectorAll('.vms-snap')].some(e=>/clean/.test(e.innerText))")
                assert await b.js("!document.querySelector('[data-snap-revert=clean]').disabled && !document.querySelector('.vms-snap-off')"), \
                    'shut off: an ok snapshot can be reverted and the hint is gone'
                assert await b.js("__vm.args['vm.snapshot.create'].name==='clean'")
                # ---------------- ISO library
                await b.js("document.querySelector('[data-act=back]').click()")
                await b.until("!!document.querySelector('[data-act=isos]')")
                await b.js("document.querySelector('[data-act=isos]').click()")
                await b.until("[...document.querySelectorAll('.vms-iso')].some(e=>/debian\\.iso/.test(e.innerText))")
                assert await b.js("!!document.querySelector('[data-act=iso-fetch]') && !!document.querySelector('input[data-act=iso-upload]')")
                assert await b.js("document.querySelector('.vms').scrollWidth <= innerWidth + 1"), 'ISO screen: no horizontal scroll'
                await b.js("document.querySelector('[data-act=to-host]').click()")
                await b.until("!!document.querySelector('[data-act=isos]')")
                # ---------------- add a host
                if width < 900:
                    await b.js("document.querySelector('[data-act=back]').click()")
                await b.until("!!document.querySelector('[data-act=add]')")
                host2 = await b.js("NostrTools.getPublicKey(Uint8Array.from('7'.repeat(64).match(/../g).map(x=>parseInt(x,16))))")
                await b.js("document.querySelector('[data-act=add]').click()")
                await b.until("!!document.querySelector('.uiprompt-in')")
                await b.js("document.querySelector('.uiprompt-in').value=NostrTools.nip19.npubEncode('%s');document.querySelector('.uiconfirm [data-uc=\"1\"]').click()" % host2)
                await b.until("!!document.querySelector('.uiprompt-in')")
                await b.js("document.querySelector('.uiprompt-in').value='wss://vmhost2.invalid/relay';document.querySelector('.uiconfirm [data-uc=\"1\"]').click()")
                await b.until("/Second host/.test(document.querySelector('#feed').innerText)")
                assert await b.js("(__docs||[]).length>=1"), 'the host list was saved to pcai:vmhosts'
                # ---------------- find hosts: one announced host answers, one does not
                await b.js("""(()=>{const mk=(skHex,relay,name)=>{const sk=Uint8Array.from(skHex.match(/../g).map(x=>parseInt(x,16)));
                    return NostrTools.finalizeEvent({kind:31310,created_at:Math.floor(Date.now()/1000),tags:[['d','posterchan-vmhost'],['relay',relay]],
                      content:JSON.stringify({v:1,name,relays:[relay]})},sk);};
                    window.__events.push(mk('6'.repeat(64),'wss://vmhost4.invalid/relay','Found host'), mk('5'.repeat(64),'wss://vmhost3.invalid/relay','Silent host'));})()""")
                if width < 900:
                    if await b.js("!document.querySelector('[data-act=find]')"):
                        await b.js("document.querySelector('[data-act=back]').click()")
                await b.until("!!document.querySelector('[data-act=find]')")
                await b.js("document.querySelector('[data-act=find]').click()")
                await b.until("/Added 1 host/.test(document.querySelector('#feed').innerText)")
                found = await b.js("NostrTools.getPublicKey(Uint8Array.from('6'.repeat(64).match(/../g).map(x=>parseInt(x,16))))")
                silent = await b.js("NostrTools.getPublicKey(Uint8Array.from('5'.repeat(64).match(/../g).map(x=>parseInt(x,16))))")
                hosts = await b.js("PCVms._state.hosts.map(h=>h.pubkey)")
                assert found in hosts and silent not in hosts, hosts
                assert await b.js("document.querySelector('.vms').scrollWidth <= innerWidth + 1"), 'find screen: no horizontal scroll'
                errors = await b.js("__errors.filter(e=>!/ResizeObserver/.test(e))")
                assert not errors, errors
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            server.shutdown()
            server.server_close()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome is required')
@pytest.mark.parametrize('width', [390, 1280])
def test_vms_full_app(width):
    asyncio.run(main(width))
