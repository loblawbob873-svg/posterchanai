/* Vault core — the parts of the password manager that are pure functions.
 *
 * THIS FILE IS SHARED, VERBATIM, BY THREE CONSUMERS: the web client (vault.js), the Firefox
 * extension (extension/), and — through the web layer — the snapshot the Android autofill service
 * reads. That is the whole reason it exists. A password generator, a TOTP implementation and a
 * "does this credential belong to this site" rule that differ between the app and the extension are
 * three ways to hand someone the wrong answer at the exact moment they cannot check it: a code that
 * doesn't work, a password that silently omits the character class the site required, a login
 * offered on a lookalike domain. One implementation, one set of tests.
 *
 * DOM-FREE ON PURPOSE, like joplin.js: no document, no window, no browser APIs beyond WebCrypto and
 * crypto.getRandomValues (both of which exist in a service worker, an extension background script
 * and node). tests/test_vault_core.py runs THIS file under node against fixtures built in Python —
 * including the RFC 6238 test vectors, which is the only way to know the TOTP codes are right
 * rather than merely self-consistent.
 *
 * CRYPTO. Items are sealed with AES-256-GCM under a random 32-byte VAULT KEY, with a random 12-byte
 * IV prepended to the ciphertext — the same shape as the encrypted drive's _masterEncrypt, and
 * deliberately NOT NIP-44-to-self like Notes. NIP-44 to yourself can only be opened by your secret
 * key, which would mean the Firefox extension had to hold your nsec to read a password. A separate
 * symmetric key can be handed to a paired device on its own, so a stolen browser profile costs the
 * vault and not the identity. The vault key itself is NIP-44-wrapped to the user's own pubkey (see
 * vault.js), so the key that unlocks everything is still only ever readable by them.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;   // node (tests)
  else root.PCVaultCore = api;                                             // browser / extension
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const _crypto = (typeof crypto !== 'undefined' && crypto) ||
                  (typeof require === 'function' ? require('crypto').webcrypto : null);
  const subtle = _crypto && _crypto.subtle;

  // ---------------------------------------------------------------- bytes

  const enc8 = (s) => new TextEncoder().encode(s);
  const dec8 = (b) => new TextDecoder().decode(b);

  function toB64(bytes) {
    let s = '';
    for (const b of bytes) s += String.fromCharCode(b);
    return btoa(s);
  }
  function fromB64(b64) {
    const s = atob(b64);
    const out = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out;
  }
  function toHex(bytes) {
    let s = '';
    for (const b of bytes) s += b.toString(16).padStart(2, '0');
    return s;
  }
  function fromHex(hex) {
    const h = String(hex || '').trim();
    const out = new Uint8Array(h.length >> 1);
    for (let i = 0; i < out.length; i++) out[i] = parseInt(h.substr(i * 2, 2), 16);
    return out;
  }

  // ---------------------------------------------------------------- sealing

  function newVaultKey() { return _crypto.getRandomValues(new Uint8Array(32)); }

  /* Seal a JS value under the vault key. Random IV per write — NOT content-derived like the drive's
   * blobs, which do that so identical files dedup on Blossom. Here a deterministic IV would leak
   * that a password had been re-saved unchanged, and there is nothing to dedup. */
  async function seal(key, obj) {
    const iv = _crypto.getRandomValues(new Uint8Array(12));
    const ck = await subtle.importKey('raw', key, 'AES-GCM', false, ['encrypt']);
    const ct = new Uint8Array(await subtle.encrypt({ name: 'AES-GCM', iv }, ck, enc8(JSON.stringify(obj))));
    const out = new Uint8Array(12 + ct.length);
    out.set(iv, 0); out.set(ct, 12);
    return toB64(out);
  }
  async function open(key, b64) {
    const blob = fromB64(b64);
    const iv = blob.slice(0, 12), ct = blob.slice(12);
    const ck = await subtle.importKey('raw', key, 'AES-GCM', false, ['decrypt']);
    return JSON.parse(dec8(new Uint8Array(await subtle.decrypt({ name: 'AES-GCM', iv }, ck, ct))));
  }

  // ---------------------------------------------------------------- TOTP (RFC 6238)

  const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  /* Base32, RFC 4648, tolerant of what people actually paste: lowercase, the spaces every site puts
   * in the "can't scan the QR?" string, and missing '=' padding (which most authenticator exports
   * omit). A '1'/'0' is NOT silently mapped to I/O — that is a different alphabet (Crockford), and
   * guessing would produce a plausible key that generates wrong codes forever. */
  function b32decode(s) {
    const clean = String(s || '').toUpperCase().replace(/[\s-]/g, '').replace(/=+$/, '');
    if (!clean) return new Uint8Array(0);
    let bits = 0, value = 0;
    const out = [];
    for (const c of clean) {
      const idx = B32.indexOf(c);
      if (idx < 0) throw new Error('not valid base32: ' + c);
      value = (value << 5) | idx;
      bits += 5;
      if (bits >= 8) { out.push((value >>> (bits - 8)) & 0xff); bits -= 8; }
    }
    return new Uint8Array(out);
  }

  const HASHES = { SHA1: 'SHA-1', SHA256: 'SHA-256', SHA512: 'SHA-512' };

  /* One code. `at` is unix SECONDS, injected rather than read from the clock so the RFC's test
   * vectors can be checked — a TOTP implementation that is only ever tested against itself is
   * exactly the one that ships an off-by-one counter and works nowhere. */
  async function totp(secret, opts) {
    const o = opts || {};
    const digits = o.digits || 6;
    const period = o.period || 30;
    const algo = HASHES[String(o.algorithm || 'SHA1').toUpperCase().replace('-', '')] || 'SHA-1';
    const at = (o.at == null ? Math.floor(Date.now() / 1000) : o.at);
    const key = (secret instanceof Uint8Array) ? secret : b32decode(secret);
    if (!key.length) throw new Error('empty TOTP secret');
    const counter = Math.floor(at / period);
    // 8-byte big-endian counter. JS bitwise ops are 32-bit, so the high word is written by division
    // rather than by shifting — a >>> 32 is a no-op and would pin the high half to the low one.
    const buf = new Uint8Array(8);
    let hi = Math.floor(counter / 0x100000000), lo = counter >>> 0;
    for (let i = 3; i >= 0; i--) { buf[i] = hi & 0xff; hi = hi >>> 8; }
    for (let i = 7; i >= 4; i--) { buf[i] = lo & 0xff; lo = lo >>> 8; }
    const ck = await subtle.importKey('raw', key, { name: 'HMAC', hash: algo }, false, ['sign']);
    const mac = new Uint8Array(await subtle.sign('HMAC', ck, buf));
    const off = mac[mac.length - 1] & 0x0f;
    const bin = ((mac[off] & 0x7f) << 24) | (mac[off + 1] << 16) | (mac[off + 2] << 8) | mac[off + 3];
    return String(bin % Math.pow(10, digits)).padStart(digits, '0');
  }

  function totpRemaining(period, at) {
    const p = period || 30;
    const t = (at == null ? Math.floor(Date.now() / 1000) : at);
    return p - (t % p);
  }

  /* Accept whatever the user pastes into the TOTP field: a bare base32 secret, or the whole
   * `otpauth://totp/Issuer:you@example.com?secret=...&digits=8&period=60&algorithm=SHA256` URI that
   * every site's QR encodes. Returns null when it isn't a URI, so the caller can treat the input as
   * a raw secret. otpauth-migration:// (Google Authenticator's export) is NOT handled — it is a
   * protobuf payload, and half-parsing it would produce silently wrong secrets. */
  function parseOtpAuth(str) {
    const s = String(str || '').trim();
    if (!/^otpauth:\/\//i.test(s)) return null;
    let u;
    try { u = new URL(s); } catch (_) { return null; }
    const type = (u.host || '').toLowerCase();
    const q = u.searchParams;
    const label = decodeURIComponent((u.pathname || '').replace(/^\//, ''));
    const parts = label.split(':');
    return {
      type: type || 'totp',
      secret: (q.get('secret') || '').replace(/\s/g, ''),
      issuer: q.get('issuer') || (parts.length > 1 ? parts[0] : '') || '',
      account: (parts.length > 1 ? parts.slice(1).join(':') : label) || '',
      digits: parseInt(q.get('digits') || '6', 10) || 6,
      period: parseInt(q.get('period') || '30', 10) || 30,
      algorithm: (q.get('algorithm') || 'SHA1').toUpperCase(),
      counter: q.get('counter') ? parseInt(q.get('counter'), 10) : null,   // HOTP; stored, not used
    };
  }

  /* Normalise a TOTP field to {secret, digits, period, algorithm} or null. One place, because the
   * app, the extension and the importer all take the same input from three directions. */
  function totpConfig(raw) {
    const s = String(raw || '').trim();
    if (!s) return null;
    const uri = parseOtpAuth(s);
    const cfg = uri ? { secret: uri.secret, digits: uri.digits, period: uri.period, algorithm: uri.algorithm }
                    : { secret: s.replace(/\s/g, ''), digits: 6, period: 30, algorithm: 'SHA1' };
    if (!cfg.secret) return null;
    try { b32decode(cfg.secret); } catch (_) { return null; }   // unparseable → say so, don't half-work
    return cfg;
  }

  // ---------------------------------------------------------------- password generator

  const SETS = {
    lower: 'abcdefghijklmnopqrstuvwxyz',
    upper: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
    digits: '0123456789',
    symbols: '!@#$%^&*()-_=+[]{};:,.?',
  };
  // Characters that are one glyph in the wrong font: 1/l/I, 0/O. Excluded on request, because the
  // password you cannot read aloud or retype from a screen is the one people write down.
  const AMBIGUOUS = /[1lI0O]/g;

  /* Uniform over the alphabet — rejection sampling, not `% len`. A modulo of a random byte is biased
   * toward the first (256 % len) characters, which is a real, measurable weakening of every password
   * this ever produces, for no gain over looping. */
  function randInt(n) {
    if (n <= 0) throw new Error('empty alphabet');
    const limit = Math.floor(256 / n) * n;
    const b = new Uint8Array(1);
    for (;;) {
      _crypto.getRandomValues(b);
      if (b[0] < limit) return b[0] % n;
    }
  }
  function shuffle(arr) {
    for (let i = arr.length - 1; i > 0; i--) {
      const j = randInt(i + 1);
      const t = arr[i]; arr[i] = arr[j]; arr[j] = t;
    }
    return arr;
  }

  /* Generate a password. Every ENABLED class is guaranteed to appear at least once (sites reject a
   * password for missing one, and a user who has to regenerate five times to get a digit stops
   * using the generator), then the rest is filled uniformly and the whole thing shuffled so the
   * guaranteed characters aren't always in front. */
  function generate(opts) {
    const o = Object.assign({ length: 20, lower: true, upper: true, digits: true, symbols: true,
                              avoidAmbiguous: false }, opts || {});
    const pools = [];
    for (const name of ['lower', 'upper', 'digits', 'symbols']) {
      if (!o[name]) continue;
      const set = o.avoidAmbiguous ? SETS[name].replace(AMBIGUOUS, '') : SETS[name];
      if (set) pools.push(set);
    }
    if (!pools.length) throw new Error('no character sets enabled');
    const len = Math.max(pools.length, Math.min(128, o.length | 0 || 20));
    const out = [];
    for (const set of pools) out.push(set[randInt(set.length)]);          // one from each class
    const all = pools.join('');
    while (out.length < len) out.push(all[randInt(all.length)]);
    return shuffle(out).join('');
  }

  /* Bits of entropy for what generate() would produce with these options — the honest figure,
   * log2(alphabet) * length, shown next to the generator so "20 characters" means something. It
   * slightly UNDERSTATES the guaranteed-class version (which is a subset), and understating is the
   * right direction to be wrong in. */
  function entropyBits(opts) {
    const o = Object.assign({ length: 20, lower: true, upper: true, digits: true, symbols: true,
                              avoidAmbiguous: false }, opts || {});
    let n = 0;
    for (const name of ['lower', 'upper', 'digits', 'symbols']) {
      if (!o[name]) continue;
      n += (o.avoidAmbiguous ? SETS[name].replace(AMBIGUOUS, '') : SETS[name]).length;
    }
    if (!n) return 0;
    return Math.round(Math.log2(n) * Math.max(1, Math.min(128, o.length | 0 || 20)));
  }

  // ---------------------------------------------------------------- URL matching

  /* Multi-label public suffixes common enough that getting them wrong matters. This is NOT the
   * Public Suffix List — that is 10k lines and a background update problem — and the difference is
   * deliberate and bounded: an unknown multi-label suffix makes eTLD+1 come out as `co.uk`-shaped,
   * i.e. two sites under the same odd TLD could be treated as one. So the matcher NEVER fills on
   * base-domain alone; it fills on exact host, and offers base-domain matches for the user to pick.
   * Suggesting is safe, filling silently is not. */
  const MULTI_SUFFIX = new Set([
    'co.uk', 'org.uk', 'me.uk', 'ac.uk', 'gov.uk', 'net.uk', 'sch.uk',
    'com.au', 'net.au', 'org.au', 'edu.au', 'gov.au', 'id.au',
    'co.nz', 'net.nz', 'org.nz', 'govt.nz', 'ac.nz',
    'co.za', 'org.za', 'net.za', 'web.za',
    'com.br', 'net.br', 'org.br', 'gov.br',
    'co.jp', 'ne.jp', 'or.jp', 'ac.jp', 'go.jp',
    'com.cn', 'net.cn', 'org.cn', 'gov.cn', 'edu.cn',
    'co.in', 'net.in', 'org.in', 'gen.in', 'firm.in',
    'com.mx', 'com.ar', 'com.tr', 'com.sg', 'com.hk', 'com.tw', 'com.pl', 'com.ua',
    'co.kr', 'or.kr', 'ne.kr', 'go.kr',
  ]);

  function hostOf(url) {
    const s = String(url || '').trim();
    if (!s) return '';
    try {
      const u = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(s) ? s : 'https://' + s);
      return (u.hostname || '').toLowerCase().replace(/^www\./, '');
    } catch (_) { return ''; }
  }

  /* eTLD+1, best effort. An IP address is returned whole — it has no registrable domain, and
   * chopping it to "0.1" would make every host on a LAN look like the same site. */
  function baseDomain(host) {
    const h = String(host || '').toLowerCase().replace(/^www\./, '');
    if (!h || /^\d{1,3}(\.\d{1,3}){3}$/.test(h) || h.indexOf(':') >= 0) return h;
    const parts = h.split('.');
    if (parts.length <= 2) return h;
    const last2 = parts.slice(-2).join('.');
    if (MULTI_SUFFIX.has(last2)) return parts.slice(-3).join('.');
    return last2;
  }

  /* How well does `item` match `pageUrl`? 'exact' = same host, 'domain' = same registrable domain,
   * '' = no. Every URI on the item is considered: sites move their login to another host
   * (accounts.example.com) and a manager that only remembers the one you first saved is a manager
   * you stop trusting. */
  /* Is this pattern safe to run on untrusted input?
   *
   * Catastrophic backtracking needs a quantifier applied to something that is itself quantified —
   * `(a+)+`, `(a*)*`, `(a|aa)+`. That family is what this refuses, by finding every quantified
   * group and checking whether its body contains a quantifier or an alternation. It is deliberately
   * CONSERVATIVE: some harmless patterns are refused, and refusing costs a match that was never
   * offered, while accepting costs a hung browser.
   *
   * Not a general safety proof — that is undecidable in the interesting cases — but it covers the
   * shapes a person actually writes by accident, and the length cap bounds the rest. */
  const _reCache = new Map();
  function _safeRegex(pattern) {
    const p = String(pattern || '');
    if (!p || p.length > 200) return false;
    if (_reCache.has(p)) return _reCache.get(p);
    let ok = true;
    const stack = [];
    for (let i = 0; i < p.length && ok; i++) {
      const c = p[i];
      if (c === '\\') { i++; continue; }                 // escaped: never structural
      if (c === '[') { while (i < p.length && p[i] !== ']') { if (p[i] === '\\') i++; i++; } continue; }
      if (c === '(') { stack.push({ start: i, inner: false }); continue; }
      if (c === ')') {
        const g = stack.pop();
        const next = p[i + 1];
        if (g && g.inner && (next === '+' || next === '*' || next === '{')) ok = false;
        // A quantified group nested in another group makes THAT one quantified-inside too.
        if (stack.length && (next === '+' || next === '*' || next === '{')) stack[stack.length - 1].inner = true;
        continue;
      }
      if (c === '+' || c === '*' || c === '{' || c === '|') {
        if (stack.length) stack[stack.length - 1].inner = true;
      }
    }
    _reCache.set(p, ok);
    return ok;
  }

  function matchLevel(item, pageUrl) {
    const host = hostOf(pageUrl);
    if (!host) return '';
    const page = String(pageUrl || '');
    let best = '';
    for (const r of itemUriRules(item)) {
      const u = r.uri;
      switch (r.match) {
        case 'never':
          continue;
        case 'regex':
          /* A user-supplied pattern, run against a page-supplied string, for every stored item on
           * every page. try/catch covers a syntax error but NOT catastrophic backtracking:
           * `^https://example\.com/(([a-z]+)+)+x$` against a sixty-character path took 111 SECONDS
           * here, on a URL well under any length cap — bounding the input does not help, because
           * the blow-up is in the pattern. In the extension's background worker that is a freeze of
           * everything, triggered by whatever page you happen to open.
           *
           * So the PATTERN is vetted (see _safeRegex) and a rejected one simply does not match.
           * Failing closed is right: the cost is a credential not offered, which is visible and
           * fixable, against a browser that stops responding, which is neither. */
          if (page.length > 2048 || !_safeRegex(u)) continue;
          try { if (new RegExp(u).test(page)) return 'exact'; } catch (_) {}
          continue;
        case 'exact':
          if (page === u || page.replace(/\/+$/, '') === u.replace(/\/+$/, '')) return 'exact';
          continue;
        case 'startsWith': {
          /* A raw string prefix is NOT a URL match. `https://good.com` is a prefix of
           * `https://good.com.evil.com/login`, of `https://good.commerce.net`, and of
           * `https://good.com@evil.com` — and matchLevel is the gate the extension uses to decide
           * whether to hand a plaintext password to a frame, so a prefix test alone releases the
           * credential to whoever registers the lookalike. The host has to agree first; the prefix
           * then narrows within that host, which is what the rule is actually for. */
          if (!u) continue;
          const ph = hostOf(u);
          if (ph && ph === host && page.startsWith(u)) return 'exact';
          continue;
        }
        case 'host': {
          // Bitwarden's "host" includes the port; hostOf drops it, so this is host-without-port.
          const hh = hostOf(u);
          if (hh && hh === host) return 'exact';
          continue;
        }
        default: {
          const h = hostOf(u);
          if (!h) continue;
          if (h === host) return 'exact';
          if (baseDomain(h) && baseDomain(h) === baseDomain(host)) best = 'domain';
        }
      }
    }
    return best;
  }

  function itemUris(item) { return itemUriRules(item).map(r => r.uri); }

  /* Every site on an entry, with how it wants to be matched. An entry routinely has SEVERAL — the
   * one this was reported over lists a public site, two other Nostr clients, a LAN address and an
   * onion — and all of them must work, not just the first.
   *
   * `match` comes from Bitwarden and is honoured rather than flattened: 'domain' (the default),
   * 'host', 'startsWith', 'exact', 'regex', and 'never'. It is the one place a user has already
   * said how a site should be recognised. */
  function itemUriRules(item) {
    const out = [];
    if (!item) return out;
    if (Array.isArray(item.uris)) {
      for (const u of item.uris) {
        if (!u) continue;
        if (typeof u === 'string') out.push({ uri: u, match: '' });
        else if (u.uri) out.push({ uri: u.uri, match: u.match || '' });
      }
    }
    if (item.url) out.push({ uri: item.url, match: '' });
    return out.filter(r => r.uri);
  }

  /* Credentials for a page, best match first. Ranking, not filtering: an exact-host match beats a
   * same-domain one, and within a tier the most recently used comes first, because the account you
   * used last on a site is overwhelmingly the one you want next. */
  function matchesFor(items, pageUrl) {
    const rank = { exact: 0, domain: 1 };
    return (items || [])
      .map(it => ({ it, lvl: matchLevel(it, pageUrl) }))
      .filter(x => x.lvl)
      .sort((a, b) => (rank[a.lvl] - rank[b.lvl]) || ((b.it.used || b.it.updated || 0) - (a.it.used || a.it.updated || 0)))
      .map(x => Object.assign({ _match: x.lvl }, x.it));
  }

  // ---------------------------------------------------------------- Bitwarden import

  /* Bitwarden's UNENCRYPTED .json export, and its .csv. Not the encrypted one: that is a
   * PBKDF2/AES envelope around the same data, and asking for the export password here would mean
   * this code handling a second secret to no benefit — the user can export unencrypted, import, and
   * delete the file. An encrypted export is REFUSED LOUDLY rather than imported as a wall of blank
   * entries, which is the same call joplin.js makes about an E2EE Joplin export and for the same
   * reason: silence looks like success.
   *
   * Types: 1 login, 2 secure note, 3 card, 4 identity. Logins are the point; notes come across as
   * notes; cards and identities are carried as a note with their fields rendered, because dropping
   * a user's card entries without saying so is data loss and inventing UI for them is not this
   * change. Everything keeps its Bitwarden id in `src`, so a re-import updates in place instead of
   * duplicating — the same rule the Joplin importer follows. */
  function parseBitwarden(text) {
    const raw = String(text || '');
    let data = null;
    try { data = JSON.parse(raw); } catch (_) { data = null; }
    if (data && typeof data === 'object') return _bwJson(data);
    if (/^[^\n]*\bname\b[^\n]*,/i.test(raw) && /login_password|login_username/i.test(raw)) return _bwCsv(raw);
    throw new Error('that is not a Bitwarden export — expected the .json or .csv file');
  }

  function _bwJson(data) {
    if (data.encrypted === true || data.encKeyValidation_DO_NOT_EDIT) {
      throw new Error('that export is ENCRYPTED. Export again with "Export as: .json" and the ' +
                      'password protection turned off, import it, then delete the file.');
    }
    const folders = new Map();
    for (const f of (data.folders || [])) if (f && f.id) folders.set(f.id, String(f.name || '').trim());
    for (const c of (data.collections || [])) if (c && c.id) folders.set(c.id, String(c.name || '').trim());
    const items = [];
    for (const it of (data.items || [])) {
      const rec = _bwItem(it, folders);
      if (rec) items.push(rec);
    }
    return { items, folders: Array.from(new Set(Array.from(folders.values()).filter(Boolean))) };
  }

  // Bitwarden's URI match types, by their numeric value in the export.
  const BW_MATCH = { 0: 'domain', 1: 'host', 2: 'startsWith', 3: 'exact', 4: 'regex', 5: 'never' };

  function _bwItem(it, folders) {
    if (!it || typeof it !== 'object') return null;
    const type = it.type || 1;
    const folder = folders.get(it.folderId) || (Array.isArray(it.collectionIds) && it.collectionIds.length
      ? folders.get(it.collectionIds[0]) : '') || '';
    const base = {
      src: { app: 'bitwarden', id: it.id || '' },
      title: String(it.name || '').trim() || 'Untitled',
      folder,
      notes: String(it.notes || ''),
      favorite: !!it.favorite,
      fields: (it.fields || []).filter(f => f && f.name)
        .map(f => ({ name: String(f.name), value: String(f.value == null ? '' : f.value),
                     hidden: f.type === 1 })),
    };
    if (type === 1) {
      const lg = it.login || {};
      return Object.assign(base, {
        kind: 'login',
        username: String(lg.username || ''),
        password: String(lg.password || ''),
        totp: String(lg.totp || ''),
        // Keep Bitwarden's per-URI match RULE, not just the string. 0 base domain, 1 host,
        // 2 starts-with, 3 exact, 4 regular expression, 5 never. Dropping it (which this did) throws
        // away the one place a user has already said how they want a site matched.
        uris: (lg.uris || []).map(u => {
          if(!u) return null;
          const uri = (typeof u === 'string') ? u : u.uri;
          if(!uri) return null;
          const m = (typeof u === 'object' && u.match != null) ? BW_MATCH[u.match] : null;
          return m ? { uri, match: m } : uri;
        }).filter(Boolean),
      });
    }
    if (type === 3) {
      const c = it.card || {};
      return Object.assign(base, { kind: 'card', card: {
        holder: String(c.cardholderName || ''), brand: String(c.brand || ''),
        number: String(c.number || ''), expMonth: String(c.expMonth || ''),
        expYear: String(c.expYear || ''), code: String(c.code || ''),
      }});
    }
    if (type === 4) {
      const d = it.identity || {};
      return Object.assign(base, { kind: 'identity', identity: Object.fromEntries(
        Object.entries(d).filter(([, v]) => v != null && v !== '').map(([k, v]) => [k, String(v)])) });
    }
    return Object.assign(base, { kind: 'note' });
  }

  /* Bitwarden puts SEVERAL URIs in one `login_uri` cell, joined by commas — not newlines, which is
   * what this used to split on. So an entry with more than one site came across as a single
   * unparseable URL and matched nothing at all: measured on a real export, the entry saved for
   * https://poster.place (plus yakihonne, primal, a LAN address and an onion) was invisible on
   * poster.place, which is exactly how it was reported.
   *
   * Split on a comma ONLY where a new URL plainly begins — a scheme, or a bare host — because a
   * single URL can legitimately carry commas in its query string, and that same export has OAuth
   * redirect URLs that do. Newlines split too; different Bitwarden versions have used both. */
  function splitUris(cell){
    const out = [];
    for (const line of String(cell || '').split('\n')) {
      let seg = '';
      for (let i = 0; i < line.length; i++) {
        const c = line[i];
        if (c === ',' && _startsUrl(line.slice(i + 1))) { out.push(seg); seg = ''; continue; }
        seg += c;
      }
      out.push(seg);
    }
    return out.map(u => u.trim().replace(/,+$/, '')).filter(Boolean);
  }

  /* Does a new URL begin here? ONLY an explicit `scheme://` counts.
   *
   * The tempting extra rule — also split before a bare host — is unsafe in a password manager,
   * because matchLevel is what the extension consults before handing a plaintext password to a
   * page. `https://ex.com/cb?redirect=https://good.com,evil.com/pwn` and `https://ex.com/a,b.io/c`
   * are each ONE saved URL; splitting them produced a second "URI" of `evil.com` / `b.io`, and the
   * credential was then offered on a third-party domain that merely appeared inside the user's own
   * link. Narrowing the rule (no splitting inside a query, require a real TLD) shrank that class
   * without closing it.
   *
   * The cost is a secondary URI written without a scheme — `https://cloud.example/,test.example` —
   * staying attached to the first, so it does not match on its own. That is a missing convenience
   * the user can see and fix. The other direction is a password offered on a domain they never
   * saved, which they cannot see at all. */
  function _startsUrl(rest) {
    return /^\s*[a-z][a-z0-9+.-]*:\/\//i.test(rest);
  }

  /* The CSV export. Quoted fields with embedded commas and newlines are the normal case here (a
   * secure note is multi-line), so this is a real parser and not a split(','). */
  function parseCsv(text) {
    const rows = [];
    let row = [], cell = '', q = false;
    const s = String(text || '').replace(/\r\n/g, '\n');
    for (let i = 0; i < s.length; i++) {
      const c = s[i];
      if (q) {
        if (c === '"') { if (s[i + 1] === '"') { cell += '"'; i++; } else q = false; }
        else cell += c;
      } else if (c === '"') q = true;
      else if (c === ',') { row.push(cell); cell = ''; }
      else if (c === '\n') { row.push(cell); rows.push(row); row = []; cell = ''; }
      else cell += c;
    }
    if (cell.length || row.length) { row.push(cell); rows.push(row); }
    return rows.filter(r => r.length > 1 || (r[0] || '').trim());
  }

  function _bwCsv(text) {
    const rows = parseCsv(text);
    if (!rows.length) return { items: [], folders: [] };
    const head = rows[0].map(h => String(h || '').trim().toLowerCase());
    const col = (name) => head.indexOf(name);
    const iName = col('name'), iUser = col('login_uri') >= 0 ? col('login_username') : col('username');
    const iPass = col('login_password') >= 0 ? col('login_password') : col('password');
    const iUri = col('login_uri') >= 0 ? col('login_uri') : col('uri');
    const iTotp = col('login_totp'), iNotes = col('notes'), iFolder = col('folder'), iFav = col('favorite');
    const items = [], folders = new Set();
    for (const r of rows.slice(1)) {
      const get = (i) => (i >= 0 && i < r.length ? String(r[i] || '') : '');
      const name = get(iName).trim();
      const user = get(iUser), pass = get(iPass), uri = get(iUri);
      if (!name && !user && !pass) continue;
      const folder = get(iFolder).trim();
      if (folder) folders.add(folder);
      items.push({
        src: { app: 'bitwarden', id: '' },
        kind: (user || pass || uri) ? 'login' : 'note',
        title: name || uri || 'Untitled',
        folder, username: user, password: pass, totp: get(iTotp),
        uris: splitUris(uri),
        notes: get(iNotes), favorite: /^(1|true)$/i.test(get(iFav)), fields: [],
      });
    }
    return { items, folders: Array.from(folders) };
  }

  /* Weak/reused/old — the report a password manager owes you, computed locally over the decrypted
   * set. Reuse is counted by exact password across DIFFERENT sites: the same password on two URIs of
   * one account is not reuse, and flagging it teaches people to ignore the warning. */
  function audit(items, now) {
    const t = now || Math.floor(Date.now() / 1000);
    const logins = (items || []).filter(i => i && i.kind === 'login' && i.password);
    const byPw = new Map();
    for (const i of logins) {
      const k = i.password;
      const dom = baseDomain(hostOf(itemUris(i)[0] || '')) || (i.title || '');
      if (!byPw.has(k)) byPw.set(k, new Set());
      byPw.get(k).add(dom);
    }
    const reused = logins.filter(i => (byPw.get(i.password) || new Set()).size > 1);
    const weak = logins.filter(i => i.password.length < 12);
    const old = logins.filter(i => i.updated && (t - i.updated) > 365 * 86400);
    const noTotp = logins.filter(i => !i.totp);
    return { total: logins.length, reused, weak, old, noTotp };
  }

  return {
    toB64, fromB64, toHex, fromHex,
    newVaultKey, seal, open,
    b32decode, totp, totpRemaining, parseOtpAuth, totpConfig,
    generate, entropyBits, randInt,
    hostOf, baseDomain, matchLevel, matchesFor, itemUris, itemUriRules,
    parseBitwarden, parseCsv, audit,
  };
});
