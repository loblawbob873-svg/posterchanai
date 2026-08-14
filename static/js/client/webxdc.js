/* #webxdc — mini apps (games, polls, shared editors) from a `.xdc` file, run in a sandbox and kept
 * in step over Nostr.
 *
 * A `.xdc` is a zip with an index.html in it. The webxdc spec gives such an app exactly one thing —
 * `sendUpdate()` / `setUpdateListener()`, a shared append-only log scoped to the message it was
 * posted in — and takes away everything else: no network, no origin of its own to escape into. Every
 * game and poll is built on that one primitive, which is why the whole ecosystem works in any
 * messenger that implements it.
 *
 * THE TRANSPORT IS NOSTR, per Ditto's NOSTR_WEBXDC draft (NIP-DC), so a game started in Ditto is
 * playable here and vice versa:
 *   • the app is an attachment — an `imeta` tag (NIP-92) with `m application/x-webxdc`, or a kind
 *     1063 file-metadata event, carrying `url`, `x` (sha256) and a `webxdc` identifier;
 *   • `sendUpdate()` publishes kind 4932 with that identifier in an `i` tag and the payload as
 *     content;
 *   • the identifier — NOT the event id — is what makes two people the same game. Copying the app
 *     into a new post with a new identifier starts a fresh one, which is exactly what a "new game"
 *     button should do.
 *
 * SERIALS ARE OURS. The spec has them ordered and increasing with gaps allowed, and says nothing
 * about them being the same for everyone — so they are assigned locally, by (created_at, id) over
 * what this device has seen. That is what lets an append-only log ride a network with no global
 * ordering: two devices can disagree about the numbers and still agree about the SET, which is all
 * `setUpdateListener(cb, lastSerial)` actually needs.
 *
 * WHERE IT RUNS is the security of the whole feature: a different ORIGIN, because same-origin means
 * the game can read the localStorage and IndexedDB this client keeps your key and your session in.
 * One dedicated hostname (`xdc.<instance>`) is enough for that, and it is one extra name on the
 * certificate certbot already renews — no wildcard, no DNS API token. See `sandboxOrigin` for the
 * two designs that were tried and measured first, static/webxdc-sandbox/ for the two files that
 * origin serves, and docs/WEBXDC.md.
 */
