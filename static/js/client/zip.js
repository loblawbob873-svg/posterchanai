/* #zip — a read-only ZIP reader, for the `.xdc` files webxdc apps ship as.
 *
 * NO LIBRARY, and that is the point rather than a flourish: the browser already has the decompressor
 * (`DecompressionStream`, which joplin.js has been using for gzipped exports since the Joplin
 * import), so all that is missing is the ~90 lines of container parsing below. Vendoring fflate or
 * JSZip to read a directory table and call inflate would be a third copy of code every browser here
 * already ships, and one more thing in static/vendor to keep in step.
 *
 * DOM-free on purpose, so tests/test_webxdc.py can run the SHIPPED parser under node against real
 * zips built by Python's zipfile. Everything this gets wrong is silent in the worst way — a mis-read
 * offset yields plausible-looking bytes, and the app it belongs to then fails to start with no
 * indication that the ARCHIVE was misread rather than the app being broken.
 *
 * Reads the CENTRAL DIRECTORY, never the stream of local headers. They are not interchangeable: a
 * local header is allowed to carry zeroed sizes with the real ones in a trailing data descriptor
 * (that is what streaming writers emit), so walking local headers works on the zips you test with
 * and fails on the ones somebody else's tool produced. The central directory is the authority, which
 * is why it exists and why every real unzipper starts at the end of the file.
 */
(function(){
  const U8 = (b) => (b instanceof Uint8Array) ? b : new Uint8Array(b);
  const u16 = (b, o) => b[o] | (b[o + 1] << 8);
  // >>> 0: a 4-byte offset past 2GB is negative as a signed int, and would index nothing.
  const u32 = (b, o) => ((b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)) >>> 0);

  const SIG_EOCD  = 0x06054b50;
  const SIG_CD    = 0x02014b50;
  const SIG_LOCAL = 0x04034b50;

  /* Find the End Of Central Directory record by scanning BACKWARDS from the end.
   *
   * It is the last thing in the file, except that it carries a trailing comment of up to 65535 bytes
   * — so its position is not fixed and it has to be searched for. Bounded to 64KB + its own 22-byte
   * header, which is the whole legal range; scanning the entire file would find the signature inside
   * compressed data sooner or later, which is a corrupt read that looks like a valid one. */
  function findEocd(b){
    const min = Math.max(0, b.length - (0xFFFF + 22));
    for(let i = b.length - 22; i >= min; i--){
      if(u32(b, i) === SIG_EOCD) return i;
    }
    return -1;
  }

  /* The archive's table of contents: [{name, offset, method, size, csize}].
   * Throws with a reason rather than returning an empty list — "this is not a zip" and "this zip
   * holds no files" must not look the same to the caller, which is a UI that has to say something. */
  function entries(bytes){
    const b = U8(bytes);
    if(b.length < 22) throw new Error('not a zip file (too short)');
    const eocd = findEocd(b);
    if(eocd < 0) throw new Error('not a zip file (no end-of-central-directory record)');
    const count = u16(b, eocd + 10);
    let p = u32(b, eocd + 16);
    /* ZIP64 announces itself by parking 0xFFFFFFFF in the 32-bit fields and putting the real value in
     * an extra record. Refused rather than mis-parsed: a webxdc app that needs 4GB or 65535 files is
     * not a thing, and reading the placeholder as an offset would seek to the end of the file and
     * report an unhelpful corruption error from somewhere else entirely. */
    if(p === 0xFFFFFFFF || count === 0xFFFF) throw new Error('zip64 archives are not supported');
    const out = [];
    for(let i = 0; i < count; i++){
      if(p + 46 > b.length || u32(b, p) !== SIG_CD) throw new Error('corrupt zip (bad central directory)');
      const method = u16(b, p + 10);
      const csize  = u32(b, p + 20);
      const size   = u32(b, p + 24);
      const nlen   = u16(b, p + 28);
      const elen   = u16(b, p + 30);
      const clen   = u16(b, p + 32);
      const local  = u32(b, p + 42);
      const raw    = b.subarray(p + 46, p + 46 + nlen);
      /* Names are UTF-8 when bit 11 of the flags is set and CP437 otherwise. Decoded as UTF-8
       * regardless: every writer of this century sets the flag, CP437 is ASCII for the characters a
       * path in a web app actually uses, and a lone mis-decoded byte is a wrong FILENAME rather than
       * a wrong file. */
      let name = '';
      try{ name = new TextDecoder('utf-8').decode(raw); }catch(_){ name = ''; }
      p += 46 + nlen + elen + clen;
      if(!name || name.charAt(name.length - 1) === '/') continue;         // directory entry
      out.push({ name: normalise(name), offset: local, method, size, csize });
    }
    return out;
  }

  /* Path normalisation, and it is a SECURITY step, not tidying.
   *
   * An entry name is attacker-controlled text: `..%2f..%2fetc` and absolute paths are the classic zip
   * traversal, and while nothing here writes to a filesystem, these names become the keys a sandboxed
   * app fetches by — so `/../../` must not be able to reach out of the archive's own namespace or
   * collide with the injected bridge script's path. Backslashes are normalised because Windows
   * writers emit them and a browser would treat `a\b.js` as one segment. */
  function normalise(name){
    const parts = String(name).replace(/\\/g, '/').split('/');
    const out = [];
    for(const seg of parts){
      if(!seg || seg === '.') continue;
      if(seg === '..'){ out.pop(); continue; }
      out.push(seg);
    }
    return out.join('/');
  }

  async function inflateRaw(bytes){
    if(typeof DecompressionStream === 'undefined'){
      throw new Error('this browser cannot decompress zip files');
    }
    const ds = new DecompressionStream('deflate-raw');
    const stream = new Blob([bytes]).stream().pipeThrough(ds);
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }

  /* The bytes of one entry.
   *
   * The data does NOT start at the central directory's `local` offset — that points at the local file
   * header, whose own name and extra fields have to be measured to find where the payload begins. The
   * extra field length in the LOCAL header is frequently different from the one in the central
   * directory (writers pad alignment there), so it must be read from the local header itself. Using
   * the central directory's value is the single most common way to get this wrong, and it yields data
   * that starts a few bytes late: inflate then fails on files that are merely aligned differently. */
  async function read(bytes, entry){
    const b = U8(bytes);
    const p = entry.offset;
    if(p + 30 > b.length || u32(b, p) !== SIG_LOCAL) throw new Error('corrupt zip (bad local header)');
    const nlen = u16(b, p + 26), elen = u16(b, p + 28);
    const start = p + 30 + nlen + elen;
    const end = start + entry.csize;
    if(end > b.length) throw new Error('corrupt zip (entry runs past the end of the file)');
    const raw = b.subarray(start, end);
    if(entry.method === 0) return raw.slice();          // stored
    if(entry.method === 8) return await inflateRaw(raw);
    throw new Error('unsupported zip compression method ' + entry.method);
  }

  /* Read the whole archive into a Map(name → bytes). One pass, and the caller keeps it: a webxdc app
   * is served file-by-file to a sandbox that asks for them one at a time, over and over, so unzipping
   * per request would inflate the same index.html on every navigation. */
  async function readAll(bytes){
    const b = U8(bytes);
    const map = new Map();
    for(const e of entries(b)){
      if(!e.name) continue;
      map.set(e.name, await read(b, e));
    }
    return map;
  }

  const api = { entries, read, readAll, normalise };
  if(typeof window !== 'undefined') window.PCZip = api;
  if(typeof module !== 'undefined' && module.exports) module.exports = api;
})();
