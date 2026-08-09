/* QR codes, drawn in the browser.
 *
 * Every QR this app shows used to be rendered BY THE SERVER (POST /client/qr → segno → SVG), which
 * made a picture of a string depend on a working instance. Three screens paid for that, and they are
 * the three where it hurts most:
 *
 *   - the signer login QR. With no instance there is no server to ask, and over Tor a .onion that is
 *     not routing yet fails the same way — so the one screen whose entire instruction is "scan this"
 *     had nothing to scan. That is what this file was written for.
 *   - the two tip QRs (a Bitcoin Cash address URI). A person reading a post offline, or on a
 *     relays-only build, could not tip.
 *
 * It also removes a round-trip and a POST of the connect secret from the common case, and works in the
 * service worker's offline mode, where fetch() to the origin is the thing that does not happen.
 *
 * WHAT IT IMPLEMENTS, deliberately narrowly: byte mode, error correction level M, versions 1-40 —
 * the same choices segno was called with, so nothing that scanned before scans differently now. Byte
 * mode alone is a real decision: alphanumeric mode would pack an all-caps URI tighter, but every
 * payload here (a nostrconnect:// URI, a bech32 address) is mixed case or hex, where byte mode is
 * what an encoder would pick anyway. The other three EC levels are omitted rather than guessed at.
 *
 * Correctness is not asserted from the tables — tests/test_client_qr_encoder.py encodes payloads at
 * every version from 1 to 40 and DECODES each one back with jsQR (the scanner this app already
 * vendors for the camera), which is the only check that means "a phone can read it".
 *
 * Structure follows Nayuki's reference implementation, which is the clearest description of the
 * placement and masking rules there is.
 */