(function(){
  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, enc, toast, publish } = PC;
    const Relay = window.Relay;

    const KIND_UPDATE = 4932;          // NIP-DC state update (regular event)
    const KIND_REALTIME = 20932;       // NIP-DC realtime data (EPHEMERAL: relays forward, store nothing)
    const MIME = 'application/x-webxdc';
    /* 256 MB. Not a guess: the published Half-Life port is 178 MB, because it ships the three demo
     * campaigns (dayone.zip 75 MB, hldm.zip 62 MB, uplink.zip 29 MB) beside a 3.7 MB Xash wasm. A cap
     * sized for "a zip of HTML and sprites" refuses the most impressive app in the ecosystem, and the
     * refusal reads as the app being broken. The archive is never unzipped whole (see load), so the
     * cost of a big one is the download and one copy of the bytes. */
    const MAX_XDC = 256 * 1024 * 1024;
    const UPDATE_MAX = 128000;         // the spec's sendUpdateMaxSize default
    const UPDATE_INTERVAL = 1000;      // we are not an email network; a move should land immediately

    // ---- the sandbox origin ---------------------------------------------------------------------

    /* A per-device secret, so the subdomain an app runs on cannot be guessed.
     *
     * The label is derived rather than random so that reopening the same game returns to the same
     * origin — and therefore to the same localStorage, which is where an app keeps whatever it did
     * not send as an update. Deriving it from a DEVICE-LOCAL secret is what stops a second app (or a
     * page anywhere else) computing another app's origin and reading its storage. */
    function seed(){
      try{
        let s = localStorage.getItem('pc_sandbox_seed');
        if(!s){
          s = [...crypto.getRandomValues(new Uint8Array(32))].map(b => b.toString(16).padStart(2, '0')).join('');
          localStorage.setItem('pc_sandbox_seed', s);
        }
        return s;
      }catch(_){ return 'pc-fallback-seed'; }          // private mode: still isolated per app
    }
    const _hex = (b) => [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, '0')).join('');
    // 32 bytes as base36 — 50-odd characters, inside the 63-byte limit on one DNS label, and no
    // characters a hostname cannot carry (which rules out base64 and hex is too long at 64).
    function toBase36(bytes){
      let n = 0n;
      for(const b of new Uint8Array(bytes)) n = (n << 8n) | BigInt(b);
      let s = n.toString(36);
      while(s.length < 50) s = '0' + s;
      return s.slice(0, 50);
    }
    async function subdomain(id){
      const enc2 = new TextEncoder();
      const key = await crypto.subtle.importKey('raw', enc2.encode(seed()),
                                                { name:'HMAC', hash:'SHA-256' }, false, ['sign']);
      const mac = await crypto.subtle.sign('HMAC', key, enc2.encode('webxdc|' + id));
      return toBase36(mac);
    }
    /* WHERE THE SANDBOX LIVES — one dedicated HOSTNAME, `xdc.<instance>`.
     *
     * A different host is a different origin, which is the whole requirement: separate localStorage,
     * separate IndexedDB, separate cookies, no DOM access to the page that framed it. The client's
     * key and session live on the instance's origin and nothing here can reach them.
     *
     * Two designs were tried first and both are worth recording, because both look right:
     *
     *   A SUBDOMAIN PER APP (what Ditto does, via iframe.diy) is better still — it isolates apps
     *   from EACH OTHER as well as from the client. It needs a WILDCARD certificate, and certbot
     *   cannot issue one over HTTP-01: that means DNS-01, which means a DNS provider API token
     *   sitting on the web server. Too much standing credential for a game feature.
     *
     *   A PORT (https://host:8443) is also a distinct origin and needs no new certificate at all,
     *   which made it the obvious answer. MEASURED: it does not survive Cloudflare. CF accepts 8443
     *   from the browser and then connects to the ORIGIN on 443, so the request lands on the main
     *   vhost and the sandbox is never reached — proven with a marker header that appears on a
     *   direct request and is absent through the CDN. Do not re-attempt it.
     *
     * So: one ordinary hostname, one extra `-d` on the certificate certbot already renews. Every app
     * shares it, so an app can read another app's leftovers in localStorage (keys are namespaced in
     * the bridge, which is a collision guard, not a boundary). A node that HAS a wildcard can turn
     * on `pc_webxdc_wildcard` and get an origin per app. docs/WEBXDC.md says all of this out loud. */
    const SANDBOX_LABEL = 'xdc';
    function instanceHost(){
      let base = '';
      try{ base = (PC.apiBase && PC.apiBase()) || ''; }catch(_){}
      try{ return new URL(base || location.href).hostname; }catch(_){ return location.hostname; }
    }
    /* An instance that HAS a wildcard certificate can turn this on and get an origin per app. A
     * localStorage switch rather than a server setting, deliberately: it is a property of the
     * CERTIFICATE in front of whichever instance this client is pointed at, the packaged apps can be
     * pointed anywhere, and a dead server field nobody can reach would be worse than an honest
     * manual one. */
    function sandboxWildcard(){
      try{ return localStorage.getItem('pc_webxdc_wildcard') === '1'; }catch(_){ return false; }
    }
    function sandboxHostName(){
      try{ return SANDBOX_LABEL + '.' + instanceHost(); }catch(_){ return 'the sandbox origin'; }
    }
    async function sandboxOrigin(appId){
      const host = instanceHost();
      if(sandboxWildcard()) return 'https://' + (await subdomain(appId)) + '.' + host;
      return 'https://' + SANDBOX_LABEL + '.' + host;
    }

    // ---- reading the app ------------------------------------------------------------------------

    const _cache = new Map();          // key -> Promise<{bytes, index}>
    const CACHE = 'pc-webxdc-v1';      // the archives themselves, one Response per app URL

    async function sha256Hex(bytes){
      return _hex(await crypto.subtle.digest('SHA-256', bytes));
    }

    const _isDigest = (v) => /^[0-9a-f]{64}$/i.test(String(v || ''));

    /* THE URL IS THE IDENTITY, NOT `x`. This memo used to key on `app.sha || app.url`, and `x` is
     * only SOMETIMES a sha256 — the published Half-Life port carries the literal "hl", which is
     * exactly what the verification code a few lines down exists to tolerate. Two apps whose authors
     * both wrote a label there collide, and the second one served the first one's archive: the
     * wrong game, with no download and nothing in any console. The digest still joins the key when
     * it IS one, so a post correcting a wrong `x` is not answered from the old bytes. */
    function _cacheKey(app){
      const sha = String((app && app.sha) || '');
      return String((app && app.url) || '') + '#' + (_isDigest(sha) ? sha.toLowerCase() : '');
    }

    /* Forget everything THIS device remembers about an app, so the next launch starts from nothing.
     *
     * A mini app accumulates two kinds of stored state and neither has any UI of its own: the archive
     * cached here, and whatever the app itself wrote on the sandbox origin (localStorage, IndexedDB —
     * an emscripten game keeps its whole config there). When one of them goes bad the app fails the
     * same way on every launch for ever, and the only recovery a reader can reach is the browser's
     * "clear browsing data", which also signs them out of this instance. That is what turned an
     * evening of black screens into an evening: not the poison, the absence of a way out of it. */
    async function forget(app){
      try{ _cache.delete(_cacheKey(app)); }catch(_){}
      try{ const c = await caches.open(CACHE); await c.delete(app.url); }catch(_){}
    }

    /* Fetch the .xdc and unzip it, once per app per session.
     *
     * The `x` tag is VERIFIED when present. The URL is somebody else's server and the file is code
     * we are about to run: without the check, whoever hosts it can swap the app after it was posted,
     * for one reader or for everybody, and nothing about the post would change. A mismatch is fatal
     * and says so — silently running it anyway would make the check decorative. */
    async function load(app){
      const key = _cacheKey(app);
      if(_cache.has(key)) return _cache.get(key);
      const p = (async () => {
        /* FETCHED ONCE PER DEVICE, NOT PER LAUNCH.
         *
         * Uploading the app put it on a SERVER; running it needs the bytes in this BROWSER, because
         * the sandbox has no network by design and every file it asks for is answered from memory
         * here. So the download is unavoidable — but repeating it is not, and at 178 MB that is the
         * difference between "opens instantly" and "why is it downloading again". Kept in a cache of
         * its own rather than the client's: those evict on rules written for a firehose of timeline
         * images, and a mini app is a deliberate, enormous, content-addressed thing. */
        /* A CACHE HIT HAS TO EARN IT, AND A BAD ONE MUST NOT BE PERMANENT.
         *
         * `bytes.length > 0` is not a check: a truncated entry has a length, parses far enough to
         * look plausible, and then serves half an app. And whatever the reason, a cached archive that
         * cannot be opened used to THROW out of here — leaving the bad entry exactly where it was, so
         * the next launch failed identically, and the next, with nothing the reader could do about it
         * short of clearing the whole origin. So the hit is opened inside a try: anything wrong with
         * it deletes the entry and falls through to the network, which is the one path that can
         * actually fix it. The sha is checked too when the tag is a real digest — a cached copy is
         * bytes this device has held for days, and it is the only copy anything reads after that. */
        let bytes = null;
        try{
          const c = await caches.open(CACHE);
          const hit = await c.match(app.url);
          if(hit) bytes = new Uint8Array(await hit.arrayBuffer());
        }catch(_){}
        if(bytes && bytes.length){
          try{
            if(_isDigest(app.sha)){
              const got = await sha256Hex(bytes);
              if(got.toLowerCase() !== String(app.sha).toLowerCase()){
                throw new Error('the cached copy is not the app that was posted');
              }
            }
            return await _openArchive(bytes, app);
          }catch(e){
            try{ console.warn('[webxdc] dropping a cached archive that will not open:',
                              (e && e.message) || e); }catch(_){}
            try{ const c = await caches.open(CACHE); await c.delete(app.url); }catch(_){}
          }
        }
        const r = await fetch(app.url, { credentials:'omit', referrerPolicy:'no-referrer' });
        if(!r.ok) throw new Error('could not download the app (HTTP ' + r.status + ')');
        const len = Number(r.headers.get('content-length') || 0);
        if(len > MAX_XDC) throw new Error('that app is too large to open here');
        bytes = new Uint8Array(await r.arrayBuffer());
        if(bytes.length > MAX_XDC) throw new Error('that app is too large to open here');
        /* VERIFY THE HASH — WHEN THERE IS ONE. `x` is defined as the sha256, and checking it is what
         * stops whoever hosts the file swapping the app after it was posted. But the published
         * Half-Life port carries `["x", "hl"]` — a label, not a digest — and treating that as a
         * mismatch refuses the app outright, with a message accusing its author of tampering. So the
         * check applies to a value that IS a sha256, and a non-hash `x` is ignored rather than
         * enforced: the tag is advisory in the wild, and a wrong refusal is worse than a missing
         * check on a file the reader chose to open. */
        if(_isDigest(app.sha)){
          const got = await sha256Hex(bytes);
          if(got.toLowerCase() !== String(app.sha).toLowerCase()){
            throw new Error('this app does not match the one that was posted — refusing to run it');
          }
        }
        /* THE DIRECTORY, NOT THE CONTENTS. Unzipping everything up front costs the whole archive
         * again in memory — 178 MB of Half-Life becomes 178 MB of Map on top of the 178 MB of bytes,
         * on a phone, before a single frame is drawn. The central directory is a few KB, and one
         * entry is inflated per request (which is what the sandbox's worker asks for anyway). */
        const opened = await _openArchive(bytes, app);
        // Store only once it has PARSED. Caching bytes that turn out not to be an app would make a
        // bad download permanent, and the next launch would fail identically with no way to retry.
        try{
          const c = await caches.open(CACHE);
          await c.put(app.url, new Response(bytes, { headers:{ 'content-type': MIME } }));
        }catch(_){}                                        // out of quota: it still runs, just not cached
        return opened;
      })();
      _cache.set(key, p);
      p.catch(() => _cache.delete(key));               // a failed load must not be cached for ever
      return p;
    }

    /* THE DIRECTORY, NOT THE CONTENTS — see load(). Shared by the cached and freshly-downloaded
     * paths so an app that parses from the network parses identically from the cache. */
    async function _openArchive(bytes, app){
      const index = new Map();
      for(const e of window.PCZip.entries(bytes)) if(e.name) index.set(e.name, e);
      if(!index.has('index.html')) throw new Error('that .xdc has no index.html in it');
      return { bytes, index };
    }

    /* The app's name and icon, for the card — read from the archive rather than trusted from the
     * post, because the post is written by whoever shared it and the manifest by whoever wrote the
     * app. A tiny hand-rolled TOML read: the spec defines exactly two keys and pulling in a TOML
     * parser for `name = "…"` would be the third dependency this feature does not need. */
    async function manifestName(files){
      const b = await fileOf(files, 'manifest.toml');
      if(!b) return '';
      let text = '';
      try{ text = new TextDecoder().decode(b); }catch(_){ return ''; }
      const m = /^\s*name\s*=\s*(?:"([^"]*)"|'([^']*)')/m.exec(text);
      return (m && (m[1] || m[2]) || '').slice(0, 80);
    }
    // One entry, inflated on demand. Null when the archive has no such file.
    async function fileOf(files, name){
      const e = files.index.get(name);
      if(!e) return null;
      try{ return await window.PCZip.read(files.bytes, e); }catch(_){ return null; }
    }

    // ---- serving the app into the sandbox --------------------------------------------------------

    const MIMES = {
      html:'text/html', htm:'text/html', js:'text/javascript', mjs:'text/javascript',
      css:'text/css', json:'application/json', svg:'image/svg+xml', png:'image/png',
      jpg:'image/jpeg', jpeg:'image/jpeg', gif:'image/gif', webp:'image/webp', avif:'image/avif',
      ico:'image/x-icon', wasm:'application/wasm', woff:'font/woff', woff2:'font/woff2',
      ttf:'font/ttf', otf:'font/otf', mp3:'audio/mpeg', ogg:'audio/ogg', oga:'audio/ogg',
      wav:'audio/wav', m4a:'audio/mp4', mp4:'video/mp4', webm:'video/webm', txt:'text/plain',
      xml:'application/xml', map:'application/json', toml:'text/plain',
    };
    const mimeOf = (name) => MIMES[(name.split('.').pop() || '').toLowerCase()] || 'application/octet-stream';

    /* THE CONTENT-SECURITY-POLICY, on every response, and it is the second of the two independent
     * answers to "can this app reach the network" (the first is the service worker refusing every
     * cross-origin request). 'unsafe-inline' and 'unsafe-eval' are deliberate: mini apps are written
     * as single files with inline scripts and several ship a small interpreter. What matters is that
     * there is no host in this policy at all — 'self' is an origin serving nothing but this app. */
    const CSP = [
      "default-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' data: blob:",
      "connect-src 'self' data: blob:",
      "base-uri 'self'",
      "form-action 'none'",
    ].join('; ');

    const BRIDGE_PATH = '/__webxdc__.js';

    // Injected into every HTML response, as a <script src> rather than inline so it works even in an
    // app that ships its own restrictive CSP meta tag.
    function injectBridge(html){
      const tag = '<script src="' + BRIDGE_PATH + '"></script>';
      if(/<head[^>]*>/i.test(html)) return html.replace(/<head[^>]*>/i, (m) => m + tag);
      if(/<html[^>]*>/i.test(html)) return html.replace(/<html[^>]*>/i, (m) => m + tag);
      return tag + html;
    }

    const _enc = new TextEncoder(), _dec = new TextDecoder();
    const b64 = (bytes) => { let s = ''; const b = new Uint8Array(bytes);
      for(let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
      return btoa(s); };

    // ---- the bridge that runs inside the app -----------------------------------------------------

    /* `window.webxdc`, as the app sees it. A string because it is served INTO the sandbox — it runs
     * on the app's origin, not ours, and can therefore be reached by the app (which is fine: it can
     * only ask for what the API already offers) but reaches nothing of ours.
     *
     * It speaks JSON-RPC to `parent` — the sandbox loader — which forwards to this document. */
    const BRIDGE = `(function(){
  var nextId = 1, pending = {}, listener = null, ready = null, rtListener = null;
  /* A controller read by ANDROID and handed in, for the engine that will not hand one to the page.
     Same shape as a Gamepad (standard-mapping buttons + axes) so the shim below cannot tell the
     difference; 'padAt' is what makes a stale one fall back to the real API rather than pinning the
     player to the last state a dying bridge sent. */
  var padNative = null, padAt = 0;
  /* SHARED-ORIGIN STORAGE, NAMESPACED. Every app on this instance runs on one sandbox origin (see
     sandboxOrigin), so two games that both keep their save under "state" would overwrite each
     other. Keys are prefixed per app. This is a COLLISION guard, not a security boundary — the real
     store is still reachable by anything that looks for it — and it is unnecessary on a node with a
     wildcard, where each app has an origin of its own. A Proxy rather than a plain object because
     apps write \`localStorage.foo = 1\` as often as they call setItem, and an object shim silently
     drops those. */
  (function(ns){
    if(!ns) return;
    try{
      var real = window.localStorage, pre = '__xdc_' + ns + '_';
      var mine = function(k){ return k.indexOf(pre) === 0; };
      var api = {
        getItem: function(k){ return real.getItem(pre + k); },
        setItem: function(k, v){ real.setItem(pre + k, v); },
        removeItem: function(k){ real.removeItem(pre + k); },
        clear: function(){ Object.keys(real).filter(mine).forEach(function(k){ real.removeItem(k); }); },
        key: function(i){ var ks = Object.keys(real).filter(mine); return i < ks.length ? ks[i].slice(pre.length) : null; },
      };
      var shim = new Proxy(api, {
        get: function(t, p){
          if(p === 'length') return Object.keys(real).filter(mine).length;
          if(typeof p === 'string' && t[p] === undefined) return real.getItem(pre + p);
          return t[p];
        },
        set: function(t, p, v){ if(typeof p === 'string' && t[p] === undefined) real.setItem(pre + p, String(v)); return true; },
        deleteProperty: function(t, p){ real.removeItem(pre + p); return true; },
        has: function(t, p){ return t[p] !== undefined || real.getItem(pre + p) !== null; },
        ownKeys: function(){ return Object.keys(real).filter(mine).map(function(k){ return k.slice(pre.length); }); },
        getOwnPropertyDescriptor: function(t, p){
          var v = real.getItem(pre + p);
          return v === null ? undefined : { value: v, writable: true, enumerable: true, configurable: true };
        },
      });
      Object.defineProperty(window, 'localStorage', { value: shim, configurable: true });
    }catch(e){}
  })(__XDC.ns);
  function send(m){ try{ parent.postMessage(m, '*'); }catch(e){} }
  function rpc(method, params){
    var id = nextId++;
    return new Promise(function(res, rej){
      pending[id] = { res: res, rej: rej };
      send({ jsonrpc:'2.0', id:id, method:method, params:params });
    });
  }
  window.addEventListener('message', function(ev){
    var d = ev.data;
    if(!d || typeof d !== 'object' || d.jsonrpc !== '2.0') return;
    if(d.id !== undefined && d.method === undefined){
      var p = pending[d.id]; if(!p) return;
      delete pending[d.id];
      if(d.error) p.rej(new Error(d.error.message || 'error')); else p.res(d.result);
      return;
    }
    if(d.method === 'webxdc.update' && listener){
      try{ listener(d.params); }catch(e){}
    }
    if(d.method === 'webxdc.realtime' && rtListener){
      try{ rtListener(unb64(d.params && d.params.b64)); }catch(e){}
    }
    if(d.method === 'webxdc.padstate'){
      padNative = d.params || null;
      padAt = Date.now();
    }
  });
  function b64(bytes){
    var s = '', b = bytes;
    for(var i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
    return btoa(s);
  }
  function unb64(s){
    var bin = atob(s || ''), out = new Uint8Array(bin.length);
    for(var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  window.webxdc = {
    selfAddr: __XDC.addr,
    selfName: __XDC.name,
    sendUpdateInterval: ${UPDATE_INTERVAL},
    sendUpdateMaxSize: ${UPDATE_MAX},
    sendUpdate: function(update, descr){ return rpc('webxdc.sendUpdate', { update: update, descr: descr }); },
    setUpdateListener: function(cb, serial){
      /* "Calling setUpdateListener() multiple times is undefined behaviour" — so the LAST one wins
         and the promise is shared, rather than stacking listeners that each replay the history. */
      listener = cb;
      ready = rpc('webxdc.setUpdateListener', { serial: serial || 0 });
      return ready;
    },
    /* The realtime channel: ephemeral, unordered, delivered only to whoever is connected NOW. It is
       what a game with continuous motion needs — Quake III uses it, and sendUpdate cannot serve that
       purpose because every packet would be kept for ever by everyone. Optional in the spec, so an
       app that wants it feature-detects joinRealtimeChannel !== undefined; defining it is a promise
       that it works. Base64 over the wire rather than a transferred Uint8Array: structured clone of a
       typed array through two frames behaves differently across WebViews, and this is the one path
       that carries a packet every frame. */
    joinRealtimeChannel: function(){
      rpc('webxdc.rtJoin', {});
      return {
        setListener: function(cb){ rtListener = cb; },
        send: function(data){
          if(!(data instanceof Uint8Array)) throw new Error('realtime data must be a Uint8Array');
          if(data.length > ${UPDATE_MAX}) throw new Error('realtime packet too large');
          rpc('webxdc.rtSend', { b64: b64(data) });
        },
        leave: function(){ rtListener = null; rpc('webxdc.rtLeave', {}); },
      };
    },
  };
  /* AN APP THAT DIES SHOULD SAY SO. It runs in a nested frame on another origin, so its console is
     two devtools context-switches away from whoever is looking at a black rectangle — and a WebGL
     context that fails to create, a missing file, or a throw during boot all look identical from
     outside: nothing. These forward to the parent, which shows them. */
  window.addEventListener('error', function(e){
    send({ jsonrpc:'2.0', method:'webxdc.crash', params:{
      message: (e && (e.message || (e.error && e.error.message))) || 'error',
      where: (e && e.filename ? String(e.filename).split('/').pop() + ':' + e.lineno : '') } });
  });
  window.addEventListener('unhandledrejection', function(e){
    var r = e && e.reason;
    send({ jsonrpc:'2.0', method:'webxdc.crash', params:{
      message: (r && (r.message || String(r))) || 'unhandled rejection', where: '' } });
  });
  /* CAN THIS FRAME DRAW AT ALL? A game that renders through WebGL and cannot get a context paints
     nothing and often says nothing — a black rectangle, which is indistinguishable from "still
     loading" and from "crashed". The app runs nested inside a sandboxed cross-origin iframe, which
     is an unusual place to ask for a GPU context, so the answer is worth having rather than
     assuming. One scratch canvas, once, reported only when it FAILS. */
  try{
    var _c = document.createElement('canvas');
    var _gl = _c.getContext('webgl2') || _c.getContext('webgl');
    if(!_gl){
      send({ jsonrpc:'2.0', method:'webxdc.crash', params:{
        message: 'no WebGL context in the sandbox frame — a 3D app cannot draw here', where:'probe' } });
    }
  }catch(e){
    send({ jsonrpc:'2.0', method:'webxdc.crash', params:{
      message: 'WebGL probe threw: ' + ((e && e.message) || e), where:'probe' } });
  }
  /* A GAME CONTROLLER, FOR APPS THAT ONLY SPEAK KEYBOARD.

     Every layer under this one already worked, which is why it took a controller and a tester to find
     out where the gap was: 'gamepad' is delegated to both frames (webxdc.js and the sandbox loader),
     the sandbox origin is https so the secure-context rule is met, and Chromium enumerates the pad
     fine — measured on an EasySMX X15 over Bluetooth, which reports axes and buttons in a browser on
     the same phone the app was dead on. The gap is the APP: SDL2's emscripten joystick backend is
     compiled in by default, but a port has to SDL_Init(SDL_INIT_GAMECONTROLLER) and read the events,
     and the idTech-derived ports (the Doom and Quake family) overwhelmingly init video+audio only.
     The controller was sitting there and nothing was asking.

     So the pad is translated to the keys those games already read. This runs INSIDE the app's frame —
     it is part of the injected bridge, on the app's own origin — which is the only place it can:
     the client is a different origin and cannot dispatch an event into this document.

     POLLING IS NOT CONSENT, and assuming it was is what made the first version of this do NOTHING.
     It disabled itself as soon as the app touched 'navigator.getGamepads', on the reasoning that an
     app which reads the pad does not need fake keys. That reasoning is contradicted by the very fact
     that justifies this shim: SDL2's emscripten joystick backend is compiled in BY DEFAULT
     (SDL_JOYSTICK_EMSCRIPTEN is 1 in SDL_config_emscripten.h, so -sUSE_SDL=2 gets it without asking)
     and emscripten_sample_gamepad_data() IS navigator.getGamepads(). So any port that inits the
     joystick subsystem — SDL_INIT_EVERYTHING is enough, and it is what a lot of them pass — polls
     the pad every frame while its input code reads only the keyboard. Exactly the game this exists
     for, and it switched the shim off on the first frame. Reported as "no controller movement
     worked", with the shim installed, running, and correct.

     So the flag is REPORTED and no longer acted on, and the off switch is a real one the client owns
     ('pc_xdc_pad', default on). Getting it wrong now costs a gamepad-native app some extra arrow
     keys; getting it wrong the old way cost the whole feature, silently.

     WHY BOTH ARROWS AND WASD for one direction: Quake reads WASD, Doom reads the arrows, and in the
     ports that read both they are bound to the same action. Nothing here is a chord, so a game that
     knows only one of the two simply never sees the other.

     THE LOOP STARTS AT LOAD, not on 'gamepadconnected'. Waiting for that event was the second way
     this could do nothing: gamepads are hidden from a document until it has seen a gamepad BUTTON
     press, that flag is per-Navigator, and whether the event reaches a doubly-nested cross-origin
     frame at all is exactly the thing nobody could confirm. A poll is one array read in a page that
     is already running a game loop, so the cautious version costs nothing measurable and removes a
     whole class of never-started.

     AND IT COUNTS WHAT IT SAW. There is no controller and no phone on the machine this is written
     on, so the difference between "no pad visible", "pad visible, no press", "keys sent, game
     ignored them" and "the app turned it off" has to come back from the device — the same reason
     MusicPlugin.status() exists. PC.padStats() reads it. */
  (function(){
    var poll = navigator.getGamepads && navigator.getGamepads.bind(navigator);
    if(!poll) return;
    var stat = { on:(__XDC.pad !== false), pads:0, presses:0, keys:0, appPolls:0, frames:0, last:'' };
    try{ navigator.getGamepads = function(){ stat.appPolls++; return poll(); }; }catch(e){}
    // Reported to the parent, which is the only side with a UI. Throttled: this is a diagnostic, not
    // a telemetry stream, and it rides the same postMessage channel the game's own updates use.
    setInterval(function(){
      try{ send({ jsonrpc:'2.0', method:'webxdc.padstat', params:stat }); }catch(e){}
    }, 2000);
    if(__XDC.pad === false) return;

    // key → [KeyboardEvent.key, .code, legacy keyCode]. emscripten reads keyCode, so it is not
    // optional: a synthetic event carrying 0 there is "no key" to every SDL build.
    var K = {
      up:['ArrowUp','ArrowUp',38], down:['ArrowDown','ArrowDown',40],
      left:['ArrowLeft','ArrowLeft',37], right:['ArrowRight','ArrowRight',39],
      w:['w','KeyW',87], a:['a','KeyA',65], s:['s','KeyS',83], d:['d','KeyD',68],
      e:['e','KeyE',69], q:['q','KeyQ',81], r:['r','KeyR',82],
      space:[' ','Space',32], ctrl:['Control','ControlLeft',17], shift:['Shift','ShiftLeft',16],
      enter:['Enter','Enter',13], esc:['Escape','Escape',27], tab:['Tab','Tab',9] };
    // Standard mapping button indices. 12-15 are the d-pad, which is why it is here and not with the
    // axes — Android reports a d-pad as a hat on some pads and as buttons on others, and the browser
    // normalises both to these four when 'mapping === "standard"'.
    var BTN = { 0:['space'], 1:['ctrl'], 2:['enter'], 3:['e'], 4:['q'], 5:['r'], 6:['shift'],
                7:['ctrl'], 8:['tab'], 9:['esc'],
                12:['up','w'], 13:['down','s'], 14:['left','a'], 15:['right','d'] };

    var held = {}, raf = 0;

    /* WHERE THE EVENT GOES. An event dispatched on 'document' never passes through <body>, and one
       dispatched on 'window' passes through nothing at all — so aiming at the wrong node is a shim
       that fires perfectly and reaches no listener. The deepest plausible node is the target and
       'bubbles' carries it up to every other: an emscripten canvas listener, a body listener, a
       document listener and a window listener are all served by one dispatch. */
    function target(){
      var el = document.activeElement;
      if(el && el !== document.body && el !== document.documentElement) return el;
      return document.querySelector('canvas') || document.body || document;
    }
    function fire(type, name){
      var k = K[name]; if(!k) return;
      var ev;
      try{
        ev = new KeyboardEvent(type, { key:k[0], code:k[1], keyCode:k[2], which:k[2], charCode:0,
                                       bubbles:true, cancelable:true, composed:true, view:window,
                                       repeat:false, location:0 });
      }catch(e){ return; }
      // keyCode/which in the init dict are a LEGACY extension: Chrome honours them, and an engine
      // that does not leaves 0 behind — which is the silent half of this, since the event still
      // dispatches and still carries a correct 'key' nothing old reads.
      if(ev.keyCode !== k[2]){
        try{
          Object.defineProperty(ev, 'keyCode', { get:function(){ return k[2]; } });
          Object.defineProperty(ev, 'which',   { get:function(){ return k[2]; } });
        }catch(e2){}
      }
      try{ target().dispatchEvent(ev); }catch(e3){}
    }
    function release(){
      var ns = Object.keys(held);
      for(var i = 0; i < ns.length; i++){ delete held[ns[i]]; fire('keyup', ns[i]); }
    }
    /* HYSTERESIS, not one threshold. A stick resting near the edge crosses a single threshold many
       times a second, and each crossing is a keyup+keydown pair — which a game reads as the key
       being tapped rather than held. Harder to leave than to enter. */
    function axis(want, v, neg, negAlt, pos, posAlt){
      if(typeof v !== 'number') return;
      if(v <= (held[neg] ? -0.35 : -0.5)){ want[neg] = 1; want[negAlt] = 1; }
      else if(v >= (held[pos] ? 0.35 : 0.5)){ want[pos] = 1; want[posAlt] = 1; }
    }
    /* THE RIGHT STICK IS LOOK, AND LOOK IS THE MOUSE — which is why mapping it to keys like the
       left one would not have worked either. Only ax[0]/ax[1] were ever read here, so on a pad with
       two sticks the second one did nothing at all: reported as "left joystick is doing everything,
       right joystick doing nothing", and true in this shim long before any native plugin existed. It
       works in Firefox because the app reads the real Gamepad API there and gets all four axes
       itself; this shim only exists for apps that read the KEYBOARD, and a keyboard has no aim.

       So the right stick is delivered as relative mouse motion. movementX/movementY is what a
       pointer-locked engine reads — Quake, Half-Life and OpenArena all do — and pointer lock is
       delegated to the frame by the iframe allow attribute, so the lock is real and the deltas land
       where the engine expects them. Dispatched at the locked element when there is one, because an
       engine holding the lock listens there rather than at the document.

       NOTE FOR ANYONE EDITING THIS BLOCK: it lives inside a template literal, so a backtick here
       ends the bridge and the syntax error lands hundreds of lines away.

       Squared response, deliberately: a stick is an ANALOGUE control being read once a frame, and a
       linear map makes small corrections impossible while large ones fly past the target. */
    /* Pixels per frame at FULL deflection, LINEAR, with only enough deadzone to swallow drift.
       Two goes at this were both wrong in the same direction. 18px with a SQUARED response put
       half-stick at 4.5px ("way too slow"); 45px with a 1.5 power still put a quarter-stick at
       5.6px, which reads as the stick RESISTING — you have to shove it before anything happens, and
       then it arrives all at once. A curve is a way of buying fine aim near centre, and it is not
       worth buying at that price: an engine applies its own sensitivity anyway, and a player who
       wants a curve has one in the game's own settings.
       THE DEADZONE IS ABOUT THE SPRING, NOT THE NOISE, and 0.06 was set against the wrong one. A
       stick released from deflection does not stop at centre — it swings PAST it and settles back,
       and at 0.06 with a linear response that overshoot clears the gate at nearly full rate, in the
       opposite direction: "I move one way and then it follows then jerks me in opposite direction".
       The old 1.5-power curve hid it by crushing small values, which is why it appeared only once
       the response was straightened. Resting drift measures 0.0153, so 0.15 is ten times the noise
       and comfortably past a spring's return swing, while everything ABOVE it still answers
       linearly — the dead region is wider, the response inside it is not weaker. Tunable with
       PCWebxdc.dead(n) for a pad with a looser or tighter return than this one. */
    var LOOK_PX = (typeof __XDC.look === 'number' && __XDC.look > 0) ? __XDC.look : 45;
    var DEAD = (typeof __XDC.dead === 'number' && __XDC.dead >= 0 && __XDC.dead < 0.9)
                 ? __XDC.dead : 0.15;
    var lookX = -1, lookY = -1;      // virtual cursor; -1 until the first aim seeds it at the centre
    function look(x, y){
      if(typeof x !== 'number' || typeof y !== 'number') return;
      var ax2 = Math.abs(x) > DEAD ? x : 0;
      var ay2 = Math.abs(y) > DEAD ? y : 0;
      if(!ax2 && !ay2) return;
      var dx = Math.round(ax2 * LOOK_PX), dy = Math.round(ay2 * LOOK_PX);
      if(!dx && !dy) return;
      stat.look = (stat.look || 0) + 1;
      var t = document.pointerLockElement || document.body || document.documentElement;
      if(!t) return;
      /* A VIRTUAL CURSOR, because clientX/clientY are not decoration here.
         movementX/Y is only meaningful to an engine holding the pointer lock; one that is NOT locked
         ignores it and reads the ABSOLUTE position instead. Sending 0,0 every frame told those
         engines the cursor was pinned to the top-left corner — which, mixed with any real input,
         reads as being thrown the other way: "press right joystick and sometimes it sends me the
         other direction jerking me around". So the position advances by the same delta and is
         clamped to the viewport, and both kinds of engine see a cursor that moves the way the stick
         was pushed. Seeded at the centre rather than 0,0 for the same reason. */
      var vw = window.innerWidth || 640, vh = window.innerHeight || 480;
      if(lookX < 0){ lookX = vw / 2; lookY = vh / 2; }
      lookX = Math.max(0, Math.min(vw - 1, lookX + dx));
      lookY = Math.max(0, Math.min(vh - 1, lookY + dy));
      try{
        t.dispatchEvent(new MouseEvent('mousemove', {
          bubbles: true, cancelable: true, view: window,
          movementX: dx, movementY: dy,
          clientX: lookX, clientY: lookY, screenX: lookX, screenY: lookY }));
      }catch(e){
        /* An engine on an older WebView may not accept movementX through the constructor. Falling
           back to a plain event with the properties attached keeps it working there rather than
           losing aim entirely — read-only on the prototype, so define them on the instance. */
        try{
          var ev = document.createEvent('MouseEvents');
          ev.initMouseEvent('mousemove', true, true, window, 0, lookX, lookY, lookX, lookY,
                            false, false, false, false, 0, null);
          Object.defineProperty(ev, 'movementX', { value: dx });
          Object.defineProperty(ev, 'movementY', { value: dy });
          t.dispatchEvent(ev);
        }catch(e2){}
      }
    }
    function tick(){
      raf = requestAnimationFrame(tick);
      stat.frames++;
      // A backgrounded app must not be left holding a key: it would still be walking into a wall
      // when whoever put the phone down comes back.
      if(document.hidden){ release(); return; }
      /* NATIVE FIRST, then the real API. In the APK the WebView hands the page no gamepad at all —
         the same game with the same pad on the same tablet works in Firefox and does nothing here —
         so Android reads the controller and passes it in (see GamepadPlugin). A stale snapshot is
         treated as gone, or a bridge that stops reporting would pin the player to whatever it last
         said. The window is generous BECAUSE Android only sends a MotionEvent when something
         CHANGES: a stick held at a steady angle can legitimately report nothing for a while, and at
         one second that starved the aim mid-turn — "laggy or resisting". Backgrounding still
         releases instantly (above), which is the case the short window was really protecting.
         When both exist the native one wins simply because it is the one proven to arrive; they
         carry identical shapes, so nothing below can tell. */
      var p = null, i;
      if(padNative && (Date.now() - padAt) < 4000){
        p = { buttons: padNative.buttons || [], axes: padNative.axes || [],
              id: padNative.id || 'native', mapping: 'standard', connected: true };
        stat.src = 'native';
      }else{
        var pads = poll() || [];
        for(i = 0; i < pads.length; i++) if(pads[i] && pads[i].connected){ p = pads[i]; break; }
        stat.src = p ? 'webapi' : 'none';
      }
      stat.pads = p ? 1 : 0;
      if(!p){ release(); return; }
      if(!stat.id) stat.id = String(p.id || '').slice(0, 60) + ' [' + (p.mapping || 'no mapping') + ']';
      var want = {}, b = p.buttons || [], idx, j, list, bb, on;
      for(idx in BTN){
        bb = b[idx];
        if(bb == null) continue;
        on = (typeof bb === 'object') ? (bb.pressed || bb.value > 0.5) : (bb > 0.5);
        if(!on) continue;
        list = BTN[idx];
        for(j = 0; j < list.length; j++) want[list[j]] = 1;
      }
      var ax = p.axes || [];
      axis(want, ax[0], 'left', 'a', 'right', 'd');
      axis(want, ax[1], 'up', 'w', 'down', 's');
      look(ax[2], ax[3]);
      // Releases before presses, so a direction reversed inside one frame is not briefly both.
      var hn = Object.keys(held);
      for(i = 0; i < hn.length; i++) if(!want[hn[i]]){ delete held[hn[i]]; fire('keyup', hn[i]); }
      var wn = Object.keys(want);
      for(i = 0; i < wn.length; i++) if(!held[wn[i]]){ held[wn[i]] = 1; fire('keydown', wn[i]); }
      if(wn.length){ stat.presses++; stat.keys += wn.length; stat.last = wn.join('+'); }
    }
    function start(){ if(!raf) raf = requestAnimationFrame(tick); }
    window.addEventListener('gamepadconnected', start);
    window.addEventListener('blur', release);
    start();   // …and at load, because the event may never reach a doubly-nested cross-origin frame
    // In case a pad is already visible to this document (a reload after the gesture was given).
    try{ var now = poll() || []; for(var n = 0; n < now.length; n++) if(now[n]) { start(); break; } }catch(e){}
  })();
  send({ jsonrpc:'2.0', method:'webxdc.hello' });
})();`;

    // ---- one running app -------------------------------------------------------------------------

    /* Everything about one open mini app: its files, its frame, its subscription and the serial
     * counter it hands out. One instance per window — two people can have the same game open in two
     * windows, and they must not share a listener. */
    function Session(app, files){
      this.app = app;                    // {url, sha, uuid, name}
      this.files = files;
      this.frame = null;
      this.origin = '';
      /* THIS RUN'S TOKEN, and it is what stops one game serving another one's files.
       *
       * Every mini app on this instance runs on ONE sandbox origin, so they share ONE service worker.
       * That worker used to answer a request from whichever loader came first out of `matchAll()` —
       * so with Half-Life still open, pressing Play on Quake III started Half-Life. It is handed to
       * the sandbox in the frame's URL, travels to the app's own frame from there, and the worker
       * answers only the loader holding the same one (sw.js). A uuid rather than a counter because
       * an app can read its own URL and must not be able to guess anybody else's. */
      this.token = (crypto.randomUUID ? crypto.randomUUID()
                                      : _hex(crypto.getRandomValues(new Uint8Array(16))));
      this.sub = null;
      this.rtSub = null;            // the realtime channel, when an app joins one
      this._rtNext = null;          // the newest unsent realtime packet (newest wins)
      this._rtBusy = false;
      this._rtSk = null;            // the realtime channel's own key — see rtKey()
      this.rtPk = '';               // its pubkey, which is how we drop our own packets
      this.seen = new Map();             // event id -> serial
      this.ordered = [];                 // events, oldest first
      this.listening = false;
      this.wantSerial = 0;
      this.delivered = 0;                // the mark deliver() sends from
      this._frozen = 0;                  // how much of `ordered` the app has actually been handed
      this.self = { addr: '', name: '' };
      this.dead = false;
    }

    Session.prototype.destroy = function(){
      this.dead = true;
      // Relay.subscribe hands back an ID, and Relay.close takes it — a live REQ left open for a game
      // nobody has on screen is the cost the Notes subscription audit was about.
      try{ if(this.sub) Relay.close(this.sub); }catch(_){}
      this.sub = null;
      try{ if(this.rtSub) Relay.close(this.rtSub); }catch(_){}
      this.rtSub = null;
      if(this._onMsg) window.removeEventListener('message', this._onMsg);
      this._onMsg = null;
      if(this.frame && this.frame.parentElement) this.frame.remove();
      this.frame = null;
      // The full-screen sheet is part of the session, not something the caller has to remember to
      // remove — a destroy that leaves it standing is a black rectangle with no way out of it.
      if(this.sheet && this.sheet.parentElement) this.sheet.remove();
      this.sheet = null;
      if(this.onDestroy){ const f = this.onDestroy; this.onDestroy = null; try{ f(); }catch(_){} }
    };

    Session.prototype.post = function(m, transfer){
      if(!this.frame || !this.frame.contentWindow || !this.origin) return;
      try{ this.frame.contentWindow.postMessage(m, this.origin, transfer || []); }catch(_){}
    };

    /* Resolve one request from the archive. `/` is index.html; the bridge is a virtual file that is
     * not in the zip; anything else is looked up by path. A miss is a 404 rather than an error,
     * because apps probe for optional files (favicon, a manifest) and a thrown error would surface
     * as a broken sandbox rather than as a missing icon. */
    Session.prototype.resolve = async function(pathname){
      if(pathname === BRIDGE_PATH){
        /* The per-session values are handed to the bridge as ONE object it reads, rather than
         * patched into its source with string replacement. Substituting into code that also
         * contains the literals being searched for is how a bridge ends up with somebody's display
         * name spliced into a function body. */
        const head = 'var __XDC = ' + JSON.stringify({
          addr: this.self.addr, name: this.self.name,
          ns: String(this.app.uuid || this.app.sha || '').replace(/[^A-Za-z0-9_-]/g, '').slice(0, 64),
          /* The controller shim's off switch, decided HERE because the app frame has no settings UI
           * and no durable storage of its own worth the name. Default on: a game that reads the pad
           * natively getting keys as well is a nuisance, while a keyboard-only game getting nothing
           * is the entire feature failing. */
          pad: (function(){ try{ return localStorage.getItem('pc_xdc_pad') !== '0'; }catch(e){ return true; } })(),
          /* Look sensitivity, in pixels of mouse motion per frame at full stick. Decided here for
             the same reason the off switch is, and adjustable without a rebuild (PCWebxdc.look(n))
             because "too slow" and "too fast" are the same distance from right and neither can be
             judged from here — the pad, the game and the screen are all the player's. */
          look: (function(){ try{ var n = parseFloat(localStorage.getItem('pc_xdc_look'));
                                  return (isFinite(n) && n > 0 && n <= 400) ? n : 45; }
                             catch(e){ return 45; } })(),
          /* Right-stick deadzone. Sized for the SPRING's return swing, not for resting noise —
             see the aim code. */
          dead: (function(){ try{ var n = parseFloat(localStorage.getItem('pc_xdc_dead'));
                                  return (isFinite(n) && n >= 0 && n < 0.9) ? n : 0.15; }
                             catch(e){ return 0.15; } })(),
        }) + ';\n';
        return { status:200, contentType:'text/javascript', body:_enc.encode(head + BRIDGE) };
      }
      let name = decodeURIComponent(String(pathname || '/')).replace(/^\/+/, '');
      if(!name || name.charAt(name.length - 1) === '/') name += 'index.html';
      name = window.PCZip.normalise(name);
      if(!this.files.index.has(name) && name !== 'index.html'
         && this.files.index.has(name + '/index.html')) name = name + '/index.html';
      const bytes = await fileOf(this.files, name);
      if(!bytes) return { status:404, contentType:'text/plain', body:_enc.encode('not in this app') };
      const type = mimeOf(name);
      if(type === 'text/html'){
        let html = '';
        try{ html = _dec.decode(bytes); }catch(_){ html = ''; }
        return { status:200, contentType:'text/html; charset=utf-8', body:_enc.encode(injectBridge(html)) };
      }
      return { status:200, contentType:type, body:bytes };
    };

    // ---- the Nostr half ---------------------------------------------------------------------------

    /* Deliver everything the app has not been given yet, oldest first.
     *
     * `max_serial` is the highest we hold, which is how an app knows it has caught up (the spec:
     * "when max_serial equals serial, this update is the last one"). Both are LOCAL numbers — see
     * the header — so they are assigned here, in (created_at, id) order.
     *
     * DRIVEN BY WHAT HAS BEEN DELIVERED, never by an index captured before a sort. This used to read
     * `before = ordered.length`, absorb, then deliver from `before` — but absorb() SORTS, so an
     * update that lands out of order (a peer whose clock is behind, or the same second with a lower
     * id — both ordinary) takes a position at or before `before`. The app was then handed a
     * DUPLICATE of an old update and never saw the new one at all, which for an append-only log is
     * a move that silently never happened. Worst on the sender's own echo, which the spec requires
     * be delivered. */
    Session.prototype.deliver = function(){
      const max = this.ordered.length;
      const from = Math.max(0, Math.min(this.delivered, max));
      this.delivered = max;
      for(let i = from; i < max; i++){
        const ev = this.ordered[i];
        let payload = null;
        try{ payload = JSON.parse(ev.content || 'null'); }catch(_){ payload = null; }
        const tag = (n) => { const t = (ev.tags || []).find(x => x[0] === n); return t && t[1]; };
        this.post({ jsonrpc:'2.0', method:'webxdc.update', params:{
          payload: payload,
          serial: i + 1,
          max_serial: max,
          info: tag('info') || undefined,
          document: tag('document') || undefined,
          summary: tag('summary') || undefined,
        } });
      }
      this._frozen = max;                // nothing at or below this may be reordered again
    };

    Session.prototype.absorb = function(evs){
      let added = false;
      for(const ev of (evs || [])){
        if(!ev || ev.kind !== KIND_UPDATE || this.seen.has(ev.id)) continue;
        this.seen.set(ev.id, 0);
        this.ordered.push(ev);
        added = true;
      }
      if(!added) return false;
      /* Total order, and STABLE: two updates in the same second are broken by id, so every device
       * that holds both puts them in the same order. Sorting rather than inserting is fine — a
       * game's history is hundreds of events, not millions.
       *
       * ONLY THE UNDELIVERED TAIL IS SORTED. A serial is a promise: once update 7 has been handed to
       * the app, nothing may become update 7 afterwards. Re-sorting the whole list lets a late
       * arrival slide in under events the app already has, which renumbers them — so the app would
       * be told the same serial twice with different payloads, and `max_serial` would stop meaning
       * "you are caught up". The delivered prefix is frozen; a late event joins the tail and is
       * ordered among the others still waiting to go out.
       *
       * The mark is what has actually been POSTED, not `delivered` — those differ for exactly one
       * call, and it is the one that matters. `setUpdateListener(cb, 12)` seeds `delivered = 12`
       * while this session has posted NOTHING, so freezing 12 events there would freeze them in the
       * arbitrary order the relay happened to answer in, and the resumed app would then be handed
       * the wrong tail. Before the first deliver() the whole list is still free to sort. */
      const head = this.ordered.slice(0, this._frozen);
      const tail = this.ordered.slice(this._frozen);
      tail.sort((a, b) => (a.created_at - b.created_at) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
      this.ordered = head.concat(tail);
      return true;
    };

    Session.prototype.start = async function(fromSerial){
      this.listening = true;
      this.wantSerial = Math.max(0, Number(fromSerial) || 0);
      // `setUpdateListener(cb, serial)` means "I already have everything up to `serial`", so that is
      // where delivery starts. It is NOT a freeze mark — see absorb().
      this.delivered = this.wantSerial;
      const filter = { kinds:[KIND_UPDATE], '#i':[this.app.uuid], limit: 1000 };
      let evs = [];
      try{ evs = await Relay.query([filter]); }catch(_){ evs = []; }
      try{ (window.Store.query([filter]) || []).forEach(e => evs.push(e)); }catch(_){}
      this.absorb(evs);
      if(this.dead) return;
      this.deliver();
      // Live from here. A game is two people taking turns; without this the second player's move
      // arrives only if something else happens to re-query.
      try{
        this.sub = Relay.subscribe([filter], { onEvent: (ev) => {
          if(this.dead) return;
          if(this.absorb([ev])) this.deliver();
        } });
      }catch(_){}
    };

    /* sendUpdate → one kind-4932 event.
     *
     * The payload is the app's own JSON and is published as content. `alt` is required by the NIP
     * (NIP-31) so that a client which cannot run the app still shows something human; `info`,
     * `document` and `summary` are the optional fields of the same name in the spec. */
    Session.prototype.sendUpdate = async function(update, descr){
      const u = (update && typeof update === 'object') ? update : {};
      const content = JSON.stringify(u.payload === undefined ? null : u.payload);
      if(content.length > UPDATE_MAX) throw new Error('that update is too big to send');
      const tags = [['i', this.app.uuid],
                    ['alt', (this.app.name ? this.app.name + ': ' : '') + 'webxdc update']];
      const str = (v, n) => String(v == null ? '' : v).slice(0, n);
      if(u.info) tags.push(['info', str(u.info, 200)]);
      if(u.document) tags.push(['document', str(u.document, 200)]);
      if(u.summary) tags.push(['summary', str(u.summary, 200)]);
      // publish() answers {ev, ok, …} — the SIGNED EVENT is on `.ev`, and it is there even when the
      // relay refused it (the outbox may have queued it), which is what makes the echo below right.
      const r = await publish(KIND_UPDATE, content, tags);
      const ev = r && r.ev;
      /* Feed it back to ourselves rather than waiting for the relay to echo it. The spec is explicit
       * that the sender receives its own updates, and a game whose own move does not appear until a
       * round trip completes feels broken on a slow connection — and never appears at all if the
       * relay drops the echo. */
      if(ev && ev.id && !this.seen.has(ev.id)){
        if(this.absorb([ev])) this.deliver();
      }
      return true;
    };

    // ---- mounting ---------------------------------------------------------------------------------

    /* THE REALTIME CHANNEL IS SIGNED BY A KEY THAT IS NOBODY, MINTED PER SESSION AND NEVER STORED.
     *
     * It used to be signed with the user's identity, through `PC.sign` like every other event, and
     * that was wrong in a way only a real game shows: a moving player sends 20-30 packets a SECOND,
     * and with any external signer each one is a round trip to another program. A browser extension
     * answers that with "extension declined" — measured, in Brave, where everything else about mini
     * apps works perfectly — and Amber or a bunker would simply be a prompt storm. The channel is
     * unusable on exactly the signers most people have.
     *
     * A fresh secp256k1 key per session costs nothing and fixes it outright: signing is local and
     * ~1.77ms, so every signer behaves identically and the limitation this feature documented
     * ("a remote-signer player simply moves less smoothly") is gone.
     *
     * NOTHING IS LOST BY IT. Realtime data is not attributable in webxdc to begin with — the spec is
     * explicit that nothing inside a mini app is authenticated and that `selfAddr` proves nothing, so
     * a player can already claim to be anyone within the app; identity there is the app's business,
     * carried in its own payload. What IS gained beyond the signer: continuous movement telemetry is
     * no longer a stream of events tied to the reader's npub.
     *
     * THE TURN-BASED CHANNEL KEEPS THE REAL KEY. Kind 4932 is a durable, attributable move that
     * belongs to the account and is read back by everyone who opens the post later; only 20932 —
     * ephemeral, never stored, delivered to whoever is connected now — is unattributed.
     *
     * The relay has to know that: its publishing gate is by author and this key is in nobody's web of
     * trust, so kind 20932 is exempted alongside the other ephemeral transports (NIP-46 signer
     * traffic, call signaling) in nostr_relay/server.py. Without that exemption every packet comes
     * back "blocked: not in web of trust" and multiplayer dies quietly. */
    Session.prototype.rtKey = function(){
      if(this._rtSk) return this._rtSk;
      const NT = window.NostrTools;
      this._rtSk = NT.generateSecretKey();
      this.rtPk = NT.getPublicKey(this._rtSk);
      return this._rtSk;
    };

    /* Send one realtime packet: NEWEST WINS, and never a queue.
     *
     * A movement packet is worthless the moment a newer one exists, so when a send is already in
     * flight the pending one is REPLACED rather than queued. That is still right with a local key —
     * a slow relay or a busy tab must never build a backlog of stale positions — it is simply no
     * longer the thing standing between a remote-signer player and a playable game. */
    Session.prototype.rtSend = function(b64){
      this._rtNext = b64;
      if(this._rtBusy) return;
      this._rtBusy = true;
      const pump = async () => {
        while(this._rtNext && !this.dead){
          const payload = this._rtNext;
          this._rtNext = null;
          try{
            const NT = window.NostrTools;
            const ev = NT.finalizeEvent({ kind: KIND_REALTIME, content: payload,
                                          tags: [['i', this.app.uuid]],
                                          created_at: Math.floor(Date.now() / 1000) }, this.rtKey());
            Relay.publishFast(ev);
          }catch(_){ break; }        // stop this burst rather than spin; the game itself is fine
        }
        this._rtBusy = false;
      };
      pump();
    };

    Session.prototype.mount = async function(host){
      this.origin = await sandboxOrigin(this.app.uuid || this.app.sha || this.app.url);
      /* selfAddr / selfName. The spec lets a messenger put anything here and warns apps not to trust
       * them — nothing in the sandbox is signed, so any player can claim to be anyone WITHIN the app.
       * The npub is still the useful answer: it is what the app shows next to a move, and it matches
       * what every other player's client will show for the same person. A guest gets neither, which
       * is honest — there is no key to name. */
      try{
        const me = (PC.me && PC.me()) || null;
        this.self.addr = (me && (me.npub || me.pubkey)) || '';
        const prof = (me && me.pubkey && PC.profOf) ? (PC.profOf(me.pubkey) || {}) : {};
        this.self.name = String(prof.display_name || prof.name || '').slice(0, 60);
      }catch(_){}
      /* A GUEST STILL HAS TO BE SOMEBODY — to the app, not to the network. The spec asks for an
       * identifier "unique in this chat", and an empty string is not unique: Half-Life hashes
       * `selfAddr` into the fake IP it routes multiplayer packets by, so two signed-out players would
       * land on one address and neither would ever see the other. The realtime channel's own
       * ephemeral key is the honest answer — it is what this session actually signs with, it is
       * unique, and it lasts exactly as long as the game does. (Signing out no longer bars multiplayer
       * at all, now that movement is not signed by the account.) */
      if(!this.self.addr){ try{ this.rtKey(); this.self.addr = this.rtPk; }catch(_){} }

      const f = document.createElement('iframe');
      f.className = 'xdc-frame';
      /* NO `sandbox` ATTRIBUTE, and that is a decision rather than an omission.
       *
       * It was there as defence in depth — allow-scripts + allow-same-origin, with top-navigation
       * withheld. FIREFOX WILL NOT REGISTER A SERVICE WORKER IN A SANDBOXED FRAME, whatever flags are
       * set: `SecurityError: The operation is insecure`, which is fatal here because the worker is
       * what serves the app its files. Confirmed the hard way — it fails with Enhanced Tracking
       * Protection turned OFF, so it is the attribute and not the privacy setting.
       *
       * The security of this feature never rested on that attribute. It rests on the app being on a
       * DIFFERENT ORIGIN (xdc.<instance>), which is what keeps it away from the client's localStorage
       * and IndexedDB where the key and session live, and on it having no network: the worker refuses
       * every cross-origin request and the CSP on every response names no host. Both still hold.
       *
       * What is given up: with no sandbox attribute a frame may navigate the top-level page after a
       * user activation. That is a phishing surface, and it is the price of the feature working in
       * Firefox at all — noted in docs/WEBXDC.md rather than discovered later. */
      f.setAttribute('referrerpolicy', 'no-referrer');
      /* `pointer-lock` is what makes MOUSE-LOOK work, and leaving it out breaks every first-person
       * game in the gallery — Doom, Quake, the Half-Life port.
       *
       * Pointer lock is a permissions-policy feature, and a CROSS-ORIGIN frame has it disabled unless
       * the embedder delegates it here. The app runs on xdc.<instance> precisely so it is cross-origin
       * (that is the whole security model), so this is not an edge case — it is guaranteed. The
       * failure is loud in the console and silent on screen: the game asks on mousedown, the promise
       * rejects, and the player just cannot turn.
       *
       *   Uncaught (in promise) DOMException: The document is not focused.
       *     requestPointerLock ← _emscripten_request_pointerlock ← handlerFunc (mousedown)
       *
       * That message names the OTHER half, and it needs fixing too: a frame the user has not clicked
       * into is not the focused document, and pointer lock is refused for that reason as well — which
       * is why it can fail even where the policy is granted. See the focus handling below. */
      f.setAttribute('allow', 'autoplay; fullscreen; gamepad; pointer-lock');
      /* `__reset` is handled by the loader BEFORE it boots anything, and it is not passed on to the
       * app's own frame: the app must not be able to ask for it, and must not see it in its URL. */
      f.src = this.origin + '/__sandbox__/?__xdc=' + encodeURIComponent(this.token)
            + (this.reset ? '&__reset=1' : '');
      this.frame = f;

      this._onMsg = (ev) => {
        if(this.dead || !this.frame) return;
        if(ev.source !== this.frame.contentWindow) return;
        if(ev.origin !== this.origin) return;          // only ever this app's own sandbox
        const d = ev.data;
        if(!d || typeof d !== 'object' || d.jsonrpc !== '2.0') return;
        this.onRpc(d);
      };
      window.addEventListener('message', this._onMsg);
      host.appendChild(f);

      /* FOCUS THE FRAME, or pointer lock is refused with "The document is not focused" even with the
       * policy granted above. Opening a mini app from a card leaves focus on the CLIENT's document —
       * the app is on screen and filling it, but as far as the browser is concerned nobody has
       * clicked into it. The game then asks for the pointer on the very first mousedown and is turned
       * down, which reads as "the mouse does nothing".
       *
       * Once on load, and again on pointerdown: a WebView or a re-parented window (the desktop's
       * windowed mode re-appends the frame, which reloads it) can land focus back on the parent, and
       * the press that starts the game is the natural moment to take it. Both are best-effort —
       * `contentWindow.focus()` is permitted cross-origin, but a browser is free to ignore it, so
       * nothing here may throw into the mount path. */
      const _focusApp = () => { try{ f.focus(); }catch(_){}
                                try{ f.contentWindow && f.contentWindow.focus(); }catch(_){} };
      f.addEventListener('load', _focusApp);
      // On the PARENT, because a pointerdown inside a cross-origin frame is not visible to us — this
      // fires for the press that lands on the frame's own box before the app sees it.
      f.addEventListener('pointerdown', _focusApp, true);
      setTimeout(_focusApp, 250);
    };

    Session.prototype.reply = function(id, result, transfer){
      if(id === undefined) return;
      this.post({ jsonrpc:'2.0', id:id, result:result }, transfer);
    };
    Session.prototype.fail = function(id, message){
      if(id === undefined) return;
      this.post({ jsonrpc:'2.0', id:id, error:{ code:-32000, message:String(message || 'error') } });
    };

    Session.prototype.onRpc = function(d){
      const id = d.id;
      if(d.method === 'ready'){
        this.post({ jsonrpc:'2.0', method:'init', params:{ version:1 } });
        return;
      }
      if(d.method === 'webxdc.crash'){
        const p = d.params || {};
        const m = String(p.message || '').slice(0, 200);
        // Once per distinct message: a game that throws every frame must not become a toast storm.
        this._crashed = this._crashed || new Set();
        if(!this._crashed.has(m)){
          this._crashed.add(m);
          toast((this.app.name || 'the app') + ': ' + m + (p.where ? ' (' + p.where + ')' : ''));
          try{ console.warn('[webxdc]', this.app.name, m, p.where || ''); }catch(_){}
        }
        return;
      }
      /* WHAT THE PHONE MEASURED about the controller. There is no pad and no device on the machine
       * this is developed on, so "the controller does nothing" has to be split into its four very
       * different causes from the device itself: no pad visible to the frame, a pad visible but
       * never pressed (the gesture rule), keys sent that the game ignored, or the shim switched off.
       * Two rounds were spent guessing before this existed. PC.padStats() reads it. */
      if(d.method === 'webxdc.padstat'){
        this.padStat = d.params || {};
        return;
      }
      if(d.method === 'sandbox.error'){
        toast((d.params && d.params.message) || 'the sandbox failed to start');
        return;
      }
      if(d.method === 'fetch'){
        /* WHAT THE SANDBOX ACTUALLY ASKED FOR. "Black screen, no error" has two completely different
         * causes wearing one face: the app ran and drew nothing, or the app never started and its
         * injected bridge — which is what reports errors and probes WebGL — never executed, so of
         * course it said nothing. The parent is the one component guaranteed to be running, and it
         * sees every request. Served files mean the app is alive. */
        this._served = (this._served || 0) + 1;
        try{ console.debug('[webxdc]', this.app.name, 'serving', (this._served) + ':',
                           new URL(d.params.request.url).pathname); }catch(_){}
        let path = '/';
        try{ path = new URL(d.params.request.url).pathname; }catch(_){}
        /* THE BYTES ARE TRANSFERRED, NOT COPIED, and not base64. A published mini app can hold a
         * 75 MB archive (Half-Life ships three), and base64 turns that into a ~100 MB string that is
         * built, structured-cloned across two frames, and decoded again — hundreds of megabytes of
         * copying per file, which on a phone is the difference between a game that starts and a black
         * screen. An ArrayBuffer in the transfer list is a pointer move. Every entry is freshly
         * inflated per request, so giving the buffer away costs the parent nothing. */
        this.resolve(path).then((r) => {
          /* ONLY A BUFFER NOBODY ELSE HOLDS MAY BE TRANSFERRED. Transferring DETACHES it in this
           * realm — every byte of it, not the slice being sent — so handing over a view onto the
           * ARCHIVE would empty the archive itself, and every file served after that would be zero
           * bytes: an app that boots into nothing, permanently, with no error anywhere. zip.js does
           * return a private copy for both compression methods today (measured against the real
           * 178 MB Half-Life archive: all 21 entries own their buffer), but that is a property of a
           * file two directories away, and this is the line that depends on it. So it is checked
           * HERE, where the transfer happens, and a shared view is copied instead. */
          const v = (r.body && r.body.buffer) ? r.body : null;
          const buf = !v ? null
                    : (v.byteOffset === 0 && v.byteLength === v.buffer.byteLength) ? v.buffer
                    : v.slice().buffer;
          this.reply(id, {
            status: r.status,
            statusText: '',
            headers: {
              'content-type': r.contentType,
              'content-security-policy': CSP,
              'cache-control': 'no-store',
              'x-content-type-options': 'nosniff',
            },
            bytes: buf,
          }, buf ? [buf] : []);
        }, () => this.fail(id, 'could not read that file from the app'));
        return;
      }
      if(d.method === 'webxdc.sendUpdate'){
        this.sendUpdate((d.params || {}).update, (d.params || {}).descr)
          .then(() => this.reply(id, null), (e) => this.fail(id, (e && e.message) || 'could not send'));
        return;
      }
      /* ---- the realtime channel: ephemeral kind 20932 -------------------------------------------
       * Nostr's ephemeral range (20000-29999) is exactly this semantic — relays forward to current
       * subscribers and store nothing — so "only peers connected right now receive it" is the
       * transport's own behaviour rather than something enforced on top of it. `since` is now: a
       * relay that does keep a few is not going to replay a minute of somebody else's movement into
       * a game that just started. */
      if(d.method === 'webxdc.rtJoin'){
        if(!this.rtSub){
          // Mint the channel key BEFORE subscribing: it is what tells our own packets from everyone
          // else's, and a packet that arrives before it exists would be delivered back to the app.
          try{ this.rtKey(); }catch(_){}
          try{
            /* `since` IS BACKDATED, AND IT HAS TO BE. It was `now`, which reads as "only what
             * happens from here on" and is really "only what the OTHER PLAYER'S CLOCK calls from
             * here on": the relay compares `created_at` against it (`_match_one`), and a peer whose
             * clock is two seconds behind ours has EVERY packet dropped, for the whole session, with
             * an OK on their side and silence on ours. Measured against the live relay — a packet
             * stamped 3s early never arrives. Two browsers on one machine share a clock and hide it;
             * a phone and a laptop do not.
             *
             * Two minutes costs nothing to be wrong about: 20932 is ephemeral, so this relay stores
             * none of it and there is no backlog to replay — the window only decides how much clock
             * skew the channel survives. */
            this.rtSub = Relay.subscribe([{ kinds:[KIND_REALTIME], '#i':[this.app.uuid],
                                            since: Math.floor(Date.now() / 1000) - 120 }], {
              onEvent: (ev) => {
                if(this.dead || !ev || ev.kind !== KIND_REALTIME) return;
                // Not our own packets: the sender already has them, and an app that echoes its own
                // movement back into its state sees every player twice. Matched on the CHANNEL key
                // (see rtKey), which is what signs them — the account's key never touches this path.
                if(ev.pubkey && this.rtPk && ev.pubkey === this.rtPk) return;
                this.post({ jsonrpc:'2.0', method:'webxdc.realtime', params:{ b64: ev.content || '' } });
              },
            });
          }catch(_){}
        }
        this.reply(id, null);
        return;
      }
      if(d.method === 'webxdc.rtSend'){
        const b = String((d.params || {}).b64 || '');
        if(b.length > UPDATE_MAX * 2){ this.fail(id, 'realtime packet too large'); return; }
        this.rtSend(b);
        this.reply(id, null);          // best-effort by design: the spec guarantees no delivery
        return;
      }
      if(d.method === 'webxdc.rtLeave'){
        try{ if(this.rtSub) Relay.close(this.rtSub); }catch(_){}
        this.rtSub = null;
        this.reply(id, null);
        return;
      }
      if(d.method === 'webxdc.setUpdateListener'){
        const from = Number((d.params || {}).serial) || 0;
        if(this.listening){
          // A second call is undefined behaviour per the spec; replaying from the given serial is the
          // least surprising thing to do with it. It rewinds the delivered mark, which is the ONE
          // place that is allowed to go backwards — the app has just said it wants those again.
          this.delivered = Math.max(0, Math.min(from, this.ordered.length));
          this.deliver();
          this.reply(id, null);
          return;
        }
        this.start(from).then(() => this.reply(id, null), () => this.reply(id, null));
        return;
      }
    };

    // ---- opening ------------------------------------------------------------------------------------

    let _openSeq = 0;

    /* EVERY RUNNING APP, so pressing Play on one that is already open cannot start a second copy.
     *
     * A Session owns a relay REQ, a `message` listener on this window and an iframe. Mounting a new
     * one over an old one leaves ALL of that behind: the old subscription keeps streaming, and its
     * listener answers the sandbox too — so every update is delivered twice and the app plays two
     * moves for one. On the desktop it was invisible, because PCOS.openDoc focuses the existing
     * window and the second session was mounted into it after wiping the first one's frame out of
     * the DOM; on a phone it was a second full-screen sheet stacked on the first. Keyed the way the
     * window is keyed, so the two can never disagree about what "already open" means. */
    const _live = new Map();
    const _liveKey = (app) => String((app && (app.uuid || app.sha)) || (app && app.url) || '');

    /* Open an app. On the DESKTOP it gets a real window — movable, resizable, and able to sit beside
     * the timeline, which is what a game wants; everywhere else it is a full-screen sheet, because a
     * phone has no room for anything else. */
    async function open(app, opts){
      if(!app || !app.url){ toast('that post has no app in it'); return; }
      const reset = !!(opts && opts.reset);
      const key = _liveKey(app);
      const prev = _live.get(key);
      if(prev && !prev.dead){
        /* Already running. A RESET means "throw this away and start again", so it is the one case
         * that tears the old one down; anything else brings it forward, because restarting a game
         * somebody is in the middle of is the bug, not the fix. */
        if(!reset){
          try{ if(window.PCOS && PCOS.isOn && PCOS.isOn() && PCOS.focusDoc) PCOS.focusDoc('webxdc:' + key); }catch(_){}
          try{ if(prev.sheet && prev.sheet.parentElement) document.body.appendChild(prev.sheet); }catch(_){}
          return prev;
        }
        try{ prev.destroy(); }catch(_){}
      }
      _live.delete(key);
      const id = ++_openSeq;
      let files;
      try{
        if(reset){ await forget(app); toast('resetting ' + (app.name || 'the app') + '…'); }
        else toast('opening ' + (app.name || 'the app') + '…');
        files = await load(app);
      }catch(e){ toast((e && e.message) || 'could not open that app'); return; }
      if(id !== _openSeq) return;                      // superseded by another launch

      const name = app.name || (await manifestName(files)) || 'Mini app';
      const session = new Session(Object.assign({}, app, { name }), files);
      session.reset = reset;                           // the loader wipes the sandbox origin first
      session.key = key;
      _live.set(key, session);
      // Out of the registry the moment it dies, so a later Play mounts a fresh one rather than
      // "focusing" a corpse.
      session.onDestroy = () => { if(_live.get(key) === session) _live.delete(key); };

      const mountInto = (el) => {
        el.classList.add('xdc-host');
        session.mount(el).catch((e) => { toast((e && e.message) || 'could not start the sandbox'); });
      };

      /* If nothing has been requested a few seconds in, the app is not merely slow — the pipe never
       * opened. That is a different bug from a game that renders black, and it is the one nobody can
       * see, so it says so out loud rather than leaving a dark rectangle to be interpreted. */
      setTimeout(() => {
        if(session.dead) return;
        if(!session._served){
          toast('the sandbox never asked for a single file — the app frame did not start. Reload the '
              + 'page, and if it persists the service worker on ' + sandboxHostName() + ' is the suspect.');
        }
      // A reset wipes the sandbox origin and then navigates the loader again before any of this
      // starts, so the ordinary ceiling would accuse a reset that is working of having failed.
      }, reset ? 25000 : 9000);

      let osWin = null;
      try{ osWin = window.PCOS && PCOS.isOn && PCOS.isOn(); }catch(_){ osWin = false; }
      if(osWin){
        /* A WINDOW'S `render` TAKES NO ARGUMENTS. It is called on repaint and is expected to paint
         * into the shared #feed — that is what every other caller does (Music calls renderMusicApp,
         * which draws into the feed). Passing a callback that expected the window's body meant it
         * threw on `undefined` INSIDE os.js's `try{ w.render() }catch{}`, silently, and the iframe
         * was never created at all: a black window that never asked for a single file, which is
         * exactly how it was reported twice.
         *
         * So the game is mounted by hand into the window's own SLOT — a dedicated node that is not
         * the timeline — and `render` is a no-op, present only so the repaint path does not fall
         * through to switchView() and drag the feed in on top of the game. */
        /* noFeed: a game owns its window. Without it the window joins the shared-feed hand-off, so
         * clicking any OTHER window pulls the timeline out of this one and repaints it — and a
         * repaint around a live iframe blanks or restarts the game. Reported exactly that way. */
        const w = PCOS.openDoc('webxdc:' + key, name, '#i-gamepad', () => {}, true);
        const host = w && (w.slot || w.body);
        if(!host){ toast('could not open a window for that app'); return session; }
        host.classList.add('xdc-slot');
        host.innerHTML = '';
        mountInto(host);
        /* The window owns the session: closing it must stop the subscription and drop the frame, or
         * a closed game keeps a REQ open for the rest of the session — the exact cost the Notes
         * subscription audit was about. */
        if(w) w.onClose = () => session.destroy();
        return session;
      }

      const sheet = document.createElement('div');
      sheet.className = 'xdc-sheet';
      sheet.innerHTML = `<div class="xdc-bar">
          <span class="xdc-name">${enc(name)}</span>
          <button class="xdc-x" aria-label="Close">✕</button>
        </div><div class="xdc-body"></div>`;
      document.body.appendChild(sheet);
      session.sheet = sheet;                 // so destroy() takes the overlay with it — and so the
                                             // Android back button has something to close
      $('.xdc-x', sheet).onclick = () => session.destroy();
      mountInto($('.xdc-body', sheet));
      return session;
    }

    /* THE ANDROID BACK BUTTON. A full-screen mini app is an overlay like Notes' drawer and Web
     * Search's reader, and every one of those is registered in app.js's backButton chain. This one
     * was not, so Back navigated the view UNDERNEATH and left the game sitting on top of it — on the
     * one surface where Back is how people leave a game. */
    function sheetOpen(){
      for(const s of _live.values()) if(!s.dead && s.sheet && s.sheet.parentElement) return true;
      return false;
    }
    function closeSheet(){
      for(const s of Array.from(_live.values())){
        if(!s.dead && s.sheet && s.sheet.parentElement){ try{ s.destroy(); }catch(_){} return true; }
      }
      return false;
    }

    // ---- reading an app off an event -----------------------------------------------------------------

    /* Is this event carrying a mini app, and where? Both shapes from the NIP: an `imeta` tag on any
     * event (NIP-92), and a kind-1063 file-metadata event whose tags are flat. Returns null for
     * everything else, which is almost every event, so it is kept cheap. */
    function appOf(ev){
      if(!ev || !Array.isArray(ev.tags)) return null;
      if(ev.kind === 1063){
        const get = (n) => { const t = ev.tags.find(x => x[0] === n); return (t && t[1]) || ''; };
        if((get('m') || '').toLowerCase() !== MIME) return null;
        const url = get('url');
        if(!/^https?:\/\//i.test(url)) return null;
        /* `image` and `size` are Ditto's, and they are what make the gallery a gallery rather than a
         * list of names: every app announced this way carries real cover art. Read here so there is
         * one place that knows how an app is described, and ignored by the timeline card, which has
         * no room for a cover. A kind-1 imeta carries neither — those tiles fall back to the glyph. */
        const img = get('image');
        return { url, sha:get('x'), uuid:get('webxdc'), name:(get('alt') || '').replace(/^Webxdc app:\s*/i, ''),
                 image: /^https?:\/\//i.test(img) ? img : '', size: Number(get('size')) || 0 };
      }
      for(const t of ev.tags){
        if(t[0] !== 'imeta') continue;
        const f = {};
        for(let i = 1; i < t.length; i++){
          const s = String(t[i] || ''), sp = s.indexOf(' ');
          if(sp > 0) f[s.slice(0, sp)] = s.slice(sp + 1);
        }
        if((f.m || '').toLowerCase() !== MIME) continue;
        if(!/^https?:\/\//i.test(f.url || '')) continue;
        return { url:f.url, sha:f.x || '', uuid:f.webxdc || '', name:(f.summary || '').slice(0, 80) };
      }
      return null;
    }

    /* The card that appears in the timeline in place of a link to a zip file. Deliberately a
     * CARTRIDGE rather than an auto-running frame: an app is code somebody else wrote, and it starts
     * when the reader says so. */
    function cardHtml(app){
      const label = app.name || 'Mini app';
      return `<div class="xdc-card" data-xdc="${enc(JSON.stringify(app))}">
          <div class="xdc-ico"><svg class="ic" aria-hidden="true"><use href="#i-gamepad"></use></svg></div>
          <div class="xdc-meta">
            <b>${enc(label)}</b>
            <span>Mini app · runs in a sandbox with no network</span>
          </div>
          <button class="btn small xdc-reset" title="Throw away this app's downloaded copy and everything it has saved on this device, then start it fresh">Reset</button>
          <button class="btn btn-neon small xdc-play">Play</button>
        </div>`;
    }

    /* ===== THE DIRECTORY (Games → Webxdc) ========================================================
     *
     * A mini app reaches this client as an ATTACHMENT to somebody's post, which means the only way
     * to find one was to scroll past it. Apps that nobody happens to see are apps nobody can play,
     * and a game with no second player is not a game — so the whole point of the feature was being
     * lost to discovery. This is the directory: every app the network is carrying, as a cover-art
     * card you press to play.
     *
     * WHERE THEY COME FROM — three sources, because no single one can see them all:
     *
     *  1. kind 1063 with `#m application/x-webxdc`. This is Ditto's shape and it is the one that
     *     matters today: `m` is a single-letter tag, so relays index it and the question can be
     *     asked directly. It is also the RICH one — those events carry cover art, a byte size and a
     *     description, which is what makes a tile worth looking at.
     *  2. kind 1 with `#t webxdc`, which app.js now attaches to any post carrying an app
     *     (imetaTagsFor). `imeta` is multi-letter and therefore unindexable: without that hashtag a
     *     game posted from here could never be found by a query, only stumbled upon.
     *  3. THE LOCAL CACHE, scanned for imeta apps directly. This is the only source that can see a
     *     post made before (2) existed, and it costs nothing — the events are already in memory.
     *
     * ONE TILE PER IDENTIFIER, NOT PER FILE. Two people posting the same .xdc have the same sha and
     * different identifiers, and the identifier is what decides whose game you join — so they are
     * two rooms and belong on two tiles. Collapsing them by sha would silently drop everyone into
     * whichever room happened to sort first.
     *
     * PLAY COUNTS ARE THE SORT, and they come free: every move is a kind 4932 whose `i` tag is the
     * identifier, so one query orders the whole directory by what people are actually playing. An
     * app nobody has touched still appears — at the bottom, by recency, because a directory that
     * hides the new arrivals can never get any.
     */
    const GAL_LIMIT = 300;         // apps per source; the network carries ~10 today
    const GAL_UPDATES = 500;       // moves sampled for the play counts
    const GAL_NOTES = 2000;        // cached notes scanned for source (3) — the cache is smaller than this
    /* How long the list stays warm. Generous on purpose: a directory of posted apps changes about
     * once a week, and the cost of being stale is a tile arriving late, while the cost of being
     * eager is a relay round trip every time somebody clicks the window. Refresh is right there. */
    const GAL_TTL = 15 * 60 * 1000;
    let _gal = { apps: [], at: 0, loading: null };

    /* What makes two rows the same app. See the banner: the identifier, and only as a last resort
     * the file — an app with no identifier is a solo copy and cannot be merged with anything.
     *
     * THE FALLBACK USES `_isDigest`, for the reason `_cacheKey` already had to learn: `x` is only
     * SOMETIMES a sha256. The published Half-Life port carries the literal "hl", and two uuid-less
     * apps whose authors both wrote a label there would key alike — one tile, one app's URL under
     * the other's name, and the second app missing from the directory with nothing logged. A
     * non-digest `x` is not an identity, so the URL is. */
    const _galKey = a => a.uuid || ('sha:' + (_isDigest(a.sha) ? String(a.sha).toLowerCase() : a.url));

    function _galRecord(ev){
      const app = appOf(ev);
      if(!app) return null;
      return Object.assign({}, app, { evId: ev.id, pubkey: ev.pubkey, at: ev.created_at || 0,
                                      note: String(ev.content || '').trim().slice(0, 200),
                                      plays: 0, last: 0 });
    }

    /* Merge a row in. The BEST record wins per field rather than the newest whole record: the same
     * game can arrive as a bare kind-1 imeta from one person and as a fully described kind-1063 from
     * another, and taking either wholesale throws away half of what is known about it. */
    function _galMerge(into, rec){
      const key = _galKey(rec);
      const cur = into.get(key);
      if(!cur){ into.set(key, rec); return; }
      // DESCRIPTION fields fill in from whoever has them — these say what the app IS.
      if(!cur.image && rec.image) cur.image = rec.image;
      if(!cur.name && rec.name) cur.name = rec.name;
      if(!cur.size && rec.size) cur.size = rec.size;
      if(!cur.note && rec.note) cur.note = rec.note;
      /* IDENTITY MOVES AS ONE PIECE, and that is a security property rather than tidiness.
       *
       * The earliest post is the one to credit — that is where the app was published — but `url` and
       * `sha` are WHAT PLAY RUNS, and they belong to the post being credited. Merged field by field,
       * they would keep whatever the first-INSERTED record carried, and insertion order here is
       * cache-scan order, not post order. So a tile could read "by Alice", link to Alice's post, and
       * download and execute the bytes at Bob's URL under the same identifier — which is exactly the
       * move an attacker reposting a popular identifier would make. Credit and bytes are the same
       * decision, taken once, from one record. */
      if(rec.at && (!cur.at || rec.at < cur.at)){
        cur.at = rec.at; cur.evId = rec.evId; cur.pubkey = rec.pubkey;
        cur.url = rec.url; cur.sha = rec.sha;
      }
    }

    async function galLoad(force){
      if(!force && _gal.at && (Date.now() - _gal.at) < GAL_TTL) return _gal.apps;
      if(_gal.loading) return _gal.loading;
      _gal.loading = (async () => {
        const found = new Map();
        const soak = (evs) => { for(const ev of (evs || [])){ const r = _galRecord(ev); if(r) _galMerge(found, r); } };
        /* The cache FIRST and synchronously, so the grid can paint before the network answers —
         * with nothing on screen this view is indistinguishable from one that is broken. */
        try{
          const S = window.Store;
          if(S && S.query){
            soak(S.query([{ kinds:[1063], limit:GAL_LIMIT }]));
            soak(S.query([{ kinds:[1], limit:GAL_NOTES }]));
          }
        }catch(_){}
        /* EVERY KIND THE COMPOSER CAN ATTACH AN APP TO, not just kind 1. `imetaTagsFor` emits the
         * `t webxdc` hashtag for whatever it is building, and it builds polls (1068), NIP-22
         * comments (1111) and git issues (1621) as well as notes — so asking for kind 1 alone tags
         * those posts for discovery and then never looks for them. Following the empty state's own
         * instruction from the poll composer would have produced nothing, with no error anywhere. */
        let net = [];
        try{
          net = await Relay.query([{ kinds:[1063], '#m':[MIME], limit:GAL_LIMIT },
                                   { kinds:[1, 1068, 1111, 1621], '#t':['webxdc'], limit:GAL_LIMIT }], 9000) || [];
        }catch(_){}
        soak(net);
        // Keep what the network taught us: this is also the only path by which an app posted on
        // another client ever reaches the cache, so a later visit paints it instantly.
        try{ for(const ev of net) window.Store && Store.saveEvent(ev); }catch(_){}

        /* Moves → how busy each room is. A miss here is not a failure: the directory still lists
         * everything, just ordered by recency, so this is queried without blocking on it.
         *
         * ASKED FOR BY IDENTIFIER (`#i`), never as "the 500 newest updates". Unscoped, the answer is
         * whatever the network happened to be playing in that window — so a busy stranger's game
         * crowds out the counts for every app in this list, and the download grows with the network
         * rather than with the directory. Scoped, every move returned is one that belongs to a tile
         * on screen. Nothing to ask about if the directory is empty. */
        const ids = [...found.keys()].filter(k => k.indexOf('sha:') !== 0);
        if(ids.length){
          try{
            const ups = await Relay.query([{ kinds:[KIND_UPDATE], '#i': ids, limit:GAL_UPDATES }], 9000) || [];
            for(const ev of ups){
              const i = ((ev.tags || []).find(t => t[0] === 'i') || [])[1];
              if(!i) continue;
              const a = found.get(i);
              if(!a) continue;                     // a room whose app we have never seen: nothing to show
              a.plays++;
              if((ev.created_at || 0) > a.last) a.last = ev.created_at || 0;
            }
          }catch(_){}
        }

        const apps = [...found.values()].sort((a, b) => (b.plays - a.plays) || (b.last - a.last) || (b.at - a.at));
        /* AN EMPTY RESULT NEVER GOES WARM, and that is the whole guard.
         *
         * A failed load and an empty directory are the same answer here: `Relay.query` resolves `[]`
         * for a relay that said nothing AND for sockets that were still CONNECTING, which is the
         * normal state seconds after launch. Nothing can tell them apart from in here — so the empty
         * one is never treated as authoritative. Stamped, it would have repainted "No mini apps
         * found yet" on every entry and every desktop-window focus for a quarter of an hour without
         * asking again; the feature would look like the network carries nothing at all.
         *
         * The cost of being wrong the other way is one two-filter query per entry on a network that
         * genuinely has no apps. That is the cheap direction, and it is the same trade the uptime
         * doc and the folder-sync manifest make: never write on the strength of an empty read. */
        _gal = { apps, at: apps.length ? Date.now() : 0, loading: null };
        return apps;
      })();
      try{ return await _gal.loading; }finally{ _gal.loading = null; }
    }

    const _galSize = n => !n ? '' : (n >= 1048576 ? (n / 1048576).toFixed(n >= 10485760 ? 0 : 1) + ' MB'
                                                  : Math.max(1, Math.round(n / 1024)) + ' KB');

    /* The byline is a `.name[data-prof]`, and the CLASS is load-bearing — app.js's one
     * decorateProfiles pass keys on it, so a kind-0 that arrives after the grid painted fills the
     * name in place. With the attribute alone the tile would show a truncated pubkey for the whole
     * session, because nothing repaints this view on its own. Same shape as the article, stream and
     * market bylines.
     *
     * NEVER put a backtick in a comment inside the template literal below — it ends the string, and
     * the markup after it is then parsed as code. Cost one round of red tests to learn twice. */
    function galTile(a){
      const name = a.name || 'Mini app';
      const who = PC.profOf(a.pubkey) || {};
      const by = who.display_name || who.name || PC.safePk(a.pubkey);
      try{ PC.needProfile(a.pubkey); }catch(_){}
      const cover = a.image
        ? `<img src="${enc(a.image)}" alt="" loading="lazy" decoding="async" onerror="this.remove()">`
        : `<svg class="ic" aria-hidden="true"><use href="#i-gamepad"></use></svg>`;
      const foot = [a.plays ? (a.plays + (a.plays === 1 ? ' move' : ' moves')) : '', _galSize(a.size)]
                     .filter(Boolean).join(' · ');
      return `<div class="xdc-tile" data-key="${enc(_galKey(a))}">
          <div class="xdc-cover${a.image ? '' : ' xdc-cover-none'}">${cover}</div>
          <div class="xdc-tmeta">
            <b title="${enc(name)}">${enc(name)}</b>
            <span class="muted small">by <span class="name" data-prof="${enc(a.pubkey)}">${enc(by)}</span></span>
            ${foot ? `<span class="muted small xdc-tfoot">${enc(foot)}</span>` : ''}
          </div>
          <div class="xdc-tacts">
            <button class="btn btn-neon small xdc-tplay">Play</button>
            ${a.evId ? `<button class="btn btn-ghost small xdc-tpost" title="Open the post this app was shared in">Post</button>` : ''}
          </div>
        </div>`;
    }

    /* The view. Painted twice on purpose — once from the cache the moment it is entered, once when
     * the network answers — because the alternative is a spinner on a screen whose whole job is to
     * show that there is something to play. */
    /* ENTERING THIS VIEW IS NOT THE SAME AS REFRESHING IT.
     *
     * `renderView` runs on every entry AND every time a desktop window is focused, so a gallery that
     * queried on render would hit the relays each time you clicked its window — for a directory that
     * changes about once a week. The apps live in module state (like Web Search's results), so a
     * repaint is memory only; the network is asked when the list is COLD, and otherwise only by the
     * Refresh button. A running app is unaffected either way: its sheet hangs off document.body, not
     * #feed, and `open()` on an app that is already live brings it forward instead of reloading it. */
    async function gallery(){
      const feed = $('#feed');
      if(!feed) return;
      /* STILL OURS TO PAINT? — asked of the DOM, not of the router.
       *
       * `VIEW === 'xdc'` alone stranded a desktop window: park a gallery mid-load (click another
       * window), the query resolves with the view no longer current, the repaint is skipped, and
       * os.js restores the window without re-rendering it — so it sits on a spinner with a disabled
       * Refresh button for ever. The real question is whether the markup this call painted is still
       * on screen, which is true for a parked window and false for a view that was replaced. */
      const mine = () => document.body.contains(feed) && feed.querySelector('.xdc-gal-top');
      const paint = (apps, loading) => {
        if(window.__PC.VIEW !== 'xdc' && !mine()) return;   // replaced by another view mid-query
        const head = `<div class="xdc-gal-top">
            <div class="muted small">Mini apps — webxdc games, polls and shared editors people have posted. They run in a sandbox with no network of their own, and everyone who opens the same app is in the same game.</div>
            <button class="btn btn-ghost small" id="xdc-gal-refresh"${loading ? ' disabled' : ''}>${loading ? 'Looking…' : 'Refresh'}</button>
          </div>`;
        const body = apps.length
          ? `<div class="xdc-grid">${apps.map(galTile).join('')}</div>`
          : (loading ? '<div class="spinner"></div>'
                     : `<div class="empty">No mini apps found yet. Attach a <code>.xdc</code> to a post with <b>🎮 Mini app</b> in the composer and it will show up here.</div>`);
        feed.innerHTML = head + body;
        try{ PC.decorateProfiles && PC.decorateProfiles(); }catch(_){}
        const rf = $('#xdc-gal-refresh', feed);
        if(rf) rf.onclick = () => { _gal.at = 0; gallery(); };
      };
      const warm = _gal.at && (Date.now() - _gal.at) < GAL_TTL;
      paint(_gal.apps, !warm);
      if(warm) return;                                  // a focus/repaint costs nothing
      paint(await galLoad(false), false);
    }

    /* One delegated handler, for the same reason the timeline card has one: this grid is repainted
     * by two different passes and lives inside a desktop window as often as inside #feed. */
    document.addEventListener('click', (e) => {
      const tile = e.target.closest && e.target.closest('.xdc-tile');
      if(!tile) return;
      if(e.target.closest('[data-prof]')) return;      // the author's name is still a link to them
      e.preventDefault(); e.stopPropagation();
      const app = _gal.apps.find(a => _galKey(a) === tile.dataset.key);
      if(!app) return;
      if(e.target.closest('.xdc-tpost')){
        try{ PC.openThread && PC.openThread(app.evId); }catch(_){}
        return;
      }
      open(app);
    });

    /* ONE delegated handler for every card, ever, rather than a bind pass each render path has to
     * remember to call. The timeline, a thread, a profile, a DM bubble and the desktop's own windows
     * all paint notes through different code, and the last feature that needed a per-card binding
     * ended up with three copies and a surface where it silently did nothing. A click is a click. */
    document.addEventListener('click', (e) => {
      const card = e.target.closest && e.target.closest('.xdc-card');
      if(!card) return;
      if(e.target.closest('a')) return;                // a link inside the card is still a link
      e.preventDefault(); e.stopPropagation();
      let app = null;
      try{ app = JSON.parse(card.dataset.xdc || 'null'); }catch(_){}
      if(!app) return;
      /* RESET IS THE WAY OUT, and it exists because there was not one. A mini app keeps state in two
       * places a reader cannot see — the archive cached here, and whatever the app wrote on the
       * sandbox origin — and either going bad makes every launch fail identically for ever. The only
       * remedy was the browser's "clear browsing data", which also clears the instance this client is
       * signed in to. Asked first, because it does throw away saved games. */
      if(e.target.closest('.xdc-reset')){
        const go = PC.uiConfirm
          ? PC.uiConfirm('Reset ' + (app.name || 'this mini app') + '? Its downloaded copy and '
                       + 'anything mini apps have saved on this device are thrown away, then it '
                       + 'starts fresh. Use this when an app will not start.')
          : Promise.resolve(true);
        Promise.resolve(go).then((ok) => { if(ok) open(app, { reset:true }); });
        return;
      }
      open(app);
    }, true);

    /* ---- posting one ------------------------------------------------------------------------------
     *
     * Attach a `.xdc` to a post, which is all "publishing a game" is: the file goes to Blossom like
     * any other upload, and the post carries an `imeta` tag saying what it is plus a fresh identifier.
     *
     * THE IDENTIFIER IS MINTED HERE, PER POST, and that is the whole model. It — not the file, not
     * the event — is what makes two people the same game: everyone whose client sees this post plays
     * against each other, and posting the same file again starts a fresh game that shares nothing
     * with this one. It is what a "new game" button would do, and it costs one uuid.
     *
     * The archive is CHECKED before it is uploaded. A zip with no index.html is not a mini app, and
     * finding that out at upload time is a sentence; finding out when somebody presses Play is a
     * broken post that cannot be fixed without deleting it. */
    function attach(ta){
      const inp = document.createElement('input');
      inp.type = 'file';
      inp.accept = '.xdc,application/x-webxdc,application/zip';
      inp.style.display = 'none';
      document.body.appendChild(inp);
      inp.onchange = async () => {
        const file = inp.files && inp.files[0];
        inp.remove();
        if(!file) return;
        try{
          if(file.size > MAX_XDC) throw new Error('that file is too big for a mini app');
          const bytes = new Uint8Array(await file.arrayBuffer());
          let files;
          try{
            const index = new Map();
            for(const e of window.PCZip.entries(bytes)) if(e.name) index.set(e.name, e);
            files = { bytes, index };
          }catch(e){ throw new Error('that is not a .xdc archive (' + ((e && e.message) || 'unreadable') + ')'); }
          if(!files.index.has('index.html')) throw new Error('that .xdc has no index.html in it — it will not run');
          const name = (await manifestName(files)) || (file.name || '').replace(/\.xdc$/i, '');
          const sha = await sha256Hex(bytes);
          toast('uploading ' + (name || 'the app') + '…');
          /* Uploaded under its real name and type. A Blossom server keys on the hash, so the same
           * game posted twice is stored once — which also means the identifier below, not the URL,
           * has to be what separates two games. */
          const up = new File([bytes], (name || 'app').replace(/[^A-Za-z0-9._-]/g, '_') + '.xdc',
                              { type: MIME });
          const url = await PC.uploadBlob(up);
          if(!url) throw new Error('the upload did not come back with a URL');
          const uuid = (crypto.randomUUID ? crypto.randomUUID()
                                          : _hex(crypto.getRandomValues(new Uint8Array(16))));
          if(PC.mediaMeta) PC.mediaMeta(url, { m: MIME, x: sha, webxdc: uuid, summary: name || '' });
          // Into the post, where the composer's own imeta pass will pick the URL up.
          if(ta){
            const cur = String(ta.value || '');
            ta.value = (cur ? cur.replace(/\s*$/, '') + '\n' : '') + url + '\n';
            try{ ta.dispatchEvent(new Event('input', { bubbles:true })); }catch(_){}
            try{ ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }catch(_){}
          }
          toast(name ? ('attached ' + name) : 'mini app attached');
        }catch(e){ toast((e && e.message) || 'could not attach that app'); }
      };
      inp.click();
    }

    /* `Session` is exported for ONE reason: two of them, in one node process, against a stub relay,
     * is the only way to test that a packet one player sends is the packet the other player's app
     * receives. Every part of that path fails silently — a filter that matches nothing, a self-drop
     * that drops everybody, a base64 round trip that mangles high bytes — and none of it is visible
     * from either browser. See tests/test_webxdc.py::TwoPlayers. */
    window.PCWebxdc = { open, appOf, cardHtml, attach, sheetOpen, closeSheet,
                        // Games → Webxdc. `__gal` is the directory's own state, exposed so
                        // tests/test_webxdc_gallery.py can drive the merge/sort against real events.
                        gallery, __galLoad: galLoad, __galKey: _galKey, __galTile: galTile,
                        /* The controller diagnostic, and its off switch. `padStats()` answers the
                         * question a phone has to answer for us — see webxdc.padstat — and reads
                         * from the LIVE session, so it is asked while the game is open. `pad(false)`
                         * takes the shim off for an app that reads the pad itself; it applies on the
                         * next open, because the bridge is injected once per load. */
                        padStats: () => { for(const s of _live.values()) if(s && !s.dead && s.padStat) return s.padStat;
                                          return null; },
                        pad: (on) => { try{ localStorage.setItem('pc_xdc_pad', on === false ? '0' : '1'); }catch(e){}
                                       return on !== false; },
                        /* Right-stick look speed, px/frame at full deflection (default 45). Applies
                         * on the next open — the bridge is built once per load. */
                        look: (n) => { var v = parseFloat(n);
                                       if(!isFinite(v) || v <= 0 || v > 400) return 'give a number 1-400';
                                       try{ localStorage.setItem('pc_xdc_look', String(v)); }catch(e){}
                                       return 'look speed ' + v + ' — reopen the game'; },
                        /* Right-stick deadzone, 0-0.89 (default 0.15). Raise it if letting go of the
                         * stick kicks the view back the other way; lower it if small aim corrections
                         * do nothing. Applies on the next open. */
                        dead: (n) => { var v = parseFloat(n);
                                       if(!isFinite(v) || v < 0 || v >= 0.9) return 'give a number 0-0.89';
                                       try{ localStorage.setItem('pc_xdc_dead', String(v)); }catch(e){}
                                       return 'deadzone ' + v + ' — reopen the game'; },
                        MIME, KIND_UPDATE, Session };

    /* THE APK'S CONTROLLER, PATCHED IN FROM ANDROID.
     *
     * Measured: the same webxdc game, the same pad, the same tablet — works in Firefox, dead in the
     * app. The engine is the only variable, and a WebView embedded in someone else's Activity is not
     * on anyone's list of Gamepad API implementers. So GamepadPlugin reads the controller natively
     * and this forwards each snapshot into whichever mini app is open; the shim inside the app frame
     * prefers it over navigator.getGamepads(). On every other platform this listener finds no plugin
     * and nothing changes — a real pad keeps being read the ordinary way.
     *
     * Broadcast to every live session rather than to a tracked "current" one: only one game is on
     * screen at a time, a backgrounded session ignores it anyway (the shim releases on hidden), and
     * a stale idea of which is current is a controller that stops working after the second game. */
    try{
      const GP = PC.capPlugin && PC.capPlugin('Gamepad', 'status');
      if(GP && GP.addListener){
        GP.addListener('padstate', (st) => {
          for(const s of _live.values()){
            if(s && !s.dead) try{ s.post({ jsonrpc:'2.0', method:'webxdc.padstate', params:st }); }catch(e){}
          }
        });
        window.PCWebxdc.padNative = () => GP.status();   // what ANDROID measured, vs padStats()'s page view
      }
    }catch(e){}
  }
  init();
})();
