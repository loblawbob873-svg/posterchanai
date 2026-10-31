"""Shared fixture for the PBS full-app tests (tests/client/test_pbs_*_full_app.py).

The real bundled client (tests/client/test_desktop_offline_full_app.py builds it and drives Chrome); only the
edges are fixtures:

  * MEDIA  -- getDisplayMedia / getUserMedia / enumerateDevices return a canvas-drawn video (blue = the screen,
              green = the camera) and an OscillatorNode tone (330 Hz desktop sound, 440 Hz microphone), so every
              pixel and every level the tests read is one the test put there.
  * INGEST -- /api/streams/ingest answers like app/routers/streams.py does (`__ingestMode`: ok | refused | off |
              down | mediaserver_down).
  * WHIP   -- a REAL second RTCPeerConnection in the page answers the offer, so the publish is a real WebRTC
              session; what it received (`__srvTracks`) is what a media server would have received. The answer
              carries `Location: /api/streams/whip/<token>/<uuid>` exactly as the server now does; a DELETE on it
              is recorded in `__whipDel`. `__whipMode = 'fail'` answers 502.
  * RELAY  -- the network fixture's socket, accepting publishes (`__publishOK`), and `__silent30078` makes every
              REQ for kind 30078 go unanswered (a relay that never EOSEs: "could not read").
"""
import asyncio
import base64
import json
import os
import struct
import zlib
from pathlib import Path

SHOTS = os.environ.get('PC_PBS_SHOTS', '')   # screenshots only when asked for: the gate writes nothing
SID = '3f2b8c1e-9a4d-4c2e-8f1a-0123456789ab'
HLS = 'https://fixture.invalid/api/streams/hls/tok123/index.m3u8'

CLASSIC = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
           "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")

MEDIA = r'''
window.__gum=[];window.__gdm=0;window.__srvTracks=[];window.__whip=[];window.__whipDel=[];window.__setParams=[];
window.__ingestMode=window.__ingestMode||'ok';window.__whipMode=window.__whipMode||'ok';window.__ingestCalls=0;
window.__publishOK=true;
(()=>{
 const mkVideo=(color,w,h)=>{const c=document.createElement('canvas');c.width=w;c.height=h;const x=c.getContext('2d');let f=0;
   const paint=()=>{x.fillStyle=color;x.fillRect(0,0,w,h);x.fillStyle='#ffffff';x.fillRect((f++*9)%(w-20),4,16,16);};paint();setInterval(paint,33);
   return c.captureStream(30);};
 let ac=null;
 const tone=(freq)=>{ac=ac||new AudioContext();try{ac.resume()}catch(_){}
   const o=ac.createOscillator();o.frequency.value=freq;const g=ac.createGain();g.gain.value=0.5;const d=ac.createMediaStreamDestination();
   o.connect(g);g.connect(d);o.start();return d.stream;};
 window.__noScreen=window.__noScreen||false;
 const md=navigator.mediaDevices;
 if(window.__noScreen){ try{ Object.defineProperty(md,'getDisplayMedia',{value:undefined,configurable:true}); }catch(_){ md.getDisplayMedia=undefined; } }
 else md.getDisplayMedia=async(c)=>{__gdm++;const s=new MediaStream(mkVideo('#0000ff',1280,720).getVideoTracks());
   if(c&&c.audio)tone(330).getAudioTracks().forEach(t=>s.addTrack(t));return s;};
 md.getUserMedia=async(c)=>{__gum.push(JSON.parse(JSON.stringify(c||{})));const s=new MediaStream();
   if(c&&c.video)mkVideo('#00ff00',640,360).getVideoTracks().forEach(t=>s.addTrack(t));
   if(c&&c.audio)tone(440).getAudioTracks().forEach(t=>s.addTrack(t));return s;};
 md.enumerateDevices=async()=>[{kind:'audioinput',deviceId:'mic-a',label:'Fixture Mic A',groupId:'a'},
   {kind:'audioinput',deviceId:'mic-b',label:'Fixture Mic B',groupId:'b'},{kind:'videoinput',deviceId:'cam-a',label:'Fixture Cam',groupId:'c'}];
 const sp=RTCRtpSender.prototype.setParameters;
 RTCRtpSender.prototype.setParameters=function(p){if(this.track&&this.track.kind==='video')__setParams.push(JSON.parse(JSON.stringify(p.encodings||[])));return sp.call(this,p);};
 const json=(d,st)=>new Response(JSON.stringify(d),{status:st||200,headers:{'Content-Type':'application/json'}});
 const gather=pc=>new Promise(r=>{if(pc.iceGatheringState==='complete')return r();pc.addEventListener('icegatheringstatechange',()=>{if(pc.iceGatheringState==='complete')r()});setTimeout(r,3000);});
 const inner=window.fetch;
 window.fetch=async function(url,opts={}){const u=String(url);
  if(u.includes('/api/streams/ingest')){__ingestCalls++;
    if(__ingestMode==='down')throw new TypeError('fixture: instance unreachable');
    if(__ingestMode==='refused')return json({enabled:false,error:'no_permission',message:'Live streaming isn’t enabled for your account yet — ask an admin for access.'});
    if(__ingestMode==='off')return json({enabled:false,message:'Live streaming is turned off on this server.'});
    return json({enabled:true,server_up:__ingestMode!=='mediaserver_down',token:'tok123',rtmp_url:'rtmp://fixture.invalid:1935',stream_key:'tok123?key=k123',
      hls_url:'HLS',whip_url:location.origin+'/api/streams/whip/tok123',record_available:false});}
  if(u.includes('/api/streams/whip/')&&String(opts.method||'').toUpperCase()==='DELETE'){__whipDel.push(u);return new Response('',{status:200});}
  if(u.includes('/api/streams/whip/')){
    if(__whipMode==='fail')return new Response('',{status:502});
    __whip.push(String(opts.body||''));
    const pc=new RTCPeerConnection();window.__srv=pc;pc.ontrack=e=>__srvTracks.push(e.track.kind);
    await pc.setRemoteDescription({type:'offer',sdp:String(opts.body)});await pc.setLocalDescription(await pc.createAnswer());await gather(pc);
    return new Response(pc.localDescription.sdp,{status:201,headers:{'Content-Type':'application/sdp','Location':'/api/streams/whip/tok123/SID'}});}
  if(u.includes('/api/streams/sentinel'))return json({ok:true});
  if(u.includes('/api/streams/viewers/'))return json({viewers:3});
  if(u.includes('/upload')&&window.__uploadFails)return new Response('no',{status:500});
  return inner(url,opts);};
 const send=WebSocket.prototype.send;
 WebSocket.prototype.send=function(raw){try{const m=JSON.parse(raw);
   if(Array.isArray(m)&&m[0]==='REQ'&&window.__silent30078&&m.slice(2).some(f=>(f.kinds||[]).includes(30078)))return;}catch(_){}
   return send.call(this,raw);};
})();
'''.replace('HLS', HLS).replace('SID', SID)


