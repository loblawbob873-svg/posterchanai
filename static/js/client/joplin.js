/* Joplin import — parse a `.jex` export (or a folder of Markdown files) into plain objects.
 *
 * DELIBERATELY DOM-free and dependency-free, and it does not know what a Nostr event is: it takes
 * bytes and returns {notes, folders, tags, resources, warnings}. That is what makes it testable —
 * tests/test_joplin_import.py builds a real .jex with Python's tarfile and runs THIS file under
 * node. The previous attempt at this feature (scripts/migrate_joplin.py, now dead) read Joplin's
 * live `database.sqlite` from a server-side script, which meant it could only run on the machine
 * Joplin was installed on, broke whenever Joplin migrated its schema, and read nothing at all when
 * the user had E2EE on. A `.jex` is a stable, documented, user-produced artifact — File → Export →
 * JEX — so that is the only input worth supporting.
 *
 * FORMAT. A .jex is a plain (uncompressed) tar of:
 *   <32-hex-id>.md          one per note / folder / tag / resource-record / anything else
 *   resources/<id>.<ext>    the actual attachment bytes
 *
 * Each .md is Joplin's own item serialization:
 *
 *     <title>
 *
 *     <body…>
 *
 *     id: 0123…
 *     parent_id: 89ab…
 *     type_: 1
 *
 * and the ONLY reliable way to split it is Joplin's own: walk from the LAST line backwards taking
 * `key: value` pairs until a blank line, then the remainder is the body, whose FIRST line is the
 * title. Anything that scans forwards for the metadata block gets it wrong the moment a note's body
 * contains a line like "note: call the bank" — which is a normal thing to write in a note. So
 * `parseItem` below is a faithful port of Joplin's BaseItem.unserialize, not an approximation.
 *
 * `type_` is the item kind: 1 note, 2 folder, 4 resource, 5 tag, 6 note↔tag join. Everything else
 * (settings, revisions, master keys) is ignored by design.
 */
