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
    const card = { uid: '', fn: '', n: null, org: '', title: '', note: '', bday: '', rev: '',
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
        // REV is regenerated on every write, so it is NOT preserved as a line — but it is kept as a
        // field, because it is the only timestamp a card carries and the phone-book merge uses it to
        // order two edits that both happened since the last sync.
        case 'REV':   card.rev = p.value.trim(); break;
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

  /* ---- the phone's own Contacts app -------------------------------------------------------------
   *
   * The Android sync is TWO WAY, and everything that can get the ANSWER wrong lives here: DOM-free,
   * device-free and tested under node (tests/test_vcard.py). ContactsContract is a thin adapter over
   * it — the same split folder sync uses, for the same reason.
   *
   * THE RULE THIS FILE ALREADY ENFORCES APPLIES DOUBLY IN THIS DIRECTION. A phone edits about eight
   * properties. A card carries a base64 photo, Apple-style grouped labels, a foreign PRODID and X-*
   * fields nobody models. So a phone-side edit REWRITES the managed properties and carries every
   * other line through untouched — otherwise saving a phone number on the phone strips the photo
   * everywhere else, which is exactly the failure the web editor was built to avoid.
   */

  const P_MAX = 2 * 1024 * 1024;      // base64 chars of PHOTO; past this it is not a thumbnail

  /* FNV-1a. Not a checksum for anybody else — it only has to change when the card does. */
  function phoneHash(s){
    let h = 0x811c9dc5;
    for(let i = 0; i < s.length; i++){ h ^= s.charCodeAt(i); h = (h * 0x01000193) >>> 0; }
    return h.toString(16);
  }

  const _s = v => String(v == null ? '' : v);

  /* A card in the shape the Capacitor plugin reads and writes, plus `h` — the hash that answers
   * "did this card change since we last pushed it" without sending it. The photo is hashed by LENGTH
   * plus its ends rather than its bytes: a book of 500 faces is tens of megabytes of base64, and
   * re-hashing all of it on every repaint costs more than the push it is meant to avoid. */
  function toPhone(card){
    const c = card || {}, n = c.n || {};
    let photo = '';
    const p = _s(c.photo);
    if(p.slice(0, 5) === 'data:'){
      const i = p.indexOf(',');
      if(i > 0 && p.length - i <= P_MAX) photo = p.slice(i + 1);
    }
    const adrs = (c.adrs || []).map(a => ({ street: _s(a.street), city: _s(a.city),
                                            region: _s(a.region), code: _s(a.code),
                                            country: _s(a.country) }))
                               .filter(a => (a.street + a.city + a.region + a.code + a.country).trim());
    const out = {
      uid: c.uid,
      fn: displayName(c),
      given: _s(n.given), family: _s(n.family),
      middle: _s(n.middle), prefix: _s(n.prefix), suffix: _s(n.suffix),
      org: _s(c.org), title: _s(c.title), note: _s(c.note), bday: _s(c.bday),
      tels: (c.tels || []).filter(t => t.value).map(t => ({ type: _s(t.type), value: _s(t.value) })),
      emails: (c.emails || []).filter(e => e.value).map(e => ({ type: _s(e.type), value: _s(e.value) })),
      adrs,
      // An APK older than two-way sync reads `adr` and knows nothing of `adrs`. Both are sent so an
      // update to the JS (which ships instantly) does not blank every address on a phone whose APK
      // has not caught up (which ships through CI).
      adr: adrs[0] || null,
      photo,
    };
    const forHash = Object.assign({}, out, {
      photo: photo ? (photo.length + ':' + photo.slice(0, 32) + photo.slice(-32)) : '',
    });
    out.h = phoneHash(JSON.stringify(forHash));
    return out;
  }

  /* REV as millis. The only timestamp a card carries, and the only thing that can order an edit made
   * here against one made on the phone. Both the RFC's basic form and the extended one appear in the
   * wild; an unparseable or absent REV is 0, which hands the tie to the phone. */
  function revMs(card){
    const v = _s((card || {}).rev).trim();
    if(!v) return 0;
    const m = /^(\d{4})-?(\d{2})-?(\d{2})[T ]?(\d{2}):?(\d{2}):?(\d{2})/.exec(v);
    if(!m) { const t = Date.parse(v); return isNaN(t) ? 0 : t; }
    return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
  }

  /* Keep the stored TYPE when the phone's means the same thing. ContactsContract has one type per
   * row, so a vCard `TYPE=voice,cell` comes back as plain "cell" — taking that verbatim would quietly
   * rewrite everybody's labels the first time one number was edited. */
  function keepType(mine, theirs){
    const a = _s(mine).trim(), b = _s(theirs).trim();
    if(!b) return a;
    if(!a) return b;
    return a.toLowerCase().split(/\s+/).indexOf(b.toLowerCase()) >= 0 ? a : b;
  }

  /* Pair the phone's list against the stored one so each entry keeps its GROUP — an Apple-style
   * `item1.EMAIL` must stay with the `item1.X-ABLABEL` that names it. Matched on the value first
   * (an unchanged number is the same property however the list was reordered), falling back to
   * position for the one that was edited.
   *
   * TWO PASSES, AND THAT IS THE WHOLE POINT. Resolving matches and fallbacks in one pass lets an
   * entry with no match consume the slot an EXACT match still needed: with `item1`=Mom and
   * `item2`=Work stored, adding a number on the phone reaches this as `[new, Mom]` — the new one
   * has no match, takes `item1` positionally, and Mom is left to fall back onto `item2`. The label
   * "Mom" is now on the office, on every device and in every CardDAV client, with nothing said. So
   * every exact match claims its slot BEFORE any fallback is allowed to look. */
  function _assign(list, src, same){
    const at = list.map(() => -1);
    const used = src.map(() => false);
    for(let n = 0; n < list.length; n++){
      for(let i = 0; i < src.length; i++){
        if(!used[i] && same(src[i], list[n])){ at[n] = i; used[i] = true; break; }
      }
    }
    for(let n = 0; n < list.length; n++){
      if(at[n] >= 0) continue;
      for(let i = 0; i < src.length; i++) if(!used[i]){ at[n] = i; used[i] = true; break; }
    }
    return at;
  }

  function _pair(incoming, mine, key){
    const src = mine || [];
    const norm = v => _s(v).replace(/[\s()\-.]/g, '').toLowerCase();
    const list = (incoming || []).filter(x => _s(x && x.value));
    const at = _assign(list, src, (s, x) => norm(s[key]) === norm(x.value));
    return list.map((x, n) => {
      const from = at[n] >= 0 ? src[at[n]] : {};
      return { value: _s(x.value), type: keepType(from.type, x.type), group: _s(from.group) };
    });
  }

  function _pairAdrs(incoming, mine){
    const src = mine || [];
    const key = a => (_s(a.street) + '|' + _s(a.city)).toLowerCase();
    const list = (incoming || []).map(x => x || {}).filter(
      a => !!(_s(a.street) + _s(a.city) + _s(a.region) + _s(a.code) + _s(a.country)).trim());
    const at = _assign(list, src, (s, a) => key(s) === key(a));
    return list.map((a, n) => {
      const from = at[n] >= 0 ? src[at[n]] : {};
      return { type: _s(from.type), group: _s(from.group), po: _s(from.po), ext: _s(from.ext),
               street: _s(a.street), city: _s(a.city), region: _s(a.region),
               code: _s(a.code), country: _s(a.country) };
    });
  }

  /* The phone's version of a card, merged into the stored one. `other` — the photo, the labels, the
   * X-* fields, the other app's PRODID — is never touched. */
  function applyPhone(card, phone){
    const out = JSON.parse(JSON.stringify(card || blank()));
    const p = phone || {};
    out.other = (card && card.other) ? card.other.slice() : (out.other || []);
    const named = _s(p.fn) || _s(p.given) || _s(p.family) || _s(p.middle) || _s(p.prefix) || _s(p.suffix);
    if(named){
      out.n = { family: _s(p.family), given: _s(p.given), middle: _s(p.middle),
                prefix: _s(p.prefix), suffix: _s(p.suffix) };
      out.fn = _s(p.fn) || [out.n.given, out.n.family].filter(Boolean).join(' ');
    }
    out.tels = _pair(p.tels, card && card.tels, 'value');
    out.emails = _pair(p.emails, card && card.emails, 'value');
    out.adrs = _pairAdrs(p.adrs || (p.adr ? [p.adr] : []), card && card.adrs);
    out.org = _s(p.org); out.title = _s(p.title);
    out.note = _s(p.note); out.bday = _s(p.bday);
    return out;
  }

  /* The losing side of a conflict, kept rather than destroyed — folder sync's rule, which renames the
   * local copy before writing the incoming one. A second card is ugly; an edit that vanishes without
   * a word is worse, and the two clocks involved (a vCard REV written by any app, and the phone's
   * aggregate timestamp) are not exact enough to be trusted with somebody's only copy. */
  function conflictCopy(card, uid){
    const c = JSON.parse(JSON.stringify(card || blank()));
    c.uid = uid || blank().uid;
    c.rev = '';
    const name = displayName(c);
    c.fn = (name === '(no name)' ? '' : name) + ' (conflict copy)';
    c.fn = c.fn.trim();
    return c;
  }

  /**
   * What a sweep of the phone MEANS. Pure, so the decision that can lose somebody's contact is
   * testable without a device.
   *
   *   rows — from the plugin: {uid, rawId, version, deleted, updated, pushed, card}
   *   mine — {uid: {card, book}} for every card the app holds
   *
   * Returns one action per row:
   *   delete  the phone deleted it and we have it → delete the card
   *   drop    the phone deleted it and we do not have it → nothing to do, just acknowledge
   *   create  a contact made ON the phone → store it under the uid the plugin stamped on the row
   *   update  the phone's edit wins → store the merged card
   *   keep    OUR copy wins a conflict → store nothing under this uid; the push puts it back
   *   clean   the row is dirty but says the same thing → acknowledge only
   *
   * `copy` on update/keep is the LOSING version, to be stored beside it as a new card.
   * `ack.h` is set only when the app's card and the phone's row now agree; the plugin records it so
   * the push that follows does not rewrite rows that already match.
   */
  function phonePlan(rows, mine){
    const held = mine || {};
    const out = [];
    for(const r of (rows || [])){
      const uid = _s(r && r.uid);
      if(!uid) continue;
      const have = held[uid];
      if(r.deleted){
        out.push({ uid, row: r, action: have ? 'delete' : 'drop', book: have && have.book });
        continue;
      }
      const p = r.card || {};
      if(!have){
        const made = applyPhone(blank(), p);
        made.uid = uid;                       // the plugin already wrote this onto the row
        out.push({ uid, row: r, action: 'create', card: made, ack: { h: toPhone(made).h } });
        continue;
      }
      const merged = applyPhone(have.card, p);
      merged.uid = uid;
      const mineH = toPhone(have.card).h, mergedH = toPhone(merged).h;
      if(mergedH === mineH){
        out.push({ uid, row: r, action: 'clean', book: have.book, ack: { h: mineH } });
        continue;
      }
      // Both sides changed only if the app's card is no longer what we last pushed. When we have no
      // record of a push at all, assume it did — the safe assumption keeps a copy.
      const both = !_s(r.pushed) || _s(r.pushed) !== mineH;
      const phoneWins = !both || Number(r.updated || 0) > revMs(have.card);
      if(phoneWins){
        out.push({ uid, row: r, action: 'update', book: have.book, card: merged,
                   ack: { h: mergedH }, copy: both ? conflictCopy(have.card) : null });
      }else{
        // Ours wins: nothing is stored under this uid and NO hash is acknowledged, so the push that
        // follows rewrites the phone's rows with our version.
        out.push({ uid, row: r, action: 'keep', book: have.book, copy: conflictCopy(merged) });
      }
    }
    return out;
  }

  const API = { unfold, fold, splitCards, parseLine, parse, serialize, blank, displayName,
                sortKey, matches, photoUrl, parts, esc, unesc, MANAGED,
                toPhone, applyPhone, phonePlan, conflictCopy, revMs, keepType, phoneHash };
  root.PCVcard = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
