"""A Music library whose tracks say who they are -- for the player's album-art and metadata tests.

Five tracks in the Music folder, fed to the SHIPPED player through the shared-track seam (`m.shared` ->
PCMusicShare.plain), exactly as test_desktop_music_widget_plays_full_app.py does. The bytes are a real,
playable WAV (so <audio> plays them and the transport can be driven), and tracks 1 and 2 carry an `id3 `
RIFF chunk AFTER the audio -- where taggers put it -- holding TIT2/TPE1/TALB and an APIC PNG cover, each a
different colour. Tracks 3-5 are untagged, the common case, and must draw the generated cover.

`window.__delay[sha] = ms` holds a track's bytes back, to make a skip land while a load is in flight.
`window.__revoked` records every URL.revokeObjectURL.
"""
import base64
import json
import struct
import zlib


def png(rgb, size=16):
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def id3(title, artist, album, cover):
    def frame(fid, body):
        return fid.encode() + struct.pack(">I", len(body)) + b"\0\0" + body

    def text(s):
        return b"\x03" + s.encode()
    body = (frame("TIT2", text(title)) + frame("TPE1", text(artist)) + frame("TALB", text(album))
            + frame("APIC", b"\x00image/png\x00\x03\x00" + cover))
    size = len(body)
    ss = bytes([(size >> 21) & 0x7f, (size >> 14) & 0x7f, (size >> 7) & 0x7f, size & 0x7f])
    return b"ID3\x03\x00\x00" + ss + body


SHA = {i: str(i) * 64 for i in range(1, 6)}
TAGS = {
    1: dict(title="Neon Rain", artist="Kaito Vega", album="Night City", rgb=(255, 0, 200)),
    2: dict(title="Chrome Hearts", artist="Mira Sol", album="Afterglow", rgb=(0, 220, 255)),
}
CHUNKS = {SHA[i]: base64.b64encode(id3(t["title"], t["artist"], t["album"], png(t["rgb"]))).decode()
          for i, t in TAGS.items()}

LIB = r"""
(()=>{
  const CHUNKS=%s;
  const b64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
  // 30 s of a quiet tone at 8 kHz -- long enough that nothing ends (and auto-advances) mid-test.
  const wav=(tag)=>{const sr=8000,n=sr*30,extra=tag?8+tag.length+(tag.length&1):0,b=new ArrayBuffer(44+n*2+extra),v=new DataView(b),
      w=(o,s)=>{for(let i=0;i<s.length;i++)v.setUint8(o+i,s.charCodeAt(i))};
    w(0,'RIFF');v.setUint32(4,36+n*2+extra,true);w(8,'WAVE');w(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);
    v.setUint32(24,sr,true);v.setUint32(28,sr*2,true);v.setUint16(32,2,true);v.setUint16(34,16,true);w(36,'data');v.setUint32(40,n*2,true);
    for(let i=0;i<n;i++)v.setInt16(44+i*2,Math.round(Math.sin(i/8)*300),true);
    if(tag){const o=44+n*2;w(o,'id3 ');v.setUint32(o+4,tag.length,true);new Uint8Array(b,o+8,tag.length).set(tag);}
    return new Uint8Array(b);};
  window.__stubPlays=[]; window.__delay={}; window.__revoked=[];
  // `__tagDelayOnce = ms` holds back the NEXT tag read (audiotags.js loads on demand, so wrap it as it arrives).
  { let tags=null; Object.defineProperty(window,'PCAudioTags',{configurable:true, get(){ return tags; },
      set(v){ const rb=v.readBlob; v.readBlob=async b=>{ const d=window.__tagDelayOnce; window.__tagDelayOnce=0;
        if(d) await new Promise(r=>setTimeout(r,d)); return rb(b); }; tags=v; }}); }
  { const rv=URL.revokeObjectURL.bind(URL); URL.revokeObjectURL=u=>{ window.__revoked.push(String(u)); return rv(u); }; }
  const plain=async(sha)=>{ __stubPlays.push(sha);
    const d=__delay[sha]; if(d) await new Promise(r=>setTimeout(r,d));
    return wav(CHUNKS[sha]?b64(CHUNKS[sha]):null); };
  let real=null;
  Object.defineProperty(window,'PCMusicShare',{configurable:true,
    get(){ if(real){ real.plain=plain; return real; }
      return new Proxy({plain, meta:()=>null},{get:(t,k)=>k in t?t[k]:(typeof k==='string'?()=>'':undefined)}); },
    set(v){ real=v; }});
  window.__seedLib=()=>{
    const F=__PC.filesIdx(); F._norm();
    for(let i=1;i<=5;i++) F.data.files[String(i).repeat(64)]={folder:'Music',enc:true,shared:true,name:'Stub Song '+i,mime:'audio/wav',ts:2000-i};
    F._pullDone=true; F._pullOk=true; return true;
  };
})();
""" % json.dumps(CHUNKS)