def png(rgb, w=64, h=64):
    """A solid-colour PNG, built here so the test knows every pixel of the overlay."""
    raw = b''.join(b'\x00' + bytes(rgb) * w for _ in range(h))
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def init(**flags):
    pre = ''.join('window.%s=%s;' % (k, json.dumps(v)) for k, v in flags.items())
    return CLASSIC + pre + MEDIA


# How many canvas pixels are close to a colour, inside an optional normalised box -- the compositor's OUTPUT.
COUNT = r'''((rgb,box,tol)=>{const c=document.querySelector('#pbs-canvas');const x=c.getContext('2d');
  const b=box||[0,0,1,1];const X=Math.floor(b[0]*c.width),Y=Math.floor(b[1]*c.height),W=Math.max(1,Math.floor(b[2]*c.width)),H=Math.max(1,Math.floor(b[3]*c.height));
  const d=x.getImageData(X,Y,W,H).data;let n=0,sx=0,sy=0;for(let i=0;i<d.length;i+=4){
   if(Math.abs(d[i]-rgb[0])<=tol&&Math.abs(d[i+1]-rgb[1])<=tol&&Math.abs(d[i+2]-rgb[2])<=tol){n++;const p=i/4;sx+=p%W;sy+=Math.floor(p/W);}}
  return {n,cx:n?(X+sx/n)/c.width:0,cy:n?(Y+sy/n)/c.height:0,w:c.width,h:c.height};})'''


async def count(b, rgb, box=None, tol=40):
    return await b.js('%s(%s,%s,%d)' % (COUNT, json.dumps(list(rgb)), json.dumps(box), tol))


async def open_pbs(b, width=1280):
    from tests.client import test_desktop_offline_full_app as desktop
    await b.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': width < 600})
    await desktop.login(b)
    await b.js("__PC.switchView('pbs');true")
    await b.until("!!document.querySelector('#pbs #pbs-canvas')")
    await asyncio.sleep(.5)


async def click(b, selector):
    from tests.client.test_profile_search_full_app import _click
    await b.js("(()=>{const e=document.querySelector(%s);if(e)e.scrollIntoView({block:'center'});return true})()" % json.dumps(selector))
    await asyncio.sleep(.15)
    await _click(b, selector)


async def drag(b, x0, y0, x1, y1, steps=8):
    await b.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x0, 'y': y0})
    await b.call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': x0, 'y': y0, 'button': 'left', 'clickCount': 1})
    for i in range(1, steps + 1):
        await b.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x0 + (x1 - x0) * i / steps,
                                                  'y': y0 + (y1 - y0) * i / steps, 'button': 'left', 'buttons': 1})
        await asyncio.sleep(.02)
    await b.call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': x1, 'y': y1, 'button': 'left', 'clickCount': 1})


async def canvas_to_page(b, nx, ny):
    """Page coordinates of a normalised canvas point (where the person would put the mouse) -- with the preview
    scrolled into view first, as a person would have it before dragging on it."""
    await b.js("document.querySelector('#pbs-canvas').scrollIntoView({block:'center'});true")
    await asyncio.sleep(.15)
    r = await b.js("(()=>{const r=document.querySelector('#pbs-canvas').getBoundingClientRect();return [r.left,r.top,r.width,r.height]})()")
    return r[0] + nx * r[2], r[1] + ny * r[3]


async def set_file(b, selector, path):
    doc = await b.call('DOM.getDocument', {'depth': 0})
    node = await b.call('DOM.querySelector', {'nodeId': doc['root']['nodeId'], 'selector': selector})
    await b.call('DOM.setFileInputFiles', {'nodeId': node['nodeId'], 'files': [str(path)]})


async def shot(b, name):
    if not SHOTS:
        return
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    data = (await b.call('Page.captureScreenshot', {'format': 'png'}))['data']
    (Path(SHOTS) / name).write_bytes(base64.b64decode(data))


PUBLISHED = r'''((kind,d)=>__published.filter(e=>e.kind===kind&&(!d||e.tags.some(t=>t[0]==='d'&&t[1]===d))))'''


async def published(b, kind, d=None):
    return await b.js('%s(%d,%s)' % (PUBLISHED, kind, json.dumps(d)))
