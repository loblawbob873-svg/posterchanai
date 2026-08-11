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
    const { $, enc, toast, publish, sign } = PC;
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
    async function sandboxOrigin(appId){
      const host = instanceHost();
      if(sandboxWildcard()) return 'https://' + (await subdomain(appId)) + '.' + host;
      return 'https://' + SANDBOX_LABEL + '.' + host;
    }

    // ---- reading the app ------------------------------------------------------------------------

    const _cache = new Map();          // sha/url -> Promise<Map(name → bytes)>

    async function sha256Hex(bytes){
      return _hex(await crypto.subtle.digest('SHA-256', bytes));
    }

    /* Fetch the .xdc and unzip it, once per app per session.
     *
     * The `x` tag is VERIFIED when present. The URL is somebody else's server and the file is code
     * we are about to run: without the check, whoever hosts it can swap the app after it was posted,
     * for one reader or for everybody, and nothing about the post would change. A mismatch is fatal
     * and says so — silently running it anyway would make the check decorative. */
    async function load(app){
      const key = app.sha || app.url;
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
        const CACHE = 'pc-webxdc-v1';
        let bytes = null;
        try{
          const c = await caches.open(CACHE);
          const hit = await c.match(app.url);
          if(hit) bytes = new Uint8Array(await hit.arrayBuffer());
        }catch(_){}
        if(bytes){
          if(!bytes.length) bytes = null;                  // a truncated entry is not a cache hit
          else return await _openArchive(bytes, app);
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
        if(/^[0-9a-f]{64}$/i.test(String(app.sha || ''))){
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
      this.sub = null;
      this.rtSub = null;            // the realtime channel, when an app joins one
      this._rtNext = null;          // the newest unsent realtime packet (newest wins)
      this._rtBusy = false;
      this.selfPk = '';            // to drop our own realtime packets
      this.seen = new Map();             // event id -> serial
      this.ordered = [];                 // events, oldest first
      this.listening = false;
      this.wantSerial = 0;
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

    /* Deliver everything past `serial`, oldest first, then keep delivering as events arrive.
     *
     * `max_serial` is the highest we hold, which is how an app knows it has caught up (the spec:
     * "when max_serial equals serial, this update is the last one"). Both are LOCAL numbers — see
     * the header — so they are assigned here, in (created_at, id) order, and stay stable for this
     * device because the ordering is total and the list only grows. */
    Session.prototype.deliverFrom = function(serial){
      const max = this.ordered.length;
      for(let i = serial; i < this.ordered.length; i++){
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
      // Total order, and STABLE: two updates in the same second are broken by id, so every device
      // that holds both puts them in the same order. Sorting the whole list rather than inserting is
      // fine — a game's history is hundreds of events, not millions.
      this.ordered.sort((a, b) => (a.created_at - b.created_at) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
      return true;
    };

    Session.prototype.start = async function(fromSerial){
      this.listening = true;
      this.wantSerial = Math.max(0, Number(fromSerial) || 0);
      const filter = { kinds:[KIND_UPDATE], '#i':[this.app.uuid], limit: 1000 };
      let evs = [];
      try{ evs = await Relay.query([filter]); }catch(_){ evs = []; }
      try{ (window.Store.query([filter]) || []).forEach(e => evs.push(e)); }catch(_){}
      this.absorb(evs);
      if(this.dead) return;
      this.deliverFrom(this.wantSerial);
      // Live from here. A game is two people taking turns; without this the second player's move
      // arrives only if something else happens to re-query.
      try{
        this.sub = Relay.subscribe([filter], { onEvent: (ev) => {
          if(this.dead) return;
          const before = this.ordered.length;
          if(this.absorb([ev])) this.deliverFrom(before);
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
        const before = this.ordered.length;
        if(this.absorb([ev])) this.deliverFrom(before);
      }
      return true;
    };

    // ---- mounting ---------------------------------------------------------------------------------

    /* Send one realtime packet: NEWEST WINS, and never a queue.
     *
     * A movement packet is worthless the moment a newer one exists, so when a send is already in
     * flight the pending one is REPLACED rather than queued. That single property is what makes this
     * safe on every signer: each packet costs a signature (measured at 1.77ms with a local key, so
     * ~560/sec on a desktop core — comfortably more than a shooter needs), while a REMOTE signer
     * (Amber, nsec.app) needs a round trip to another app per signature and can manage a handful a
     * second at best. With a queue that would grow without bound and wedge the signer; dropping
     * instead means a remote-signer player simply moves less smoothly, and still SEES everyone else
     * perfectly — receiving costs no signature at all. */
    Session.prototype.rtSend = function(b64){
      this._rtNext = b64;
      if(this._rtBusy) return;
      this._rtBusy = true;
      const pump = async () => {
        while(this._rtNext && !this.dead){
          const payload = this._rtNext;
          this._rtNext = null;
          try{
            const ev = await sign(KIND_REALTIME, payload, [['i', this.app.uuid]]);
            Relay.publishFast(ev);
          }catch(_){ break; }        // a signer that will not sign: stop this burst, keep the game
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
        this.selfPk = (me && me.pubkey) || '';
        const prof = (me && me.pubkey && PC.profOf) ? (PC.profOf(me.pubkey) || {}) : {};
        this.self.name = String(prof.display_name || prof.name || '').slice(0, 60);
      }catch(_){}

      const f = document.createElement('iframe');
      f.className = 'xdc-frame';
      /* allow-same-origin is REQUIRED — the spec has apps using localStorage and IndexedDB, which an
       * opaque origin cannot — and it is safe here only because the frame is on a different origin
       * to begin with. That is the whole reason for the wildcard subdomain; without it this attribute
       * would hand the app our storage. allow-top-navigation is deliberately absent: an app must not
       * be able to navigate the tab it is running in. */
      f.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox allow-downloads allow-pointer-lock');
      f.setAttribute('allow', 'autoplay; fullscreen; gamepad');
      f.setAttribute('referrerpolicy', 'no-referrer');
      f.src = this.origin + '/__sandbox__/';
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
      if(d.method === 'sandbox.error'){
        toast((d.params && d.params.message) || 'the sandbox failed to start');
        return;
      }
      if(d.method === 'fetch'){
        let path = '/';
        try{ path = new URL(d.params.request.url).pathname; }catch(_){}
        /* THE BYTES ARE TRANSFERRED, NOT COPIED, and not base64. A published mini app can hold a
         * 75 MB archive (Half-Life ships three), and base64 turns that into a ~100 MB string that is
         * built, structured-cloned across two frames, and decoded again — hundreds of megabytes of
         * copying per file, which on a phone is the difference between a game that starts and a black
         * screen. An ArrayBuffer in the transfer list is a pointer move. Every entry is freshly
         * inflated per request, so giving the buffer away costs the parent nothing. */
        this.resolve(path).then((r) => {
          const buf = (r.body && r.body.buffer) ? r.body.buffer : null;
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
          try{
            this.rtSub = Relay.subscribe([{ kinds:[KIND_REALTIME], '#i':[this.app.uuid],
                                            since: Math.floor(Date.now() / 1000) }], {
              onEvent: (ev) => {
                if(this.dead || !ev || ev.kind !== KIND_REALTIME) return;
                // Not our own packets: the sender already has them, and an app that echoes its own
                // movement back into its state sees every player twice.
                if(ev.pubkey && ev.pubkey === this.selfPk) return;
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
          // least surprising thing to do with it.
          this.deliverFrom(from);
          this.reply(id, null);
          return;
        }
        this.start(from).then(() => this.reply(id, null), () => this.reply(id, null));
        return;
      }
    };

    // ---- opening ------------------------------------------------------------------------------------

    let _openSeq = 0;

    /* Open an app. On the DESKTOP it gets a real window — movable, resizable, and able to sit beside
     * the timeline, which is what a game wants; everywhere else it is a full-screen sheet, because a
     * phone has no room for anything else. */
    async function open(app){
      if(!app || !app.url){ toast('that post has no app in it'); return; }
      const id = ++_openSeq;
      let files;
      try{
        toast('opening ' + (app.name || 'the app') + '…');
        files = await load(app);
      }catch(e){ toast((e && e.message) || 'could not open that app'); return; }
      if(id !== _openSeq) return;                      // superseded by another launch

      const name = app.name || (await manifestName(files)) || 'Mini app';
      const session = new Session(Object.assign({}, app, { name }), files);

      const mountInto = (el) => {
        el.classList.add('xdc-host');
        session.mount(el).catch((e) => { toast((e && e.message) || 'could not start the sandbox'); });
      };

      let osWin = null;
      try{ osWin = window.PCOS && PCOS.isOn && PCOS.isOn(); }catch(_){ osWin = false; }
      if(osWin){
        const w = PCOS.openDoc('webxdc:' + (session.app.uuid || session.app.sha || String(id)),
                               name, '#i-gamepad', (body) => {
          body.innerHTML = '';
          mountInto(body);
        });
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
      $('.xdc-x', sheet).onclick = () => { session.destroy(); sheet.remove(); };
      mountInto($('.xdc-body', sheet));
      return session;
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
        return { url, sha:get('x'), uuid:get('webxdc'), name:(get('alt') || '').replace(/^Webxdc app:\s*/i, '') };
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
          <button class="btn btn-neon small xdc-play">Play</button>
        </div>`;
    }

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
      if(app) open(app);
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

    window.PCWebxdc = { open, appOf, cardHtml, attach, MIME, KIND_UPDATE };
  }
  init();
})();