# The media server holds every track's bytes, so none is listed as missing.
HELD = r"""(()=>{const f=window.fetch;window.fetch=(u,o)=>{if(/\/list\/[0-9a-f]{64}/.test(String(u)))
  return Promise.resolve(new Response(JSON.stringify([1,2,3,4,5].map(i=>({sha256:String(i).repeat(64),size:1000}))),{status:200,headers:{'Content-Type':'application/json'}}));
  return f(u,o);};})();"""

PLAIN = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),"
         "osMode:false}));")

# What the player and the page say right now, in one read.
STATE = r"""(()=>{const P=__PC.music(), n=P.now()||{}, a=document.querySelector('audio')||null, MP=__PC.MusicPlayer;
  const art=sel=>{const e=document.querySelector(sel); if(!e) return null; const p=e.matches('.pc-art')?e:e.querySelector('.pc-art');
    const i=e.querySelector('img'); return {kind:p?p.dataset.art:'', src:i?i.getAttribute('src'):'', loaded:!!(i&&i.complete&&i.naturalWidth>0)};};
  const md=navigator.mediaSession&&navigator.mediaSession.metadata;
  return {cur:MP.cur||'', title:n.title||'', artist:n.artist||'', album:n.album||'', art:n.art||'', playing:!!n.playing,
    t:n.t||0, d:n.d||0, shuffle:!!MP.shuffle,
    appTitle:(document.getElementById('ma-title')||{}).textContent||'', appArtist:(document.getElementById('ma-artist')||{}).textContent||'',
    appArt:art('#ma-art'), widgetArt:art('#music-player .mp-art'), miniArt:art('#music-player .mp-thumb'),
    widgetArtist:((document.querySelector('#music-player .mp-artist')||{}).textContent||''),
    widgetHidden:!!(document.getElementById('music-player')||{classList:{contains:()=>true}}).classList.contains('hidden'),
    full:!!document.querySelector('#feed .music-app.ma-full'),
    md:md?{title:md.title,artist:md.artist,album:md.album,art:(md.artwork||[]).map(x=>x.src)}:null,
    revoked:(window.__revoked||[]).slice()};})()"""


async def state(b):
    return await b.js(STATE)


async def until_state(b, ok, timeout=15.0, what=""):
    import asyncio
    got = None
    for _ in range(int(timeout / .2)):
        got = await state(b)
        if ok(got):
            return got
        await asyncio.sleep(.2)
    raise AssertionError({"waiting for": what, "state": got})


async def press(b, selector, within=None):
    """A REAL mouse press at the centre of the element, after proving the element is what is under that point --
    a control covered by the bottom nav, a sibling or off screen fails here rather than being clicked through."""
    sel = json.dumps(selector)
    await b.until(f"!!document.querySelector({sel})")
    import asyncio
    hit = "never measured"
    for _ in range(4):    # a widget mid-rebuild (a cover just arrived) is not "covered"; one that stays covered is
        # Scrolled to only when it is not already pressable where it is: scrollIntoView also scrolls an
        # overflow:hidden panel (the full-screen widget), shifting its content into a state nobody ever sees.
        xy = await b.js(f"(()=>{{const e=document.querySelector({sel});let r=e.getBoundingClientRect();"
                        f"const at=()=>{{const h=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);return !!(h&&h.closest({sel}))}};"
                        "if(r.top<0||r.bottom>innerHeight||!at()){e.scrollIntoView({block:'center',inline:'nearest'});r=e.getBoundingClientRect();}"
                        "return {x:r.left+r.width/2,y:r.top+r.height/2,w:r.width,h:r.height}})()")
        hit = await b.js(f"(()=>{{const h=document.elementFromPoint({xy['x']},{xy['y']});if(h&&h.closest({sel}))return '';"
                         "return h?(h.tagName+'#'+h.id+'.'+String(h.className.baseVal??h.className)+' '+(h.textContent||'').trim().slice(0,60)):'nothing'})()")
        if not hit:
            break
        await asyncio.sleep(.15)
    assert not hit, f"{selector} is covered at its own centre {xy} by {hit}"
    for t in ("mousePressed", "mouseReleased"):
        await b.call("Input.dispatchMouseEvent", {"type": t, "x": xy["x"], "y": xy["y"], "button": "left", "clickCount": 1})
    return xy