(function(root){
  'use strict';

  const TYPE = { NOTE:1, FOLDER:2, SETTING:3, RESOURCE:4, TAG:5, NOTE_TAG:6, REVISION:13 };

  // ---------------------------------------------------------------- tar

  function _str(u8, off, len){
    let end = off;
    const stop = off + len;
    while(end < stop && u8[end] !== 0) end++;
    let s = '';
    for(let i=off; i<end; i++) s += String.fromCharCode(u8[i]);
    return s;
  }

  // Octal header field. Tar pads these with spaces AND NULs, and GNU writes an empty field as all
  // NULs — parseInt('', 8) is NaN, which would advance the reader by NaN and silently truncate the
  // archive at the first such entry, so an unreadable field means 0, never NaN.
  function _oct(u8, off, len){
    const s = _str(u8, off, len).replace(/[^0-7]/g, '');
    if(!s) return 0;
    const n = parseInt(s, 8);
    return isFinite(n) ? n : 0;
  }

  function untar(buf){
    const u8 = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
    const out = [];
    let off = 0, longName = '';
    while(off + 512 <= u8.length){
      // Two consecutive zero blocks end the archive; one lone zero block is padding to skip.
      if(u8[off] === 0){
        const allZero = u8.subarray(off, off+512).every(b => b === 0);
        if(allZero){ off += 512; continue; }
      }
      const name = _str(u8, off, 100);
      const size = _oct(u8, off+124, 12);
      const type = String.fromCharCode(u8[off+156] || 48);
      const prefix = _str(u8, off+345, 155);
      off += 512;
      const data = u8.subarray(off, off + size);
      off += Math.ceil(size / 512) * 512;
      if(type === 'L'){                      // GNU long name — applies to the NEXT header
        longName = _str(data, 0, data.length);
        continue;
      }
      if(type === 'x' || type === 'g'){      // pax extended header — only `path` interests us
        const txt = new TextDecoder().decode(data);
        const m = /\d+ path=([^\n]+)\n/.exec(txt);
        if(m) longName = m[1];
        continue;
      }
      if(type !== '0' && type !== '\0' && type !== '7') continue;   // dirs/links carry no content
      const full = longName || (prefix ? prefix + '/' + name : name);
      longName = '';
      if(full) out.push({ name: full, data });
    }
    return out;
  }

  async function _gunzip(u8){
    if(typeof DecompressionStream === 'undefined') throw new Error('this export is gzipped and this browser cannot unzip it');
    const ds = new DecompressionStream('gzip');
    const ab = await new Response(new Blob([u8]).stream().pipeThrough(ds)).arrayBuffer();
    return new Uint8Array(ab);
  }

  // ---------------------------------------------------------------- items

  /* Faithful port of Joplin's BaseItem.unserialize. Read the comment at the top of this file before
   * "simplifying" it: the backwards walk is the whole point. */
  function parseItem(content){
    const lines = String(content).replace(/\r\n/g, '\n').split('\n');
    // Trailing blank lines must go BEFORE the backwards walk. The walk treats the first blank line
    // it meets as the end of the property block, so a file that merely ends with a newline — which
    // is what most editors, and any export written through a text stream, produce — would flip
    // straight into body mode and yield an item with no `type_` at all, i.e. an import that reads
    // "no Joplin notes found in that file" for a perfectly good archive.
    while(lines.length && lines[lines.length-1].trim() === '') lines.pop();
    const out = {};
    const body = [];
    let state = 'props';
    for(let i = lines.length - 1; i >= 0; i--){
      let line = lines[i];
      if(state === 'props'){
        line = line.trim();
        if(line === ''){ state = 'body'; continue; }
        const p = line.indexOf(':');
        // Joplin THROWS here. We don't: one malformed line in one item must not abort an import of
        // three thousand notes. Treat it as the end of the property block instead — the worst case
        // is that the line stays part of the body, which is recoverable by reading the note.
        if(p < 0){ state = 'body'; body.splice(0, 0, lines[i]); continue; }
        out[line.substr(0, p).trim()] = line.substr(p + 1).trim();
      } else {
        body.splice(0, 0, line);
      }
    }
    if(!out.type_) return null;              // not a Joplin item (README, .DS_Store, …)
    out.type_ = parseInt(out.type_, 10) || 0;
    if(!body.length){ out.title = ''; out.body = ''; }
    else {
      out.title = body.splice(0, 1)[0];
      if(body.length) body.splice(0, 1);     // the blank line between title and body
      out.body = body.join('\n');
    }
    return out;
  }

  /* Joplin writes timestamps as ISO-8601 in a .jex and as epoch-ms in the database; a
   * Markdown+front-matter export writes yet another format. Everything becomes epoch SECONDS,
   * because that is what a Nostr event's created_at is, and 0 means "unknown" (never `now` — a
   * missing date must not make a ten-year-old note look like it was written today, which would
   * scramble every "recently edited" sort in the app). */
  function _ts(v){
    if(v === undefined || v === null || v === '') return 0;
    if(typeof v === 'number') return v > 1e11 ? Math.floor(v/1000) : Math.floor(v);
    const s = String(v).trim();
    if(/^\d+$/.test(s)){ const n = parseInt(s, 10); return n > 1e11 ? Math.floor(n/1000) : n; }
    const t = Date.parse(s);
    return isFinite(t) ? Math.floor(t/1000) : 0;
  }

  const _bool = v => v === 1 || v === '1' || v === true || v === 'true';

  // ---------------------------------------------------------------- links

  // Joplin's internal link syntax, for BOTH resources and note-to-note links:
  //   ![alt](:/0123456789abcdef0123456789abcdef)   [text](:/…)   and bare <:/…> / (:/…" title")
  // The id is always 32 hex. Captured with the delimiter so a `:/` inside ordinary prose can't match.
  const LINK_RE = /(\]\(|<)\:\/([0-9a-fA-F]{32})/g;

  /** Rewrite every `:/id` link. `map(id)` returns the replacement target, or a falsy value to
   *  leave the link alone (an attachment that failed to upload keeps its original text rather
   *  than turning into a dead link to nothing). */
  function rewriteLinks(body, map){
    return String(body || '').replace(LINK_RE, (m, open, id) => {
      const to = map(id.toLowerCase());
      return to ? open + to : m;
    });
  }

  /** Every `:/id` referenced by a body — used to attach only the resources a note actually uses. */
  function linkedIds(body){
    const out = new Set();
    String(body || '').replace(LINK_RE, (m, open, id) => { out.add(id.toLowerCase()); return m; });
    return out;
  }

  // ---------------------------------------------------------------- front matter

  /* Joplin's OTHER export ("Markdown + Front Matter"), which is what the mobile app and a lot of
   * how-tos produce. YAML-ish, but only ever flat scalars plus one list (tags), so a 20-line reader
   * beats pulling in a YAML parser — and an unrecognised key is kept verbatim rather than dropped. */
  function parseFrontMatter(text){
    const s = String(text).replace(/\r\n/g, '\n');
    if(!s.startsWith('---\n')) return { meta:{}, body:s };
    const end = s.indexOf('\n---', 3);
    if(end < 0) return { meta:{}, body:s };
    const head = s.slice(4, end);
    const body = s.slice(end + 4).replace(/^\n/, '');
    const meta = {};
    let listKey = '';
    for(const raw of head.split('\n')){
      const line = raw.replace(/\s+$/, '');
      if(!line.trim()) continue;
      const item = /^\s*-\s+(.*)$/.exec(line);
      if(item && listKey){ (meta[listKey] = meta[listKey] || []).push(_unquote(item[1])); continue; }
      const m = /^([A-Za-z_][\w-]*)\s*:\s*(.*)$/.exec(line);
      if(!m) continue;
      listKey = '';
      if(m[2] === ''){ listKey = m[1]; meta[m[1]] = []; }
      else meta[m[1]] = _unquote(m[2]);
    }
    return { meta, body };
  }
  function _unquote(v){
    const s = String(v).trim();
    if((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) return s.slice(1, -1);
    return s;
  }

  // ---------------------------------------------------------------- import

  function _blank(){
    return { notes:[], folders:[], tags:[], resources:[], noteTags:[], warnings:[], counts:{} };
  }

  /** Build the result set from already-unpacked {name, data} entries. */
  function collect(entries){
    const r = _blank();
    const resourceBytes = new Map();          // id → Uint8Array from resources/
    let encrypted = 0, skipped = 0;

    for(const e of entries){
      const path = e.name.replace(/^\.\//, '');
      const m = /^resources\/([0-9a-fA-F]{32})(?:\.(\w+))?$/.exec(path);
      if(m){ resourceBytes.set(m[1].toLowerCase(), e.data); continue; }
      if(!/\.md$/i.test(path)) continue;
      let item = null;
      try{ item = parseItem(new TextDecoder().decode(e.data)); }
      catch(err){ r.warnings.push(`could not read ${path}: ${err.message}`); continue; }
      if(!item) continue;

      // An export made while E2EE is on carries ciphertext in `encryption_cipher_text` and an EMPTY
      // title/body. Importing those would fill the app with hundreds of blank notes that look like
      // data loss — count them and report, rather than "succeed".
      if(_bool(item.encryption_applied) || item.encryption_cipher_text){ encrypted++; continue; }

      const id = String(item.id || '').toLowerCase();
      if(item.type_ === TYPE.NOTE){
        r.notes.push({
          id, title: item.title || '', body: item.body || '',
          parent_id: String(item.parent_id || '').toLowerCase(),
          created: _ts(item.user_created_time || item.created_time),
          updated: _ts(item.user_updated_time || item.updated_time),
          todo: _bool(item.is_todo), done: _ts(item.todo_completed) > 0,
          conflict: _bool(item.is_conflict),
          source_url: item.source_url || '',
        });
      } else if(item.type_ === TYPE.FOLDER){
        r.folders.push({ id, title: item.title || 'Untitled',
                         parent_id: String(item.parent_id || '').toLowerCase(),
                         created: _ts(item.created_time), updated: _ts(item.updated_time) });
      } else if(item.type_ === TYPE.TAG){
        r.tags.push({ id, title: item.title || '' });
      } else if(item.type_ === TYPE.NOTE_TAG){
        r.noteTags.push({ note_id: String(item.note_id || '').toLowerCase(),
                          tag_id: String(item.tag_id || '').toLowerCase() });
      } else if(item.type_ === TYPE.RESOURCE){
        r.resources.push({ id, title: item.title || '',
                           filename: item.filename || item.title || '',
                           mime: item.mime || 'application/octet-stream',
                           ext: item.file_extension || '', size: _ts(item.size) || 0, data:null });
      } else if(item.type_ !== TYPE.SETTING && item.type_ !== TYPE.REVISION){
        skipped++;
      }
    }

    // Marry the resource RECORDS to their bytes. Both halves are required: a record with no bytes
    // (a .jex exported with "include resources" off, or a resource Joplin never synced down on this
    // device) must not become a broken attachment — it is dropped and reported.
    const orphanRecords = [];
    for(const res of r.resources){
      const bytes = resourceBytes.get(res.id);
      if(bytes && bytes.length){ res.data = bytes; res.size = bytes.length; }
      else orphanRecords.push(res);
    }
    if(orphanRecords.length){
      r.warnings.push(`${orphanRecords.length} attachment(s) had no file in the export and were skipped`);
      const drop = new Set(orphanRecords.map(x => x.id));
      r.resources = r.resources.filter(x => !drop.has(x.id));
    }
    // Bytes with no record: keep them, guessing from the path. Losing a picture because its
    // bookkeeping entry didn't make it into the export is the worse failure.
    for(const [id, bytes] of resourceBytes){
      if(r.resources.some(x => x.id === id)) continue;
      r.resources.push({ id, title:id, filename:id, mime:'application/octet-stream', ext:'',
                         size:bytes.length, data:bytes });
    }

    if(encrypted){
      r.warnings.push(`${encrypted} item(s) in this export are still ENCRYPTED by Joplin. ` +
        'Disable end-to-end encryption in Joplin (or wait for it to finish decrypting) and export again.');
    }
    r.counts = { notes:r.notes.length, folders:r.folders.length, tags:r.tags.length,
                 resources:r.resources.length, encrypted, skipped };
    return r;
  }

  /** Parse a .jex (ArrayBuffer/Uint8Array). Auto-detects a gzipped one. */
  async function parseJex(buf){
    let u8 = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
    if(u8[0] === 0x1f && u8[1] === 0x8b) u8 = await _gunzip(u8);
    const entries = untar(u8);
    if(!entries.length) throw new Error('this file does not look like a Joplin .jex export');
    const r = collect(entries);
    if(!r.notes.length && !r.folders.length){
      throw new Error('no Joplin notes found in that file' +
        (r.counts.encrypted ? ' — every item in it is still encrypted by Joplin' : ''));
    }
    return r;
  }

  /** Parse a set of Markdown files ({name, text}) from a Markdown+Front-Matter export. Folders come
   *  from the directory each file sits in, which is all that export preserves. */
  function parseMarkdownFiles(files){
    const r = _blank();
    const folderByPath = new Map();
    for(const f of files){
      const path = String(f.name || '').replace(/^\.?\//, '');
      if(/^_resources\//.test(path)){
        const m = /([0-9a-fA-F]{32})?[^/]*$/.exec(path);
        r.resources.push({ id:(m && m[1] ? m[1] : path).toLowerCase(), title:path.split('/').pop(),
                           filename:path.split('/').pop(), mime:'application/octet-stream',
                           ext:(path.split('.').pop()||''), size:(f.data?f.data.length:0), data:f.data||null });
        continue;
      }
      if(!/\.md$/i.test(path)) continue;
      const { meta, body } = parseFrontMatter(f.text != null ? f.text : new TextDecoder().decode(f.data));
      const dir = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '';
      let parent = '';
      if(dir){
        if(!folderByPath.has(dir)){
          const id = 'md' + folderByPath.size.toString(16).padStart(30, '0');
          folderByPath.set(dir, id);
          r.folders.push({ id, title: dir.split('/').pop(), parent_id:'', created:0, updated:0 });
        }
        parent = folderByPath.get(dir);
      }
      const tags = Array.isArray(meta.tags) ? meta.tags
                 : (meta.tags ? String(meta.tags).split(',').map(s=>s.trim()).filter(Boolean) : []);
      r.notes.push({
        id: '', title: meta.title || path.split('/').pop().replace(/\.md$/i, ''), body,
        parent_id: parent, created: _ts(meta.created), updated: _ts(meta.updated),
        todo:false, done:false, conflict:false, source_url: meta.source || '',
        tagNames: tags,
      });
    }
    if(!r.notes.length) throw new Error('no Markdown notes found in that folder');
    r.counts = { notes:r.notes.length, folders:r.folders.length, tags:0,
                 resources:r.resources.length, encrypted:0, skipped:0 };
    return r;
  }

  /** Resolve Joplin's folder tree into the display path a flat notes list can show
   *  ("Work/Clients/Acme"). Cycles (which a corrupted export can contain) stop at the first repeat
   *  rather than hanging the tab. */
  function folderPaths(folders){
    const byId = new Map(folders.map(f => [f.id, f]));
    const out = new Map();
    for(const f of folders){
      const parts = [];
      const seen = new Set();
      let cur = f;
      while(cur && !seen.has(cur.id)){
        seen.add(cur.id);
        parts.unshift(cur.title || 'Untitled');
        cur = cur.parent_id ? byId.get(cur.parent_id) : null;
      }
      out.set(f.id, parts.join('/'));
    }
    return out;
  }

  /** Tag names per note id, from the type_-6 join rows. */
  function tagsByNote(tags, noteTags){
    const name = new Map(tags.map(t => [t.id, t.title]));
    const out = new Map();
    for(const nt of noteTags){
      const n = name.get(nt.tag_id);
      if(!n) continue;
      if(!out.has(nt.note_id)) out.set(nt.note_id, []);
      const list = out.get(nt.note_id);
      if(!list.includes(n)) list.push(n);
    }
    return out;
  }

  const API = { TYPE, untar, parseItem, parseJex, parseFrontMatter, parseMarkdownFiles,
                collect, rewriteLinks, linkedIds, folderPaths, tagsByNote, _ts };
  root.PCJoplin = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
