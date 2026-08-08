/* PCVcard — reading and writing vCards, with no DOM in it.
 *
 * DOM-free so tests/test_vcard.py can run it under node against real exports. The rule this file
 * exists to keep: a contact is stored exactly as its owner's phone wrote it, and editing a phone
 * number must not throw away the parts this app has no UI for.
 *
 * PRESERVATION IS THE WHOLE DESIGN. A real addressbook carries base64 PHOTOs, Apple-style grouped
 * properties (`item1.EMAIL` labelled by `item1.X-ABLABEL`), a PRODID naming the app that wrote it,
 * and X-* fields nobody else understands. Rebuilding a card from a form would silently drop every
 * one of them — the contact would still look right in this app and lose its photo everywhere else.
 * So: managed properties are rewritten, and every other line is carried through untouched, with its
 * group prefix intact so the labels still point at the right property.
 */
(function(root){
  'use strict';

  // Properties this app has fields for. Everything else is preserved verbatim.
  const MANAGED = ['BEGIN', 'END', 'VERSION', 'UID', 'FN', 'N', 'TEL', 'EMAIL', 'ADR', 'ORG',
                   'TITLE', 'NOTE', 'BDAY', 'REV'];

  /* A continuation line begins with a space or tab. Base64 photo data is folded across dozens of
   * lines, so nothing can be read correctly without this. */
  function unfold(text){
    return String(text || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').replace(/\n[ \t]/g, '');
  }

  /* Lines longer than 75 octets are folded (RFC 6350 §3.2). Emitting a 6 KB PHOTO on one line is
   * accepted by most parsers and rejected by enough of them to matter. */
  function fold(line){
    if(line.length <= 75) return line;
    const out = [line.slice(0, 75)];
    for(let i = 75; i < line.length; i += 74) out.push(' ' + line.slice(i, i + 74));
    return out.join('\r\n');
  }

  function splitCards(text){
    const out = [];
    let cur = null;
    for(const line of String(text || '').replace(/\r\n/g, '\n').split('\n')){
      const s = line.trim().toUpperCase();
      if(s.startsWith('BEGIN:VCARD')){ cur = [line]; continue; }
      if(cur){
        cur.push(line);
        if(s.startsWith('END:VCARD')){ out.push(cur.join('\n')); cur = null; }
      }
    }
    return out;
  }

  /* One line → {group, name, params, value, raw}. `item1.EMAIL;TYPE=WORK:a@b` is a grouped property:
   * the group ties it to its `item1.X-ABLABEL`, and dropping the prefix orphans the label. */
  function parseLine(line){
    const i = line.indexOf(':');
    if(i < 0) return null;
    const head = line.slice(0, i), value = line.slice(i + 1);
    const bits = head.split(';');
    let name = bits[0], group = '';
    const dot = name.indexOf('.');
    if(dot > 0){ group = name.slice(0, dot); name = name.slice(dot + 1); }
    const params = {};
    for(const p of bits.slice(1)){
      const j = p.indexOf('=');
      if(j > 0) params[p.slice(0, j).toUpperCase()] = p.slice(j + 1).replace(/^"|"$/g, '');
      else params[p.toUpperCase()] = true;          // vCard 2.1 writes bare "HOME" / "CELL"
    }
    return { group, name: name.toUpperCase(), params, value, raw: line };
  }

  const unesc = s => String(s == null ? '' : s)
      .replace(/\\n/gi, '\n').replace(/\\,/g, ',').replace(/\\;/g, ';').replace(/\\\\/g, '\\');
  const esc = s => String(s == null ? '' : s)
      .replace(/\\/g, '\\\\').replace(/;/g, '\\;').replace(/,/g, '\\,').replace(/\r?\n/g, '\\n');

  /* Split a structured value (N, ADR) on unescaped semicolons. */
  function parts(value){
    const out = []; let cur = '', esc_ = false;
    for(const ch of String(value || '')){
      if(esc_){ cur += '\\' + ch; esc_ = false; continue; }
      if(ch === '\\'){ esc_ = true; continue; }
      if(ch === ';'){ out.push(cur); cur = ''; continue; }
      cur += ch;
    }
    out.push(cur);
    return out.map(unesc);
  }

  /* TYPE can be a parameter list, repeated, or a bare vCard 2.1 flag. Normalised to lower case for
   * display; the original line is what gets preserved, so nothing here changes what is stored. */
  function typeOf(p){
    const t = [];
    for(const k of Object.keys(p.params)){
      if(k === 'TYPE'){
        for(const v of String(p.params[k]).split(',')) if(v) t.push(v.toLowerCase());
      }else if(p.params[k] === true && k !== 'PREF'){
        t.push(k.toLowerCase());
      }
    }
    return t.filter(x => x !== 'internet' && x !== 'voice').join(' ');
  }

  function parse(text){
    const lines = unfold(text).split('\n').map(l => l.trim()).filter(Boolean);
    const card = { uid: '', fn: '', n: null, org: '', title: '', note: '', bday: '',
                   tels: [], emails: [], adrs: [], photo: '', other: [], version: '3.0' };
    for(const line of lines){
      const p = parseLine(line);
      if(!p) continue;
      switch(p.name){
        case 'BEGIN': case 'END': break;
        case 'VERSION': card.version = p.value.trim(); break;
        case 'UID': card.uid = p.value.trim(); break;
        case 'FN': card.fn = unesc(p.value); break;
        case 'N': {
          const [family, given, middle, prefix, suffix] = parts(p.value);
          card.n = { family: family || '', given: given || '', middle: middle || '',
                     prefix: prefix || '', suffix: suffix || '' };
          break;
        }
        case 'TEL':   card.tels.push({ type: typeOf(p), value: unesc(p.value), group: p.group }); break;
        case 'EMAIL': card.emails.push({ type: typeOf(p), value: unesc(p.value), group: p.group }); break;
        case 'ADR': {
          const [po, ext, street, city, region, code, country] = parts(p.value);
          card.adrs.push({ type: typeOf(p), group: p.group, po: po || '', ext: ext || '',
                           street: street || '', city: city || '', region: region || '',
                           code: code || '', country: country || '' });
          break;
        }
        case 'ORG':   card.org = parts(p.value).filter(Boolean).join(', '); break;
        case 'TITLE': card.title = unesc(p.value); break;
        case 'NOTE':  card.note = unesc(p.value); break;
        case 'BDAY':  card.bday = p.value.trim(); break;
        case 'REV':   break;                       // regenerated on write
        default:
          // PHOTO, X-ABLABEL, PRODID, anything: kept exactly as written.
          if(p.name === 'PHOTO') card.photo = photoUrl(p);
          card.other.push(line);
      }
    }
    if(!card.fn) card.fn = displayName(card);
    return card;
  }

  /* A PHOTO as something an <img> can show, or '' — vCard 3.0 writes ENCODING=b with a TYPE, vCard
   * 4.0 writes a data: URI already. Never guess a mime type onto arbitrary bytes. */
  function photoUrl(p){
    const v = String(p.value || '').trim();
    if(/^data:/i.test(v)) return v;
    if(/^https?:/i.test(v)) return v;
    const b64 = (String(p.params.ENCODING || '').toLowerCase() === 'b') || p.params.BASE64 === true;
    if(!b64 || !v) return '';
    const t = String(p.params.TYPE || 'jpeg').toLowerCase().split(',')[0];
    const mime = t.indexOf('/') > 0 ? t : 'image/' + (t === 'jpg' ? 'jpeg' : t);
    return `data:${mime};base64,${v}`;
  }

  function displayName(card){
    if(card.fn) return card.fn;
    const n = card.n;
    if(n){
      const s = [n.prefix, n.given, n.middle, n.family, n.suffix].filter(Boolean).join(' ').trim();
      if(s) return s;
    }
    return (card.emails[0] || {}).value || (card.tels[0] || {}).value || '(no name)';
  }

  /* Sort key: family name first, the way every addressbook orders people. */
  function sortKey(card){
    const n = card.n || {};
    return ((n.family || '') + ' ' + (n.given || '') + ' ' + (card.fn || '')).trim().toLowerCase();
  }

  function matches(card, q){
    if(!q) return true;
    const hay = [card.fn, card.org, card.title, card.note,
                 ...(card.tels || []).map(t => t.value),
                 ...(card.emails || []).map(e => e.value)].join(' ').toLowerCase();
    // Digits-only queries match a phone number however it is punctuated.
    const digits = q.replace(/\D+/g, '');
    if(digits.length >= 3 && (card.tels || []).some(t => t.value.replace(/\D+/g, '').includes(digits)))
      return true;
    return hay.includes(q.toLowerCase());
  }

  const withGroup = (g, name) => (g ? g + '.' : '') + name;

  /* A card back to vCard text. Managed properties are rewritten from `card`; every preserved line
   * (`card.other`) is re-emitted as it came in, so photos, labels and X-* fields survive an edit. */
  function serialize(card, opts){
    const o = opts || {};
    const uid = card.uid || o.uid || '';
    const n = card.n || { family: '', given: '', middle: '', prefix: '', suffix: '' };
    const L = ['BEGIN:VCARD', 'VERSION:' + (card.version || '3.0')];
    if(uid) L.push('UID:' + uid);
    L.push('FN:' + esc(displayName(card)));
    L.push('N:' + [n.family, n.given, n.middle, n.prefix, n.suffix].map(esc).join(';'));
    for(const t of (card.tels || [])){
      if(!t.value) continue;
      L.push(withGroup(t.group, 'TEL') + (t.type ? ';TYPE=' + t.type.split(/\s+/).join(',') : '')
             + ':' + esc(t.value));
    }
    for(const e of (card.emails || [])){
      if(!e.value) continue;
      L.push(withGroup(e.group, 'EMAIL') + (e.type ? ';TYPE=' + e.type.split(/\s+/).join(',') : '')
             + ':' + esc(e.value));
    }
    for(const a of (card.adrs || [])){
      const v = [a.po, a.ext, a.street, a.city, a.region, a.code, a.country].map(esc).join(';');
      if(v.replace(/;/g, '').trim() === '') continue;
      L.push(withGroup(a.group, 'ADR') + (a.type ? ';TYPE=' + a.type.split(/\s+/).join(',') : '') + ':' + v);
    }
    if(card.org)   L.push('ORG:' + esc(card.org));
    if(card.title) L.push('TITLE:' + esc(card.title));
    if(card.bday)  L.push('BDAY:' + card.bday);
    if(card.note)  L.push('NOTE:' + esc(card.note));
    for(const line of (card.other || [])) L.push(line);
    L.push('REV:' + new Date().toISOString().replace(/\.\d+Z$/, 'Z'));
    L.push('END:VCARD');
    return L.map(fold).join('\r\n') + '\r\n';
  }

  /* A brand new card. UID is generated HERE rather than server-side: a re-import has to be able to
   * recognise this person, and the only stable handle is the one written the first time. */
  function blank(){
    const uid = (typeof crypto !== 'undefined' && crypto.randomUUID) ? crypto.randomUUID()
              : 'pc-' + Math.random().toString(16).slice(2) + Date.now().toString(16);
    return { uid, fn: '', n: { family:'', given:'', middle:'', prefix:'', suffix:'' },
             org:'', title:'', note:'', bday:'', tels:[], emails:[], adrs:[], photo:'',
             other:[], version:'3.0' };
  }

  const API = { unfold, fold, splitCards, parseLine, parse, serialize, blank, displayName,
                sortKey, matches, photoUrl, parts, esc, unesc, MANAGED };
  root.PCVcard = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