async def drag(b, selector, frm, to):
    """Press at fraction `frm` along the element, move to `to`, release: a seek drag."""
    sel = json.dumps(selector)
    r = await b.js(f"(()=>{{const e=document.querySelector({sel});const r=e.getBoundingClientRect();return [r.left,r.top,r.width,r.height]}})()")
    y = r[1] + r[3] / 2
    x0, x1 = r[0] + r[2] * frm, r[0] + r[2] * to
    await b.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x0, "y": y, "button": "left", "clickCount": 1})
    for k in range(1, 6):
        await b.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x0 + (x1 - x0) * k / 5, "y": y, "button": "left", "buttons": 1})
    await b.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x1, "y": y, "button": "left", "clickCount": 1})


# Layout audit of a player surface: what check_client_mobile.py asks of every screen, plus what a player owes a
# thumb. `root` is the surface; returns a list of problems (empty = clean).
AUDIT = r"""((root, navTop) => {
  const out=[], vw=innerWidth, vh=innerHeight, R=document.querySelector(root);
  if(!R) return ['no '+root];
  if(document.documentElement.scrollWidth>vw+1) out.push('page scrolls sideways ('+document.documentElement.scrollWidth+' > '+vw+')');
  const vis=e=>e.checkVisibility?e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}):!!e.offsetParent;
  R.querySelectorAll('svg.ic').forEach(s=>{ if(!vis(s)) return; const r=s.getBoundingClientRect();
    if(r.width<4||r.height<4) out.push('zero-sized icon '+((s.querySelector('use')||{}).getAttribute?.('href')||'')); });
  R.querySelectorAll('button').forEach(b=>{ if(!vis(b)) return; const r=b.getBoundingClientRect(); if(!r.width) return;
    const name=b.id||b.className||b.textContent.trim().slice(0,20);
    if(r.left<-1||r.right>vw+1||r.top<-1||r.bottom>vh+1) out.push('off screen: '+name+' '+[r.left,r.top,r.right,r.bottom].map(Math.round));
    if(navTop!=null && r.bottom>navTop+1 && r.top<navTop+60) out.push('under the bottom nav: '+name);
    const h=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
    if(r.top>=0&&r.bottom<=vh&&h&&!b.contains(h)&&!h.contains(b)) out.push('covered: '+name+' by '+(h.id||h.className));
    const only=b.children.length===1&&b.textContent.trim()===''&&b.firstElementChild;
    if(only && only.matches('svg.ic')){ const i=only.getBoundingClientRect();
      const dx=(i.left+i.width/2)-(r.left+r.width/2), dy=(i.top+i.height/2)-(r.top+r.height/2);
      if(Math.abs(dx)>2.5||Math.abs(dy)>2.5) out.push('off-centre icon in '+name+' dx='+dx.toFixed(1)+' dy='+dy.toFixed(1)); } });
  R.querySelectorAll('img').forEach(i=>{ if(!vis(i)) return; const r=i.getBoundingClientRect();
    if(r.width>20&&r.height>20&&Math.abs(r.width/r.height-1)>0.05&&i.closest('.pc-art')) out.push('stretched cover '+Math.round(r.width)+'x'+Math.round(r.height)); });
  R.querySelectorAll('#ma-title,#ma-artist,.mp-now,.mp-artist').forEach(t=>{ if(!vis(t)) return; const r=t.getBoundingClientRect();
    if(r.right>vw+1||r.left<-1) out.push('text runs off the side: '+(t.id||t.className)); });
  return out;
})"""


async def audit(b, root, nav=True):
    nav_top = await b.js("(()=>{const n=document.querySelector('.mobilenav');if(!n||getComputedStyle(n).display==='none')return null;"
                         "const r=n.getBoundingClientRect();return r.height?r.top:null})()") if nav else None
    return await b.js(f"({AUDIT})({json.dumps(root)},{json.dumps(nav_top)})")
