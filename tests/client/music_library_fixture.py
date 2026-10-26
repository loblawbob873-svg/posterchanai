"""A Music library shaped like a real one -- for the Songs / Albums / Artists views, the tag scan and the opt-in covers.

Eight tracks handed to the SHIPPED client through the shared-track seam (`m.shared` -> PCMusicShare.plain, as
music_art_fixture.py does), each a real playable WAV whose `id3 ` chunk (after the audio, where taggers write it)
carries TIT2/TPE1/TALB/TPE2/TRCK/TPOS/TYER and, for two albums, an APIC cover:

  Neon Skyline   -- Kaito Vega, 2021, 4 songs over 2 discs, uploaded out of order, "2/4" style numbers, a cover
  City Lights    -- a compilation (album artist "Various Artists", two track artists), a cover
  No Cover Club  -- Ghost Band, one song, NO picture: the only album the online lookup may ask about
  mystery_track  -- no tags at all: "Unknown album" / "Unknown artist", never dropped

Knobs: `__plainDelay[sha]=ms`, `__plainFail[sha]=true`, `__plainCalls` (every decrypt the scan or player asked for),
`__coverReqs` (every request to /api/music/cover), `__coverAnswer` ('ok' | 'none' | '503').
`__seedLib({indexed})` puts the tracks in the Music folder (merging -- a reload keeps what the scan wrote);
`__seedBig(n)` a library of n tracks over n/10 albums, already indexed (the lazy-render check).
"""
import base64
import json
import struct
import zlib


def png(rgb, size=24):
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def id3(frames, cover=None):
    def frame(fid, body):
        return fid.encode() + struct.pack(">I", len(body)) + b"\0\0" + body
    body = b"".join(frame(f, b"\x03" + v.encode()) for f, v in frames if v)
    if cover:
        body += frame("APIC", b"\x00image/png\x00\x03\x00" + cover)
    n = len(body)
    return b"ID3\x03\x00\x00" + bytes([(n >> 21) & 0x7f, (n >> 14) & 0x7f, (n >> 7) & 0x7f, n & 0x7f]) + body


def sha(c):
    return c * 64


MAGENTA, CYAN = (255, 0, 200), (0, 220, 255)
# sha char -> (file name, upload ts, tags, cover)
TRACKS = {
    "a": ("night_drive", 1005, dict(TIT2="Night Drive", TPE1="Kaito Vega", TALB="Neon Skyline", TRCK="2/4", TPOS="1/2", TYER="2021"), MAGENTA),
    "b": ("bonus_cut", 1008, dict(TIT2="Bonus Cut", TPE1="Kaito Vega", TALB="neon  skyline", TRCK="1", TPOS="2/2", TYER="2021"), MAGENTA),
    "c": ("intro", 1001, dict(TIT2="Intro", TPE1="Kaito Vega", TALB="Neon Skyline", TRCK="1/4", TPOS="1/2", TYER="2021"), MAGENTA),
    "d": ("afterglow", 1003, dict(TIT2="Afterglow", TPE1="Kaito Vega", TALB="Neon Skyline", TRCK="03/04", TYER="2021"), MAGENTA),
    "e": ("rain", 1004, dict(TIT2="Rain", TPE1="Mira Sol", TALB="City Lights", TPE2="Various Artists", TRCK="2", TYER="2019"), CYAN),
    "f": ("sun", 1002, dict(TIT2="Sun", TPE1="Kaito Vega", TALB="City Lights", TPE2="Various Artists", TRCK="1", TYER="2019"), CYAN),
    "0": ("ghost_song", 1006, dict(TIT2="Ghost Song", TPE1="Ghost Band", TALB="No Cover Club", TRCK="1", TYER="2018"), None),
    "9": ("mystery_track", 1007, {}, None),
}
SHAS = {k: sha(k) for k in TRACKS}
CHUNKS = {sha(k): base64.b64encode(id3(list(t[2].items()), png(t[3]) if t[3] else None)).decode() if t[2] else ""
          for k, t in TRACKS.items()}
FILES = {sha(k): {"name": t[0], "ts": t[1]} for k, t in TRACKS.items()}
COVER_PNG = base64.b64encode(png((255, 200, 0), 64)).decode()        # what the fake cover service hands back
COVER_ID = "abcdef12-3456-7890-abcd-ef1234567890"