(function(){
  'use strict';

  // Error-correction level M: the number of EC codewords per block, and the number of blocks, per
  // version. These two rows ARE the format — a wrong entry produces a QR that looks perfect and
  // decodes to nothing, which is why the test suite walks all 40.
  const ECC_PER_BLOCK = [0,
    10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
    26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28];
  const NUM_BLOCKS = [0,
    1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
    17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49];
  const ECL_M = 0;                     // the 2-bit level code in the format information (L=1, M=0)
  const PENALTY = { N1: 3, N2: 3, N3: 40, N4: 10 };

  // Data modules a version has before error correction — total area less the function patterns.
  function rawDataModules(ver){
    let r = (16 * ver + 128) * ver + 64;
    if(ver >= 2){
      const na = Math.floor(ver / 7) + 2;
      r -= (25 * na - 10) * na - 55;   // alignment patterns, less their overlap with the timing lines
      if(ver >= 7) r -= 36;            // the two version-information blocks
    }
    return r;
  }
  function rawCodewords(ver){ return Math.floor(rawDataModules(ver) / 8); }
  function dataCodewords(ver){ return rawCodewords(ver) - ECC_PER_BLOCK[ver] * NUM_BLOCKS[ver]; }
  // Bytes that fit, after the 4-bit mode indicator and the character count (8 bits under version 10,
  // 16 from version 10 up — which is why the version has to be chosen before the bits are written).
  function capacity(ver){ return dataCodewords(ver) - (ver < 10 ? 2 : 3); }

  // ---- GF(256), for the Reed-Solomon error correction ---------------------------------------------
  function gfMul(x, y){
    let z = 0;
    for(let i = 7; i >= 0; i--){
      z = (z << 1) ^ ((z >>> 7) * 0x11D);     // reduce by the QR field polynomial as we go
      z ^= ((y >>> i) & 1) * x;
    }
    return z & 0xFF;
  }
  function rsDivisor(degree){
    const result = new Uint8Array(degree);
    result[degree - 1] = 1;
    let root = 1;
    for(let i = 0; i < degree; i++){
      for(let j = 0; j < degree; j++){
        result[j] = gfMul(result[j], root);
        if(j + 1 < degree) result[j] ^= result[j + 1];
      }
      root = gfMul(root, 0x02);
    }
    return result;
  }
  function rsRemainder(data, divisor){
    const result = new Uint8Array(divisor.length);
    for(let k = 0; k < data.length; k++){
      const factor = data[k] ^ result[0];
      result.copyWithin(0, 1);
      result[result.length - 1] = 0;
      for(let i = 0; i < divisor.length; i++) result[i] ^= gfMul(divisor[i], factor);
    }
    return result;
  }

  // ---- data → codewords ---------------------------------------------------------------------------
  function toCodewords(bytes, ver){
    const bits = [];
    const push = (val, len) => { for(let i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
    push(4, 4);                                   // mode: byte
    push(bytes.length, ver < 10 ? 8 : 16);
    for(let i = 0; i < bytes.length; i++) push(bytes[i], 8);
    const cap = dataCodewords(ver) * 8;
    push(0, Math.min(4, cap - bits.length));      // terminator, truncated if it does not fit
    while(bits.length % 8 !== 0) bits.push(0);
    const out = new Uint8Array(cap / 8);
    for(let i = 0; i < bits.length; i++) out[i >>> 3] |= bits[i] << (7 - (i & 7));
    // The two alternating pad codewords the spec names, for the rest.
    for(let i = bits.length / 8, pad = 0xEC; i < out.length; i++, pad ^= 0xEC ^ 0x11) out[i] = pad;
    return out;
  }

  // Split into blocks, compute each block's EC codewords, then INTERLEAVE — a burst of damage across
  // the printed code then lands a few codewords into each block rather than destroying one outright.
  function addEcc(data, ver){
    const blocks = NUM_BLOCKS[ver], ecLen = ECC_PER_BLOCK[ver], raw = rawCodewords(ver);
    const numShort = blocks - raw % blocks;
    const shortLen = Math.floor(raw / blocks) - ecLen;
    const divisor = rsDivisor(ecLen);
    const dat = [], ecc = [];
    for(let i = 0, k = 0; i < blocks; i++){
      const len = shortLen + (i < numShort ? 0 : 1);
      const d = data.slice(k, k + len); k += len;
      dat.push(d); ecc.push(rsRemainder(d, divisor));
    }
    const out = new Uint8Array(raw);
    let p = 0;
    for(let i = 0; i <= shortLen; i++)
      for(let j = 0; j < blocks; j++)
        if(i < dat[j].length) out[p++] = dat[j][i];
    for(let i = 0; i < ecLen; i++)
      for(let j = 0; j < blocks; j++) out[p++] = ecc[j][i];
    return out;
  }

  // ---- the matrix ---------------------------------------------------------------------------------
  function alignmentPositions(ver){
    if(ver === 1) return [];
    const n = Math.floor(ver / 7) + 2;
    const step = (ver === 32) ? 26 : Math.ceil((ver * 4 + 4) / (n * 2 - 2)) * 2;
    const pos = [6];
    for(let p = ver * 4 + 10; pos.length < n; p -= step) pos.splice(1, 0, p);
    return pos;
  }

  function build(bytes, ver){
    const size = ver * 4 + 17;
    const mod = [], fn = [];
    for(let y = 0; y < size; y++){ mod.push(new Array(size).fill(false)); fn.push(new Array(size).fill(false)); }
    // `into` lets the format bits be re-drawn onto a masked COPY of the matrix. They encode the mask
    // number, so each of the eight candidates has to carry its own — writing them to the original
    // instead produces a code whose data says one mask and whose header says another, which is a
    // perfectly well-formed picture that no scanner on earth can read.
    const setFn = (x, y, dark, into) => {
      if(x >= 0 && x < size && y >= 0 && y < size){ (into || mod)[y][x] = dark; fn[y][x] = true; }
    };
    const bit = (v, i) => ((v >>> i) & 1) !== 0;

    // Timing patterns, then the three finders (drawn with their separators — the 9x9 window).
    for(let i = 0; i < size; i++){ setFn(6, i, i % 2 === 0); setFn(i, 6, i % 2 === 0); }
    for(const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]]){
      for(let dy = -4; dy <= 4; dy++)
        for(let dx = -4; dx <= 4; dx++){
          const d = Math.max(Math.abs(dx), Math.abs(dy));
          setFn(cx + dx, cy + dy, d !== 2 && d !== 4);
        }
    }
    const ap = alignmentPositions(ver);
    for(let i = 0; i < ap.length; i++)
      for(let j = 0; j < ap.length; j++){
        // The three corners are where the finders already are.
        if((i === 0 && j === 0) || (i === 0 && j === ap.length - 1) || (i === ap.length - 1 && j === 0)) continue;
        for(let dy = -2; dy <= 2; dy++)
          for(let dx = -2; dx <= 2; dx++)
            setFn(ap[i] + dx, ap[j] + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
      }

    function drawFormat(mask, into){
      const data = (ECL_M << 3) | mask;
      let rem = data;
      for(let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
      const bits = ((data << 10) | rem) ^ 0x5412;      // 15 bits, BCH(15,5) then masked
      for(let i = 0; i <= 5; i++) setFn(8, i, bit(bits, i), into);
      setFn(8, 7, bit(bits, 6), into);
      setFn(8, 8, bit(bits, 7), into);
      setFn(7, 8, bit(bits, 8), into);
      for(let i = 9; i < 15; i++) setFn(14 - i, 8, bit(bits, i), into);
      for(let i = 0; i < 8; i++) setFn(size - 1 - i, 8, bit(bits, i), into);
      for(let i = 8; i < 15; i++) setFn(8, size - 15 + i, bit(bits, i), into);
      setFn(8, size - 8, true, into);                  // the module that is always dark
    }
    drawFormat(0);                                     // placeholder; rewritten once a mask is chosen
    if(ver >= 7){
      let rem = ver;
      for(let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1F25);
      const bits = (ver << 12) | rem;                  // 18 bits, twice, near the other two finders
      for(let i = 0; i < 18; i++){
        const b = bit(bits, i), a = size - 11 + i % 3, c = Math.floor(i / 3);
        setFn(a, c, b); setFn(c, a, b);
      }
    }

    // The codewords, zig-zagging up and down two-module columns from the bottom right.
    const all = addEcc(toCodewords(bytes, ver), ver);
    let i = 0;
    for(let right = size - 1; right >= 1; right -= 2){
      if(right === 6) right = 5;                       // the vertical timing line is not a column
      for(let vert = 0; vert < size; vert++){
        for(let j = 0; j < 2; j++){
          const x = right - j;
          const upward = ((right + 1) & 2) === 0;
          const y = upward ? size - 1 - vert : vert;
          if(!fn[y][x] && i < all.length * 8){
            mod[y][x] = bit(all[i >>> 3], 7 - (i & 7));
            i++;
          }
        }
      }
    }
    return { size, mod, fn, drawFormat };
  }

  const MASKS = [
    (x, y) => (x + y) % 2 === 0,
    (x, y) => y % 2 === 0,
    (x, y) => x % 3 === 0,
    (x, y) => (x + y) % 3 === 0,
    (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0,
    (x, y) => (x * y) % 2 + (x * y) % 3 === 0,
    (x, y) => ((x * y) % 2 + (x * y) % 3) % 2 === 0,
    (x, y) => ((x + y) % 2 + (x * y) % 3) % 2 === 0,
  ];

  /* The four penalty rules, scored so the least patterned of the eight masks wins. This is not
   * cosmetic: a mask that leaves long same-colour runs, or something that looks like a finder in the
   * middle of the data, is a mask a scanner mis-reads. */
  function penalty(mod, size){
    let result = 0;
    const addHistory = (run, hist) => {
      if(hist[0] === 0) run += size;                 // the quiet zone counts as light either side
      hist.pop(); hist.unshift(run);
    };
    const countPatterns = (h) => {
      const n = h[1];
      const core = n > 0 && h[2] === n && h[3] === n * 3 && h[4] === n && h[5] === n;   // the 1:1:3:1:1
      return (core && h[0] >= n * 4 && h[6] >= n ? 1 : 0) + (core && h[6] >= n * 4 && h[0] >= n ? 1 : 0);
    };
    const terminate = (color, run, hist) => {
      if(color){ addHistory(run, hist); run = 0; }
      addHistory(run + size, hist);
      return countPatterns(hist);
    };
    for(let y = 0; y < size; y++){
      let color = false, run = 0, hist = [0, 0, 0, 0, 0, 0, 0];
      for(let x = 0; x < size; x++){
        if(mod[y][x] === color){
          run++;
          if(run === 5) result += PENALTY.N1; else if(run > 5) result++;
        } else {
          addHistory(run, hist);
          if(!color) result += countPatterns(hist) * PENALTY.N3;
          color = mod[y][x]; run = 1;
        }
      }
      result += terminate(color, run, hist) * PENALTY.N3;
    }
    for(let x = 0; x < size; x++){
      let color = false, run = 0, hist = [0, 0, 0, 0, 0, 0, 0];
      for(let y = 0; y < size; y++){
        if(mod[y][x] === color){
          run++;
          if(run === 5) result += PENALTY.N1; else if(run > 5) result++;
        } else {
          addHistory(run, hist);
          if(!color) result += countPatterns(hist) * PENALTY.N3;
          color = mod[y][x]; run = 1;
        }
      }
      result += terminate(color, run, hist) * PENALTY.N3;
    }
    for(let y = 0; y < size - 1; y++)
      for(let x = 0; x < size - 1; x++){
        const c = mod[y][x];
        if(c === mod[y][x + 1] && c === mod[y + 1][x] && c === mod[y + 1][x + 1]) result += PENALTY.N2;
      }
    let dark = 0;
    for(let y = 0; y < size; y++) for(let x = 0; x < size; x++) if(mod[y][x]) dark++;
    const total = size * size;
    result += (Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1) * PENALTY.N4;
    return result;
  }

  /* text → a square of booleans. Throws when the text does not fit any version, which is the only
   * failure this has: callers show their fallback (the link itself) rather than an empty box. */
  function modules(text){
    const bytes = new TextEncoder().encode(String(text));
    let ver = 0;
    for(let v = 1; v <= 40; v++) if(bytes.length <= capacity(v)){ ver = v; break; }
    if(!ver) throw new Error('too long for a QR code (' + bytes.length + ' bytes)');
    const q = build(bytes, ver);
    let best = null, bestScore = Infinity;
    for(let m = 0; m < 8; m++){
      const trial = q.mod.map(row => row.slice());
      for(let y = 0; y < q.size; y++)
        for(let x = 0; x < q.size; x++)
          if(!q.fn[y][x] && MASKS[m](x, y)) trial[y][x] = !trial[y][x];
      // The format bits carry the mask number, so they are rewritten INTO the candidate before it is
      // scored: they are part of the picture the scanner reads and part of what the penalty sees.
      q.drawFormat(m, trial);
      const s = penalty(trial, q.size);
      if(s < bestScore){ bestScore = s; best = trial; }
    }
    return { size: q.size, version: ver, mod: best };
  }

  /* An SVG string. One <path> for every dark module rather than a rect each: a version-40 code is
   * 1400 modules across the diagonal and 31k elements is a page-freezing amount of DOM. */
  function svg(text, opts){
    opts = opts || {};
    /* A 4-module quiet zone, which is what the spec requires and what scanners are tuned for —
       this was 2. Every decoder here is fed a clean bitmap and is unbothered, so no test could
       have caught it; a phone camera pointed at a screen is the case that suffers, on the sign-in
       QR where a failed scan looks like the login being broken. */
    const scale = opts.scale || 6, border = opts.border == null ? 4 : opts.border;
    const dark = opts.dark || '#000', light = opts.light || '#fff';
    const q = modules(text);
    const dim = q.size + border * 2;
    let d = '';
    for(let y = 0; y < q.size; y++)
      for(let x = 0; x < q.size; x++)
        if(q.mod[y][x]) d += (d ? ' ' : '') + 'M' + (x + border) + ',' + (y + border) + 'h1v1h-1z';
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + dim + ' ' + dim + '" '
      + 'width="' + dim * scale + '" height="' + dim * scale + '" shape-rendering="crispEdges">'
      + '<rect width="100%" height="100%" fill="' + light + '"/>'
      + '<path d="' + d + '" fill="' + dark + '"/></svg>';
  }

  // Ready for an <img src>. A data: URL rather than a blob: one — there is no object to revoke, so a
  // QR that is redrawn on every keystroke (the tip amount) cannot leak them.
  function dataUrl(text, opts){
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg(text, opts));
  }

  window.PCQR = { modules, svg, dataUrl };
})();
