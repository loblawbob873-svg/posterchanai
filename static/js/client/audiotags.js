/* WHAT A MUSIC FILE SAYS ABOUT ITSELF: title, artist, album and cover art.
 *
 * "try to get it to show the audio metadata like ditto/amethyst does it". A posted track used to be named
 * after its FILE ("Night_City_Drive.mp3"), while the file itself usually carries the real title, the
 * artist and the cover. This reads them from the first bytes of the file — the tags live at the front for
 * every format handled here — without decoding any audio:
 *
 *   ID3v2.2/2.3/2.4 (MP3)  TIT2/TPE1/TALB/APIC (TT2/TP1/TAL/PIC in 2.2), all four text encodings
 *   FLAC                   VORBIS_COMMENT (block 4) and PICTURE (block 6)
 *   Ogg Vorbis / Opus      the comment packet, reassembled across pages (cover art spans several),
 *                          with METADATA_BLOCK_PICTURE decoded like a FLAC picture
 *
 * DOM-free and dependency-free, so tests/client/test_audio_tags.py runs it under node on real tag bytes.
 * A file it does not understand answers {} — never throws into the post that holds it.
 */
(function(root){
  'use strict';
  const td = (enc) => { try{ return new TextDecoder(enc); }catch(_){ return new TextDecoder('utf-8'); } };
  const latin1 = td('latin1'), utf8 = td('utf-8'), u16le = td('utf-16le'), u16be = td('utf-16be');
  const clean = s => String(s || '').replace(/\u0000+$/,'').replace(/\u0000/g,' / ').trim();
  const be32 = (b, i) => ((b[i] << 24) | (b[i+1] << 16) | (b[i+2] << 8) | b[i+3]) >>> 0;
  const le32 = (b, i) => (b[i] | (b[i+1] << 8) | (b[i+2] << 16) | (b[i+3] << 24)) >>> 0;
  const syncsafe = (b, i) => ((b[i] & 0x7f) << 21) | ((b[i+1] & 0x7f) << 14) | ((b[i+2] & 0x7f) << 7) | (b[i+3] & 0x7f);
  const ascii = (b, i, n) => String.fromCharCode.apply(null, b.subarray(i, i + n));

  function text(enc, b){
    if(enc === 0) return clean(latin1.decode(b));
    if(enc === 3) return clean(utf8.decode(b));
    if(enc === 2) return clean(u16be.decode(b));
    // 1: UTF-16 with a byte-order mark
    if(b[0] === 0xfe && b[1] === 0xff) return clean(u16be.decode(b.subarray(2)));
    if(b[0] === 0xff && b[1] === 0xfe) return clean(u16le.decode(b.subarray(2)));
    return clean(u16le.decode(b));
  }
  // The end of a NUL-terminated string in encoding `enc` (one NUL byte, or two aligned for UTF-16).
  function nulEnd(b, i, enc){
    if(enc === 1 || enc === 2){ for(let j = i; j + 1 < b.length; j += 2) if(!b[j] && !b[j+1]) return j; return b.length; }
    for(let j = i; j < b.length; j++) if(!b[j]) return j;
    return b.length;
  }

  /* ID3 needs `size` bytes in total; tell the caller how many to fetch before parsing. */
  function needed(b){
    if(b.length >= 10 && ascii(b, 0, 3) === 'ID3') return 10 + syncsafe(b, 6) + ((b[5] & 0x10) ? 10 : 0);
    return 0;
  }

  function id3(b){
    const ver = b[3], flags = b[5], size = syncsafe(b, 6), end = Math.min(b.length, 10 + size), out = {};
    let i = 10;
    if((flags & 0x40) && ver >= 3) i += (ver === 4 ? syncsafe(b, i) : be32(b, i) + 4);   // extended header
    const v22 = ver === 2, idLen = v22 ? 3 : 4, hdr = v22 ? 6 : 10;
    const names = v22 ? { TT2:'title', TP1:'artist', TAL:'album', PIC:'pic' }
                      : { TIT2:'title', TPE1:'artist', TALB:'album', APIC:'pic' };
    while(i + hdr <= end){
      const id = ascii(b, i, idLen);
      if(!/^[A-Z0-9]+$/.test(id)) break;                     // padding
      const len = v22 ? ((b[i+3] << 16) | (b[i+4] << 8) | b[i+5]) : (ver === 4 ? syncsafe(b, i + 4) : be32(b, i + 4));
      const body = b.subarray(i + hdr, Math.min(end, i + hdr + len));
      const key = names[id];
      if(key && body.length){
        const enc = body[0];
        if(key === 'pic'){
          let j = 1, mime;
          if(v22){ const f = ascii(body, 1, 3).toLowerCase(); mime = f === 'png' ? 'image/png' : 'image/jpeg'; j = 4; }
          else { const z = nulEnd(body, 1, 0); mime = latin1.decode(body.subarray(1, z)) || 'image/jpeg'; j = z + 1; }
          j += 1;                                             // picture type
          j = nulEnd(body, j, enc) + ((enc === 1 || enc === 2) ? 2 : 1);   // description
          if(!out.picture && j < body.length) out.picture = { mime: /\//.test(mime) ? mime : 'image/' + mime.toLowerCase(), data: body.slice(j) };
        } else if(!out[key]) out[key] = text(enc, body.subarray(1));
      }
      i += hdr + len;
    }
    return out;
  }

  function vorbisComments(b, i){
    const out = {};
    if(i + 4 > b.length) return out;
    const vlen = le32(b, i); i += 4 + vlen;
    if(i + 4 > b.length) return out;
    const n = le32(b, i); i += 4;
    for(let k = 0; k < n && i + 4 <= b.length; k++){
      const len = le32(b, i); i += 4;
      const s = utf8.decode(b.subarray(i, i + len)); i += len;
      const eq = s.indexOf('='); if(eq < 1) continue;
      const key = s.slice(0, eq).toUpperCase(), val = s.slice(eq + 1);
      if(key === 'TITLE' && !out.title) out.title = clean(val);
      else if(key === 'ARTIST' && !out.artist) out.artist = clean(val);
      else if(key === 'ALBUM' && !out.album) out.album = clean(val);
      else if(key === 'METADATA_BLOCK_PICTURE' && !out.picture){
        try{ const raw = Uint8Array.from(atob(val), c => c.charCodeAt(0)); out.picture = flacPicture(raw, 0); }catch(_){ }
      }
    }
    return out;
  }

  function flacPicture(b, i){
    i += 4;                                                   // picture type
    const ml = be32(b, i); i += 4; const mime = latin1.decode(b.subarray(i, i + ml)); i += ml;
    const dl = be32(b, i); i += 4 + dl;                       // description
    i += 16;                                                  // width, height, depth, colours
    const n = be32(b, i); i += 4;
    return { mime: mime || 'image/jpeg', data: b.slice(i, i + n) };
  }

  function flac(b){
    let i = 4, out = {};
    while(i + 4 <= b.length){
      const last = b[i] & 0x80, type = b[i] & 0x7f, len = (b[i+1] << 16) | (b[i+2] << 8) | b[i+3];
      const at = i + 4;
      if(type === 4) Object.assign(out, Object.fromEntries(Object.entries(vorbisComments(b, at)).filter(([k]) => !out[k])));
      if(type === 6 && !out.picture && at + len <= b.length) out.picture = flacPicture(b, at);
      i = at + len;
      if(last) break;
    }
    return out;
  }

  // Ogg: reassemble packets from pages, then find the comment packet (Vorbis type 3, or OpusTags).
  function ogg(b){
    let i = 0, cur = [], packets = [];
    while(i + 27 <= b.length && ascii(b, i, 4) === 'OggS' && packets.length < 3){
      const segs = b[i + 26], table = b.subarray(i + 27, i + 27 + segs);
      let p = i + 27 + segs;
      for(let s = 0; s < segs; s++){
        const n = table[s];
        cur.push(b.subarray(p, p + n)); p += n;
        if(n < 255){
          const len = cur.reduce((a, x) => a + x.length, 0), pk = new Uint8Array(len); let o = 0;
          for(const x of cur){ pk.set(x, o); o += x.length; }
          packets.push(pk); cur = [];
        }
      }
      i = p;
    }
    for(const pk of packets){
      if(pk[0] === 3 && ascii(pk, 1, 6) === 'vorbis') return vorbisComments(pk, 7);
      if(ascii(pk, 0, 8) === 'OpusTags') return vorbisComments(pk, 8);
    }
    return {};
  }

  function parse(bytes){
    try{
      const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
      if(b.length < 12) return {};
      const magic = ascii(b, 0, 4);
      if(magic.slice(0, 3) === 'ID3') return id3(b);
      if(magic === 'fLaC') return flac(b);
      if(magic === 'OggS') return ogg(b);
    }catch(_){ }
    return {};
  }

  /* Fetch just the head of the file (ID3 says how big its tag is; covers can be large, so up to 2 MB),
   * once per URL. "Could not read" is {} — the card keeps the name it already shows. */
  const cache = new Map();
  function read(url){
    if(cache.has(url)) return cache.get(url);
    const job = (async () => {
      const get = async (n) => {
        const r = await fetch(url, { headers: { Range: 'bytes=0-' + (n - 1) } });
        if(!r.ok && r.status !== 206) throw new Error('HTTP ' + r.status);
        return new Uint8Array(await r.arrayBuffer());
      };
      let b = await get(262144);
      const want = Math.min(needed(b), 2 * 1024 * 1024);
      if(want > b.length && b.length >= 262144) b = await get(want);
      return parse(b);
    })().catch(() => ({}));
    cache.set(url, job);
    return job;
  }

  root.PCAudioTags = { parse, read, needed };
})(typeof window !== 'undefined' ? window : globalThis);