LIB = r"""
(()=>{
  const CHUNKS=__CHUNKS__, FILES=__FILES__, COVER=__COVER__, COVER_ID=__COVER_ID__;
  const b64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
  const wav=(tag)=>{const sr=8000,n=sr*20,extra=tag?8+tag.length+(tag.length&1):0,b=new ArrayBuffer(44+n*2+extra),v=new DataView(b),
      w=(o,s)=>{for(let i=0;i<s.length;i++)v.setUint8(o+i,s.charCodeAt(i))};
    w(0,'RIFF');v.setUint32(4,36+n*2+extra,true);w(8,'WAVE');w(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);
    v.setUint32(24,sr,true);v.setUint32(28,sr*2,true);v.setUint16(32,2,true);v.setUint16(34,16,true);w(36,'data');v.setUint32(40,n*2,true);
    for(let i=0;i<n;i++) v.setInt16(44+i*2,Math.round(Math.sin(i/9)*3000),true);
    if(tag){const o=44+n*2;w(o,'id3 ');v.setUint32(o+4,tag.length,true);new Uint8Array(b,o+8,tag.length).set(tag);}
    return new Uint8Array(b);};
  window.__plainCalls=[]; window.__plainDelay={}; window.__plainFail={}; window.__coverReqs=[]; window.__coverAnswer='ok';
  // `__plainSerial`: one decrypt at a time, like ONE network pipe -- a scan that fanned out would queue a song
  // somebody pressed play on behind every read it had started.
  window.__plainSerial=false; let pipe=Promise.resolve();
  const read=async(sha)=>{ const d=__plainDelay[sha]; if(d) await new Promise(r=>setTimeout(r,d));
    if(__plainFail[sha]) throw new Error('fixture: this track cannot be read');
    return wav(CHUNKS[sha]?b64(CHUNKS[sha]):null); };
  const plain=async(sha)=>{ __plainCalls.push(sha);
    if(!__plainSerial) return read(sha);
    const go=pipe.then(()=>read(sha)); pipe=go.catch(()=>{}); return go; };
  let real=null;
  Object.defineProperty(window,'PCMusicShare',{configurable:true,
    get(){ if(real){ real.plain=plain; return real; }
      return new Proxy({plain, meta:()=>null},{get:(t,k)=>k in t?t[k]:(typeof k==='string'?()=>'':undefined)}); },
    set(v){ real=v; }});
  // The media server holds every track; the cover service is this fake (counting what it is asked).
  const f=window.fetch;
  window.fetch=(u,o)=>{ const s=String(u);
    if(/\/list\/[0-9a-f]{64}/.test(s)){
      const all=Object.keys(__PC.filesIdx()._norm().files);
      return Promise.resolve(new Response(JSON.stringify(all.map(x=>({sha256:x,size:1000}))),{status:200,headers:{'Content-Type':'application/json'}}));
    }
    if(s.includes('/api/music/cover')){
      __coverReqs.push({url:s, method:(o&&o.method)||'GET', body:(o&&o.body)||''});
      const a=__coverAnswer;
      if(a==='503') return Promise.resolve(new Response('{"detail":"down"}',{status:503,headers:{'Content-Type':'application/json'}}));
      if(a==='none') return Promise.resolve(new Response('{"detail":"none"}',{status:404,headers:{'Content-Type':'application/json'}}));
      if(/\/api\/music\/cover\/[0-9a-f-]{36}/.test(s)) return Promise.resolve(new Response(b64(COVER),{status:200,headers:{'Content-Type':'image/png'}}));
      return Promise.resolve(new Response(JSON.stringify({id:COVER_ID}),{status:200,headers:{'Content-Type':'application/json'}}));
    }
    return f(u,o); };
  // Merge, never replace: a reload keeps what the scan already wrote into each record.
  window.__seedLib=(opts)=>{
    const F=__PC.filesIdx();
    // This device's saved copy first, as renderMusicApp does -- or a reload's seed would overwrite what the scan wrote.
    try{ F.loadLocal(); }catch(_){ }
    F._norm();
    for(const [sha, m] of Object.entries(FILES)){
      const cur=F.data.files[sha]||{};
      F.data.files[sha]=Object.assign({folder:'Music',enc:true,shared:true,mime:'audio/wav'}, m, cur.tg?{tg:cur.tg}:{});
      if(opts && opts.indexed===false) delete F.data.files[sha].tg;
    }
    F._pullDone=true; F._pullOk=true;
    // The fixture has no relay to save the encrypted index to: this device's copy (localStorage) is the record.
    F._save=async()=>{ F.saveLocal(); return true; };
    F.saveLocal(); return true;
  };
  window.__seedBig=(n)=>{
    const F=__PC.filesIdx(); F._norm(); F.data.files={};
    for(let i=0;i<n;i++){ const sha=(i.toString(16).padStart(8,'0')).repeat(8);
      F.data.files[sha]={folder:'Music',enc:true,shared:true,mime:'audio/wav',name:'big '+i,ts:5000-i,
        tg:{v:1,t:'Song '+i,a:'Artist '+(Math.floor(i/10)%25),al:'Album '+Math.floor(i/10),n:(i%10)+1}}; }
    F._pullDone=true; F._pullOk=true; F._save=async()=>{ F.saveLocal(); return true; }; F.saveLocal(); return true;
  };
})();
""".replace("__CHUNKS__", json.dumps(CHUNKS)).replace("__FILES__", json.dumps(FILES)) \
   .replace("__COVER_ID__", json.dumps(COVER_ID)).replace("__COVER__", json.dumps(COVER_PNG))

PLAIN = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),"
         "osMode:false}));")
