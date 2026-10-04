/* Strip camera metadata (EXIF, GPS, XMP, IPTC, comments, appended trailers) from an image's BYTES,
 * losslessly: the pixels are copied through untouched, only the metadata blocks are left out.
 *
 * "make sure we remove exif data on all blossom uploads". A phone photo carries where it was taken
 * (GPS), when, and on what device; uploads only lost that when they happened to be re-encoded (big
 * photos), so every small one went out with its location in it. upload.js runs this on every image
 * before it leaves the device -- including ones that are encrypted for somebody else to open.
 *
 * DOM-free and dependency-free so tests/client/test_exif_stripped_from_uploads.py can run the SHIPPED
 * code under node against real image files.
 *
 *   strip(bytes: Uint8Array) -> { format, bytes, changed, orientation }
 *     format       'jpeg' | 'png' | 'webp' | null (not a format this can parse: the caller decides)
 *     bytes        the cleaned image (same object when nothing needed removing)
 *     orientation  the EXIF Orientation that was there (1 = upright). Removing EXIF removes it, so an
 *                  image with orientation > 1 must be re-drawn upright by the caller, or it turns sideways.
 */
(function(root){
  'use strict';

  function u16(b, o, le){ return le ? (b[o] | (b[o+1] << 8)) : ((b[o] << 8) | b[o+1]); }
  function u32(b, o, le){ return le ? ((b[o] | (b[o+1] << 8) | (b[o+2] << 16)) + b[o+3] * 0x1000000)
                                    : (b[o] * 0x1000000 + ((b[o+1] << 16) | (b[o+2] << 8) | b[o+3])); }
  const ascii = (b, o, n) => { let s = ''; for(let i = 0; i < n && o + i < b.length; i++) s += String.fromCharCode(b[o+i]); return s; };

  /* The Orientation tag (0x0112) in IFD0 of a TIFF block; 1 when absent or unreadable. */
  function tiffOrientation(b, o){
    try{
      if(o + 8 > b.length) return 1;
      const order = ascii(b, o, 2);
      if(order !== 'II' && order !== 'MM') return 1;
      const le = order === 'II';
      if(u16(b, o + 2, le) !== 42) return 1;
      const ifd = o + u32(b, o + 4, le);
      if(ifd + 2 > b.length) return 1;
      const n = u16(b, ifd, le);
      for(let i = 0; i < n; i++){
        const e = ifd + 2 + i * 12;
        if(e + 12 > b.length) break;
        if(u16(b, e, le) === 0x0112){ const v = u16(b, e + 8, le); return v >= 1 && v <= 8 ? v : 1; }
      }
    }catch(_){ }
    return 1;
  }
  function exifOrientation(b, o, end){
    // An EXIF payload is a TIFF block, sometimes after the "Exif\0\0" signature.
    if(ascii(b, o, 6) === 'Exif\0\0') o += 6;
    return o < end ? tiffOrientation(b, o) : 1;
  }

  function concat(parts, total){
    const out = new Uint8Array(total); let at = 0;
    for(const p of parts){ out.set(p, at); at += p.length; }
    return out;
  }

  /* JPEG: keep JFIF (APP0), ICC colour (APP2 "ICC_PROFILE"), Adobe (APP14, needed to decode colour
   * correctly) and every non-APP segment; drop EXIF/XMP (APP1), IPTC (APP13), every other APPn and COM.
   * Stop at the image's real end (EOI): phones append a second preview JPEG (MPF) or vendor trailers
   * after it, and those carry their own EXIF and GPS. */
  function stripJpeg(b){
    const parts = [b.subarray(0, 2)]; let total = 2, changed = false, orientation = 1, i = 2;
    while(i < b.length){
      if(b[i] !== 0xFF) return null;                                // not where a marker must be
      let m = i + 1; while(m < b.length && b[m] === 0xFF) m++;      // fill bytes
      if(m >= b.length) return null;
      const marker = b[m], start = m - 1;
      if(marker === 0xD9){                                          // EOI: the image ends here
        parts.push(b.subarray(start, m + 1)); total += m + 1 - start;
        if(m + 1 < b.length) changed = true;                        // trailing data dropped
        return { bytes: concat(parts, total), changed, orientation };
      }
      if(marker === 0x01 || (marker >= 0xD0 && marker <= 0xD7)){    // no length
        parts.push(b.subarray(start, m + 1)); total += m + 1 - start; i = m + 1; continue;
      }
      if(m + 3 > b.length) return null;
      const len = u16(b, m + 1, false), end = m + 1 + len;
      if(len < 2 || end > b.length) return null;
      let keep = true;
      if(marker === 0xE1){ const o = exifOrientation(b, m + 3, end); if(o !== 1) orientation = o; keep = false; }
      else if(marker === 0xE2) keep = ascii(b, m + 3, 12) === 'ICC_PROFILE\0';
      else if(marker >= 0xE3 && marker <= 0xEF) keep = marker === 0xEE;
      else if(marker === 0xFE) keep = false;
      if(keep){ parts.push(b.subarray(start, end)); total += end - start; }
      else changed = true;
      i = end;
      if(marker === 0xDA){                                          // SOS: entropy-coded data follows
        let j = i;
        while(j < b.length){
          if(b[j] === 0xFF && j + 1 < b.length){
            const n = b[j+1];
            if(n === 0x00 || (n >= 0xD0 && n <= 0xD7) || n === 0xFF){ j += (n === 0xFF ? 1 : 2); continue; }
            break;                                                  // a real marker: back to segments
          }
          j++;
        }
        parts.push(b.subarray(i, j)); total += j - i; i = j;
      }
    }
    return null;                                                    // no EOI: not a whole JPEG
  }

  /* PNG: drop eXIf and the text chunks (tEXt/zTXt/iTXt carry XMP, comments, software, sometimes GPS)
   * and tIME; stop at IEND. Chunks are copied whole, so every kept CRC stays valid. */
  const PNG_DROP = new Set(['eXIf', 'tEXt', 'zTXt', 'iTXt', 'tIME']);
  function stripPng(b){
    const parts = [b.subarray(0, 8)]; let total = 8, changed = false, orientation = 1, i = 8;
    while(i + 12 <= b.length){
      const len = u32(b, i, false), type = ascii(b, i + 4, 4), end = i + 12 + len;
      if(end > b.length) return null;
      if(PNG_DROP.has(type)){
        if(type === 'eXIf') orientation = exifOrientation(b, i + 8, i + 8 + len);
        changed = true;
      }else{ parts.push(b.subarray(i, end)); total += end - i; }
      i = end;
      if(type === 'IEND'){ if(i < b.length) changed = true; return { bytes: concat(parts, total), changed, orientation }; }
    }
    return null;
  }

  /* WebP: drop the EXIF and XMP chunks, clear their flags in VP8X, and rewrite the RIFF size. */
  function stripWebp(b){
    const parts = [], chunks = []; let changed = false, orientation = 1, i = 12;
    while(i + 8 <= b.length){
      const id = ascii(b, i, 4), len = u32(b, i + 4, true), end = i + 8 + len + (len & 1);
      if(i + 8 + len > b.length) return null;
      if(id === 'EXIF'){ orientation = exifOrientation(b, i + 8, i + 8 + len); changed = true; }
      else if(id === 'XMP '){ changed = true; }
      else chunks.push(b.slice(i, Math.min(end, b.length)));
      i = end;
    }
    if(!changed) return { bytes: b, changed: false, orientation: 1 };
    for(const c of chunks){
      if(ascii(c, 0, 4) === 'VP8X' && c.length > 8) c[8] &= ~(0x08 | 0x04);   // EXIF and XMP flags
      parts.push(c);
    }
    const body = parts.reduce((n, c) => n + c.length, 0);
    const head = new Uint8Array(12);
    head.set([0x52, 0x49, 0x46, 0x46]);                             // RIFF
    const size = body + 4;
    head[4] = size & 0xFF; head[5] = (size >> 8) & 0xFF; head[6] = (size >> 16) & 0xFF; head[7] = (size >>> 24) & 0xFF;
    head.set([0x57, 0x45, 0x42, 0x50], 8);                          // WEBP
    return { bytes: concat([head, ...parts], 12 + body), changed: true, orientation };
  }

  function strip(bytes){
    const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    let r = null, format = null;
    if(b.length > 3 && b[0] === 0xFF && b[1] === 0xD8 && b[2] === 0xFF){ format = 'jpeg'; r = stripJpeg(b); }
    else if(b.length > 8 && ascii(b, 1, 3) === 'PNG' && b[0] === 0x89){ format = 'png'; r = stripPng(b); }
    else if(b.length > 12 && ascii(b, 0, 4) === 'RIFF' && ascii(b, 8, 4) === 'WEBP'){ format = 'webp'; r = stripWebp(b); }
    if(!r) return { format, bytes: null, changed: false, orientation: 1 };
    return { format, bytes: r.changed ? r.bytes : b, changed: r.changed, orientation: r.orientation || 1 };
  }

  /* BROWSER HALF: a File in, a File with no metadata out. Lossless when strip() can do it; when the photo
   * is ROTATED (removing EXIF removes the rotation, and it would turn sideways) or is a format strip()
   * cannot read but the browser can decode (AVIF, HEIC where supported), it is re-drawn upright through a
   * canvas, which writes no metadata at all. Not an image, or GIF/SVG (animation, vector): unchanged. */
  async function cleanFile(file){
    const type = String((file && file.type) || '').toLowerCase();
    if(!file || !/^image\//.test(type) || /gif|svg/.test(type)) return file;
    const r = strip(new Uint8Array(await file.arrayBuffer()));
    if(r.bytes && r.orientation === 1) return r.changed ? new File([r.bytes], file.name || 'image', { type: file.type }) : file;
    if(typeof createImageBitmap !== 'function') return file;
    let bmp;
    try{ bmp = await createImageBitmap(file, { imageOrientation: 'from-image' }); }
    catch(_){ return file; }                                       // the browser cannot decode it either
    const w = bmp.width, h = bmp.height;
    const cv = typeof OffscreenCanvas === 'function' ? new OffscreenCanvas(w, h)
             : Object.assign(document.createElement('canvas'), { width: w, height: h });
    cv.getContext('2d').drawImage(bmp, 0, 0); if(bmp.close) bmp.close();
    const keepAlpha = r.format === 'png' || /png|webp|avif/.test(type);
    const outType = keepAlpha ? 'image/png' : 'image/jpeg';
    const blob = cv.convertToBlob ? await cv.convertToBlob({ type: outType, quality: 0.92 })
                                  : await new Promise(res => cv.toBlob(res, outType, 0.92));
    if(!blob) return file;
    const ext = outType === 'image/png' ? 'png' : 'jpg';
    return new File([blob], String(file.name || 'image').replace(/\.\w+$/, '') + '.' + ext, { type: outType });
  }

  const api = { strip, cleanFile };
  root.PCExifStrip = api;
  if(typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
