/* #vms — Virtual Machines: PosterChan VM hosts and their machines, managed over Nostr.
 *
 * A host is a PosterChan SERVER node (Admin → VMs). This screen talks to it with signed, NIP-44
 * requests (vmrpc.js) and opens a VM's screen with noVNC (vmconsole.js); nothing here needs the
 * instance it was loaded from, so it works the same from the web, the APK and the desktop, and from a
 * bundle that has only a key and relays.
 *
 * Where the host list comes from, in order: this instance's own host (/client/config `vmhost`), the
 * user's `pcai:vmhosts` document (kind 30078, NIP-44 to themselves — pinned in store.js `_isPinned`
 * and carried in app.js `_CARRY_D`), and the local cache.
 *
 * FOUR RULES THIS FILE KEEPS, each one a bug this client has shipped before on another screen:
 *   1. CACHE FIRST. Hosts, their last host.info and their last VM list are kept per account in
 *      localStorage and painted IMMEDIATELY with "last answered Xs ago"; the network refreshes behind
 *      it. With nothing cached the first paint is a spinner, never an empty list.
 *   2. A QUERY ON ENTRY WAITS FOR A SOCKET — `Relay.ready()` before reading `pcai:vmhosts`.
 *   3. "NO ANSWER" IS NOT "NO VMs". A host that is offline and a host that drops strangers both answer
 *      nothing; that is said as such, with a retry, and the last known list stays on screen marked stale.
 *   4. The document is written only after a relay ANSWERED the read — an unreachable pool must not
 *      publish an empty host list over the real one (the replaceable-document wipe).
 * And never window.confirm/prompt: PC.uiConfirm / PC.uiPrompt only (native dialogs wedge Electron).
 *
 * PHASE 2 adds, each gated on what the HOST says it can do (whoami `features`), never on a guess:
 *   * VM Settings (vm.update) — the os.js editHardware form ported: Save is the LAST control, in a sticky
 *     footer, offered only when something changed, disabled during the round trip; leaving with edits asks.
 *   * Snapshots, the ISO library (fetch by URL with progress, upload with a ticket), Access (allowed npubs).
 *   * SESSION KEYS. With a REMOTE signer (NIP-46 bunker, Amber, an extension) every request is a trip to
 *     the signer, so polling opens a short session per host (a throwaway key the host accepts for "use"
 *     ops only) and signs reads/power/console with it. Anything that changes a VM or the host is still
 *     signed by the real key — the host refuses it from a session (`step_up_required`). A local nsec
 *     simply uses the real key for everything.
 *   * Host discovery: "Find hosts" reads 31310 announcements from the pool relays and asks each
 *     `host.whoami`; only hosts that ANSWER (you are on their list) are added.
 *   * "This computer": on the desktop app, `window.pcVM` (desktop/vm.js, qemu:///session) appears as the
 *     first host through LocalHost — the same client interface, SPICE instead of noVNC, no assignment,
 *     no snapshots, and ISOs chosen with the file picker instead of a library.
 */
(function(){
  const DOC_D = 'pcai:vmhosts';
  const POLL_MS = 10000;
  const S = {
    pk: '', hosts: [], data: {}, screen: 'hosts', host: '', vm: '', filter: 'all',
    doc: { read: false, ok: false, at: 0 }, poll: null, booted: '', console: null, create: null,
    busy: {}, mig: null,
    // the explorer: table sort, the name/IP filter, collapsed tree nodes
    sort: { key: 'name', dir: 1 }, q: '', collapsed: {},
    // phase 2
    settings: null, snaps: {}, iso: null, access: null, find: null,
    sessSuspect: {}, sessFail: {}, sessProbe: {},
  };
  const LOCAL_PK = 'local';
  const SESSION_OPS = new Set(['host.whoami', 'host.info', 'vm.list', 'vm.get', 'vm.power', 'console.ticket']);
  const SESSION_KIND = 27310;
  let PC = null;
  let rpc = null;

  const nowS = () => Math.floor(Date.now() / 1000);
  const esc = s => (PC && PC.enc) ? PC.enc(String(s == null ? '' : s))
    : String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const me = () => { try{ const m = PC && PC.me && PC.me(); return (m && m.pubkey) || ''; }catch(_){ return ''; } };
  const inView = () => { try{ return !!(PC && PC.isView && PC.isView('vms')); }catch(_){ return false; } };
  const npubOf = pk => { try{ return window.NostrTools.nip19.npubEncode(pk); }catch(_){ return String(pk || ''); } };
  const short = pk => { const n = npubOf(pk); return n.length > 20 ? n.slice(0, 12) + '…' + n.slice(-6) : n; };
  const ago = t => { if(!t) return 'never'; const s = Math.max(0, nowS() - Math.floor(t / 1000));
    return s < 60 ? s + 's ago' : s < 3600 ? (s / 60 | 0) + 'm ago' : s < 86400 ? (s / 3600 | 0) + 'h ago' : (s / 86400 | 0) + 'd ago'; };
  const toast = m => { try{ PC.toast(m); }catch(_){} };

  // ---------------------------------------------------------------- modules
  const _loads = {};
  function need(file, global){
    if(window[global]) return Promise.resolve(window[global]);
    if(!_loads[file]) _loads[file] = new Promise(res => {
      const el = document.createElement('script');
      el.src = '/static/js/client/' + file;
      el.onload = () => res(window[global] || null); el.onerror = () => { delete _loads[file]; res(null); };
      (document.head || document.documentElement).appendChild(el);
    });
    return _loads[file];
  }
  async function getRpc(){
    if(rpc) return rpc;
    const mod = await need('vmrpc.js', 'PCVmRpc');
    if(!mod || rpc) return rpc;
    rpc = mod.createVmRpc({
      WebSocket: window.WebSocket,
      sign: t => PC.signTemplate(t),
      enc: (pk, text) => PC.nip44enc(pk, text),
      dec: (pk, ct) => PC.nip44dec(pk, ct),
      me,
      verify: ev => { try{ return !window.NostrTools || window.NostrTools.verifyEvent(ev); }catch(_){ return false; } },
    });
    return rpc;
  }

  // ---------------------------------------------------------------- cache
  function loadCache(){
    S.hosts = []; S.data = {};
    try{
      const j = JSON.parse(localStorage.getItem('pc_vms:' + S.pk) || 'null');
      if(j && Array.isArray(j.hosts)) S.hosts = j.hosts.filter(h => h && /^[0-9a-f]{64}$/.test(h.pubkey || ''));
      if(j && j.data && typeof j.data === 'object') S.data = j.data;
      for(const k in S.data) if(S.data[k]) S.data[k].status = S.data[k].at ? 'stale' : 'loading';
    }catch(_){}
  }
  function saveCache(){
    if(!S.pk) return;
    try{
      const data = {};
      for(const k in S.data){ const d = S.data[k] || {}; data[k] = { whoami: d.whoami, info: d.info, vms: d.vms, at: d.at }; }
      localStorage.setItem('pc_vms:' + S.pk, JSON.stringify({ v: 1, hosts: S.hosts, data }));
    }catch(_){}
  }

  // ---------------------------------------------------------------- the host list
  function instanceHost(){
    try{
      const c = PC.clientConfig && PC.clientConfig();
      const v = c && c.vmhost;
      if(v && /^[0-9a-f]{64}$/.test(v.pubkey || '') && /^wss?:\/\//.test(v.relay || ''))
        return { pubkey: v.pubkey, relay: v.relay, name: v.name || 'This server', https: v.https || '', source: 'instance' };
    }catch(_){}
    return null;
  }
  function mergeHosts(list, source){
    let changed = false;
    for(const h of list || []){
      if(!h || !/^[0-9a-f]{64}$/.test(h.pubkey || '') || !/^wss?:\/\//.test(h.relay || '')) continue;
      const cur = S.hosts.find(x => x.pubkey === h.pubkey);
      if(cur){
        if(source === 'instance' && cur.source !== 'instance'){ Object.assign(cur, h, { source }); changed = true; }
        continue;
      }
      S.hosts.push({ pubkey: h.pubkey, relay: h.relay, name: String(h.name || '').slice(0, 80),
                     https: h.https || '', source: source || h.source || 'added' });
      changed = true;
    }
    return changed;
  }

  async function readDoc(){
    const pk = S.pk;
    if(!pk || S.doc.read) return;
    try{ if(window.Relay && window.Relay.ready) await window.Relay.ready(4000); }catch(_){}
    let evs = [];
    try{ evs = await PC.relayQuery([{ authors: [pk], kinds: [30078], '#d': [DOC_D], limit: 1 }], 6000) || []; }
    catch(_){ evs = []; }
    if(pk !== S.pk) return;
    const newest = evs.slice().sort((a, b) => b.created_at - a.created_at)[0];
    if(newest){
      try{
        const j = JSON.parse(await PC.nip44dec(pk, newest.content));
        if(pk !== S.pk) return;
        mergeHosts((j && j.hosts) || [], 'added');
        S.doc.ok = true; S.doc.at = newest.created_at;
      }catch(_){ /* a signer that would not decrypt: NOT ok — never write over it */ }
    }else if(evs.complete){
      S.doc.ok = true;             // every relay answered, and there is genuinely no document yet
    }
    S.doc.read = true;
    saveCache();
    paint();
    for(const h of S.hosts) if(!S.data[h.pubkey] || S.data[h.pubkey].status !== 'ok') refresh(h.pubkey);
  }

  async function writeDoc(){
    if(!S.doc.ok){ toast('your host list could not be read from your relays, so it was not saved there — it is kept on this device'); return false; }
    const pk = S.pk;
    const hosts = S.hosts.filter(h => h.source !== 'instance' && h.source !== 'local').map(h => ({ pubkey: h.pubkey, relay: h.relay, name: h.name || '' }));
    try{
      const ct = await PC.nip44enc(pk, JSON.stringify({ v: 1, hosts, updated: nowS() }));
      const r = await PC.publish(30078, ct, [['d', DOC_D]], { quiet: true, noQueue: true });
      if(!(r && r.ok)) throw new Error('refused');
      return true;
    }catch(_){ toast('couldn’t save your host list to your relays'); return false; }
  }

  // ---------------------------------------------------------------- talking to hosts
  const hostOf = pk => S.hosts.find(h => h.pubkey === pk);
  const dataOf = pk => (S.data[pk] = S.data[pk] || { status: 'loading' });

  async function rawCall(pk, op, args, opts){
    const r = await getRpc();
    const h = hostOf(pk);
    if(!r || !h) return { ok: false, noAnswer: true, reason: 'unavailable' };
    return r.call(h, op, args || {}, opts || {});
  }

  async function call(pk, op, args, opts){
    if(pk === LOCAL_PK) return LocalHost.call(op, args || {});
    if(SESSION_OPS.has(op) && wantsSession(pk)){
      const signer = await sessionSigner(pk);
      if(signer){
        const res = await rawCall(pk, op, args, Object.assign({}, opts || {}, { signer }));
        const code = res.error && res.error.code;
        if(code === 'session_expired' || code === 'step_up_required') dropSession(pk);
        else if(!res.noAnswer) return res;
        else{
          // Silence under a session is either an offline host or a host that no longer knows the key
          // (its state was wiped). Ask ONCE with the real key, at most every two minutes, so a lost
          // session cannot make a live host look dead for ever — without a signer prompt per poll.
          if(Date.now() - (S.sessProbe[pk] || 0) < 120000) return res;
          S.sessProbe[pk] = Date.now();
          const real = await rawCall(pk, op, args, opts);
          if(!real.noAnswer) dropSession(pk);
          return real;
        }
      }
    }
    return rawCall(pk, op, args, opts);
  }

  // ---------------------------------------------------------------- session keys
  const hexOf = b => Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
  const bytesOf = h => Uint8Array.from(String(h).match(/../g).map(x => parseInt(x, 16)));
  function wantsSession(pk){
    if(pk === LOCAL_PK) return false;
    try{ const m = PC.me && PC.me(); return !!(m && m.mode && m.mode !== 'local'); }catch(_){ return false; }
  }
  // Session SECRETS live in memory, for this page and this account only. localStorage is readable by any script on
  // the origin and outlives a sign-out; a reload costs one session.open (one signer prompt), which is the price.
  const SESS = new Map();                                            // account pk -> { host pk: { sk, exp } }
  function readSess(){ return SESS.get(S.pk) || {}; }
  function writeSess(all){ if(S.pk) SESS.set(S.pk, all); }
  function purgeStoredSessions(){                                    // what builds before this one persisted
    try{
      for(let i = localStorage.length - 1; i >= 0; i--){
        const k = localStorage.key(i);
        if(k && k.startsWith('pc_vms_sess:')) localStorage.removeItem(k);
      }
    }catch(_){}
  }
  function dropSession(pk){ const all = readSess(); if(all[pk]){ delete all[pk]; writeSess(all); } }
  function signerFrom(skHex){
    const NT = window.NostrTools;
    const sk = bytesOf(skHex);
    const ck = {};
    const conv = pk => (ck[pk] = ck[pk] || NT.nip44.v2.utils.getConversationKey(sk, pk));
    return { pubkey: NT.getPublicKey(sk), sign: async t => NT.finalizeEvent(t, sk),
             enc: async (pk, text) => NT.nip44.v2.encrypt(text, conv(pk)),
             dec: async (pk, ct) => NT.nip44.v2.decrypt(ct, conv(pk)) };
  }
  const _opening = {};
  async function sessionSigner(pk){
    const NT = window.NostrTools;
    if(!NT || !NT.nip44 || !NT.generateSecretKey) return null;
    const cur = readSess()[pk];
    if(cur && cur.exp > nowS() + 300 && /^[0-9a-f]{64}$/.test(cur.sk || '')) return signerFrom(cur.sk);
    if(Date.now() - (S.sessFail[pk] || 0) < 300000) return null;          // a host without sessions: stop asking
    if(_opening[pk]) return _opening[pk];
    _opening[pk] = (async () => {
      const me0 = S.pk;
      const sk = NT.generateSecretKey();
      const spk = NT.getPublicKey(sk);
      for(const hours of [11, 1]){
        const exp = nowS() + hours * 3600 - 60;
        const proof = NT.finalizeEvent({ kind: SESSION_KIND, created_at: nowS(), tags: [],
                                         content: 'posterchan-vmhost-session:' + me0 + ':' + exp + ':' + pk }, sk);
        const r = await rawCall(pk, 'session.open', { pk: spk, exp, scope: 'use', proof }, { retries: 0 });
        if(S.pk !== me0) return null;
        if(r.ok){ const all = readSess(); all[pk] = { sk: hexOf(sk), exp }; writeSess(all); return signerFrom(hexOf(sk)); }
        if(r.noAnswer || !(r.error && /at most/.test(r.error.message || ''))) break;
      }
      S.sessFail[pk] = Date.now();
      return null;
    })().finally(() => { delete _opening[pk]; });
    return _opening[pk];
  }

  // ---------------------------------------------------------------- "This computer" (desktop, window.pcVM)
  /* The same client interface as a server host, answered by desktop/vm.js through the preload bridge.
   * A VM's id here is its NAME (qemu:///session has no uuid-keyed metadata of ours); every call's result
   * is reshaped to what the server ops return, so the screens cannot tell the two apart except through
   * `features`. Local VMs live in THIS account's home folder, so the local user is their admin. */
  const LOCAL_FEATURES = { assign: false, migrate: false, snapshots: false, console: 'spice', iso: 'picker',
                           hardware: true, access: false, local: true };
  const hasLocal = () => { try{ return !!(window.pcVM && typeof window.pcVM.list === 'function'); }catch(_){ return false; } };
  const localState = st => { const x = String(st || '').toLowerCase();
    return /running|idle|blocked/.test(x) ? 'running' : /paused/.test(x) ? 'paused' : /shutdown/.test(x) && !/shut off/.test(x) ? 'stopping' : 'shutoff'; };
  const lerr = (r, fb) => ({ ok: false, error: { code: 'backend_error', message: (r && r.error) || fb } });
  function localView(m){
    return { uuid: m.name, name: m.name, state: localState(m.state), vcpus: m.cpus || 0,
             ram_mib: m.ramMiB != null ? m.ramMiB : Math.round((m.memoryKiB || 0) / 1024), disk_gib: 0, managed: true,
             guest: '', firmware: '', autostart: !!m.autostart, labels: [], assigned: [],
             missing_media: (m.missingMedia || [])[0] || '',
             net: m.nic && m.nic.type ? { type: m.nic.type, name: m.nic.source || '' } : undefined };
  }
  function localHardware(d){
    const cd = (d.disks || []).find(x => x.device === 'cdrom');
    const src = cd && cd.source && cd.source !== '-' ? cd.source : '';
    return { boot: d.bootOrder === 'cdrom' ? 'cdrom' : 'disk', input: d.gamingMouse ? 'mouse' : 'tablet',
             nics: d.networks || 0, disks: (d.disks || []).map(x => ({ device: x.device, target: x.target })),
             media: src ? src.split(/[\\/]/).pop() : '', media_path: src, cdrom: !!cd,
             net: d.nic && d.nic.type ? { type: d.nic.type, name: d.nic.source || '' } : undefined };
  }
  const LocalHost = {
    features: LOCAL_FEATURES,
    async call(op, a){
      const vm = window.pcVM;
      if(!vm) return { ok: false, noAnswer: true, reason: 'unavailable' };
      try{
        switch(op){
          case 'host.whoami':
            return { ok: true, result: { role: 'admin', local: true, host: { name: 'This computer', features: LOCAL_FEATURES } } };
          case 'host.info': {
            const r = await vm.list();
            if(!r || !r.available) return { ok: false, error: { code: 'unsupported', message: (r && r.error) || 'virtualization is unavailable on this computer' } };
            const ms = r.machines || [];
            return { ok: true, result: { name: 'This computer', kvm: true, libvirt: true,
                     vms: { running: ms.filter(m => localState(m.state) === 'running').length, total: ms.length } } };
          }
          case 'vm.list': {
            const r = await vm.list();
            if(!r || !r.available) return { ok: false, error: { code: 'unsupported', message: (r && r.error) || 'virtualization is unavailable on this computer' } };
            return { ok: true, result: { vms: (r.machines || []).map(localView), next: null } };
          }
          case 'vm.get': {
            const d = await vm.details(a.vm);
            if(!d || !d.ok) return lerr(d, 'could not read this VM');
            return { ok: true, result: { vm: Object.assign(localView(d), { hardware: localHardware(d) }) } };
          }
          case 'vm.power': {
            const map = { start: 'start', shutdown: 'shutdown', reboot: 'reboot', destroy: 'stop' };
            if(!map[a.action]) return { ok: false, error: { code: 'bad_request', message: 'unknown action' } };
            const r = await vm.action(a.vm, map[a.action]);
            if(!r || !r.ok) return lerr(r, 'the VM action failed');
            // Starting a graphical machine means showing it, as the desktop's old Local VMs screen did.
            if(a.action === 'start'){ await new Promise(res => setTimeout(res, 300)); const v = await vm.view(a.vm);
              if(!v || !v.ok) toast((v && v.error) || 'VM started, but its display could not open'); }
            const d = await vm.details(a.vm);
            return { ok: true, result: { vm: d && d.ok ? localView(d) : { uuid: a.vm, name: a.vm, state: a.action === 'start' ? 'running' : 'shutoff' }, action: a.action } };
          }
          case 'console.open': {
            const r = await vm.view(a.vm);
            return r && r.ok ? { ok: true, result: {} } : lerr(r, 'the display could not open');
          }
          case 'vm.boot_disk': {
            const r = await vm.bootDisk(a.vm);
            if(!r || !r.ok) return lerr(r, 'could not select the installed disk');
            const s2 = await vm.action(a.vm, 'start');
            if(!s2 || !s2.ok) return lerr(s2, 'the VM could not start');
            await new Promise(res => setTimeout(res, 300));
            const v = await vm.view(a.vm); if(!v || !v.ok) toast((v && v.error) || 'VM started, but its display could not open');
            return { ok: true, result: {} };
          }
          case 'vm.create': {
            if(!a.iso) return { ok: false, error: { code: 'bad_request', message: 'choose an installer ISO' } };
            const r = await vm.create({ name: a.name, iso: a.iso, guest: a.guest, firmware: a.firmware,
                                        ramMiB: a.ram_mib, cpus: a.vcpus, diskGiB: a.disk_gib,
                                        network: a.network || { type: 'user' } });
            if(!r || !r.ok) return lerr(r, 'VM creation failed');
            await new Promise(res => setTimeout(res, 300));
            const v = await vm.view(r.name); if(!v || !v.ok) toast((v && v.error) || 'VM created, but its display could not open');
            const d = await vm.details(r.name);
            return { ok: true, result: { vm: d && d.ok ? localView(d) : { uuid: r.name, name: r.name, state: 'running' } } };
          }
          case 'vm.update': {
            // desktop/vm.js has no single atomic update: each part is its own virsh edit, in the order
            // the desktop form always used, and the first failure stops the rest and is reported.
            let d = await vm.details(a.vm);
            if(!d || !d.ok) return lerr(d, 'could not read this VM');
            if(a.vcpus != null || a.ram_mib != null || a.autostart != null || a.boot != null){
              const r = await vm.update(a.vm, { ramMiB: a.ram_mib != null ? a.ram_mib : d.ramMiB, cpus: a.vcpus != null ? a.vcpus : d.cpus,
                                                autostart: a.autostart != null ? a.autostart : d.autostart,
                                                bootOrder: a.boot != null ? a.boot : undefined });
              if(!r || !r.ok) return lerr(r, 'could not save these settings');
            }
            if(a.add_disk_gib){ const r = await vm.addDisk(a.vm, a.add_disk_gib); if(!r || !r.ok) return lerr(r, 'could not add the disk'); }
            if(a.add_nic){ const r = await vm.addNetwork(a.vm); if(!r || !r.ok) return lerr(r, 'could not add the network adapter'); }
            if(a.media === 'eject'){ const r = await vm.ejectIso(a.vm); if(!r || !r.ok) return lerr(r, 'could not eject the installer'); }
            else if(a.media && a.media.path){ const r = await vm.changeIso(a.vm, a.media.path); if(!r || !r.ok) return lerr(r, 'could not change the installer'); }
            if(a.input){ const r = await vm.gamingMouse(a.vm, a.input === 'mouse'); if(!r || !r.ok) return lerr(r, 'could not change the pointer'); }
            if(a.network){
              if(typeof vm.setNetwork !== 'function') return { ok: false, error: { code: 'unsupported', message: 'this desktop app is too old to change a VM’s network' } };
              const r = await vm.setNetwork(a.vm, a.network); if(!r || !r.ok) return lerr(r, 'could not change the network');
            }
            d = await vm.details(a.vm);
            if(!d || !d.ok) return lerr(d, 'saved, but the VM could not be read back');
            return { ok: true, result: { vm: Object.assign(localView(d), { hardware: localHardware(d) }) } };
          }
          case 'vm.delete': {
            const r = await vm.remove(a.vm, !!a.delete_disks);
            return r && r.ok ? { ok: true, result: { deleted: a.vm } } : lerr(r, 'delete failed');
          }
          case 'iso.pick': {
            const p = await vm.pickIso();
            return { ok: true, result: { path: p || '' } };
          }
          case 'iso.list': return { ok: true, result: { isos: [], jobs: [] } };
          default:
            return { ok: false, error: { code: 'unsupported', message: 'this computer cannot do that' } };
        }
      }catch(e){ return { ok: false, error: { code: 'backend_error', message: String((e && e.message) || e) } }; }
    },
  };

  function featuresOf(pk){
    if(pk === LOCAL_PK) return LOCAL_FEATURES;
    const d = S.data[pk] || {};
    const f = (d.whoami && d.whoami.host && d.whoami.host.features) || [];
    const has = n => Array.isArray(f) && f.includes(n);
    return { assign: true, migrate: has('cold-migrate'), snapshots: has('snapshots'), console: 'novnc', iso: has('iso-fetch') ? 'library' : 'list',
             hardware: has('hardware'), access: has('access'), upload: (has('iso-fetch') || has('iso-upload')), local: false };
  }

  async function refresh(pk){
    const d = dataOf(pk);
    if(d.inflight) return d.inflight;
    d.inflight = (async () => {
      if(!d.at) d.status = 'loading';
      const who = await call(pk, 'host.whoami');
      if(who.noAnswer){ d.status = 'noanswer'; return; }
      if(!who.ok){ d.status = 'error'; d.err = who.error; return; }
      const [info, list] = await Promise.all([call(pk, 'host.info'), call(pk, 'vm.list', { limit: 50 })]);
      if(info.noAnswer || list.noAnswer){ d.status = 'noanswer'; return; }
      d.whoami = who.result;
      if(info.ok) d.info = info.result;
      if(list.ok) d.vms = list.result.vms || [];
      d.status = (info.ok && list.ok) ? 'ok' : 'error';
      d.err = !info.ok ? info.error : (!list.ok ? list.error : null);
      if(d.status === 'ok'){
        d.at = Date.now();
        const nm = d.whoami && d.whoami.host && d.whoami.host.name;
        const h = hostOf(pk); if(h && nm && !h.name) h.name = nm;
      }
      saveCache();
    })().finally(() => { d.inflight = null; if(S.pk && inView()) paint(); });
    return d.inflight;
  }

  function startPoll(){
    if(S.poll) return;
    S.poll = setInterval(() => {
      if(!inView()){ clearInterval(S.poll); S.poll = null; return; }
      if(document.hidden || S.console) return;
      if(S.screen === 'hosts' && S.hosts.length) S.hosts.forEach(h => refresh(h.pubkey));
      else if(S.host) refresh(S.host);
    }, POLL_MS);
  }

  // ---------------------------------------------------------------- actions
  async function addHost(){
    const who = await PC.uiPrompt('The VM host’s npub (its node key, shown in Admin → VMs on that server)',
                                  { placeholder: 'npub1…', ok: 'Next' });
    if(!who) return;
    let pk = null, hinted = '';
    try{
      const s = who.trim().replace(/^nostr:/, '');
      if(/^[0-9a-f]{64}$/i.test(s)) pk = s.toLowerCase();
      else{
        const dec = window.NostrTools.nip19.decode(s);
        if(dec.type === 'npub') pk = dec.data;
        else if(dec.type === 'nprofile'){ pk = dec.data.pubkey; hinted = (dec.data.relays || []).find(r => /^wss?:\/\//.test(r)) || ''; }
      }
    }catch(_){}
    if(!pk || !/^[0-9a-f]{64}$/.test(pk)){ toast('that is not an npub or nprofile'); return; }
    const relay = hinted || await PC.uiPrompt('The host’s relay', { placeholder: 'wss://example.com/relay', ok: 'Add host' });
    if(!relay) return;
    const url = relay.trim();
    if(!/^wss?:\/\/[^\s]+$/.test(url)){ toast('a relay address starts with wss://'); return; }
    mergeHosts([{ pubkey: pk, relay: url }], 'added');
    saveCache();
    S.host = pk; S.screen = 'host';
    paint();
    refresh(pk);
    writeDoc();
  }

  async function findHosts(){
    S.screen = 'find';
    S.find = { running: true, candidates: 0, checked: 0, added: [], silent: 0, msg: 'Reading host announcements from your relays…' };
    paint();
    const F = S.find;
    const me0 = S.pk;
    try{ if(window.Relay && window.Relay.ready) await window.Relay.ready(4000); }catch(_){}
    let evs = [];
    try{ evs = await PC.relayQuery([{ kinds: [31310], '#d': ['posterchan-vmhost'], limit: 100 }], 8000) || []; }catch(_){ evs = []; }
    if(S.pk !== me0 || S.find !== F) return;
    const newest = {};
    for(const ev of evs){
      if(!ev || !/^[0-9a-f]{64}$/.test(ev.pubkey || '')) continue;
      if(!newest[ev.pubkey] || newest[ev.pubkey].created_at < ev.created_at) newest[ev.pubkey] = ev;
    }
    const cands = [];
    for(const ev of Object.values(newest)){
      if(hostOf(ev.pubkey)) continue;
      let c = {}; try{ c = JSON.parse(ev.content || '{}') || {}; }catch(_){}
      const relays = [].concat(Array.isArray(c.relays) ? c.relays : [], (ev.tags || []).filter(t => t[0] === 'relay').map(t => t[1]))
        .filter(r => typeof r === 'string' && /^wss?:\/\/[^\s]+$/.test(r));
      if(relays.length) cands.push({ pubkey: ev.pubkey, relay: relays[0], name: String(c.name || '').slice(0, 80), https: typeof c.https === 'string' ? c.https : '' });
    }
    F.candidates = Math.min(cands.length, 20);
    F.msg = cands.length ? 'Asking ' + F.candidates + ' host' + (F.candidates === 1 ? '' : 's') + ' whether you are on their list…'
                         : (evs.complete === false ? 'Your relays did not answer — try again.' : 'No new VM hosts are announced on your relays.');
    paint();
    const r = await getRpc();
    await Promise.all(cands.slice(0, 20).map(async h => {
      const who = r ? await r.call(h, 'host.whoami', {}, { timeout: 6000, retries: 0 }) : { noAnswer: true };
      if(S.pk !== me0 || S.find !== F) return;
      F.checked++;
      if(who.ok && who.result && who.result.role){
        mergeHosts([{ pubkey: h.pubkey, relay: h.relay, name: h.name || (who.result.host && who.result.host.name) || '', https: h.https }], 'added');
        dataOf(h.pubkey).whoami = who.result;
        F.added.push({ pubkey: h.pubkey, name: h.name || (who.result.host && who.result.host.name) || 'VM host', role: who.result.role });
      }else F.silent++;
      paint();
    }));
    if(S.pk !== me0 || S.find !== F) return;
    F.running = false;
    F.msg = F.added.length ? 'Added ' + F.added.length + ' host' + (F.added.length === 1 ? '' : 's') + '.'
                           : (F.candidates ? 'None of the announced hosts has you on its list.' : F.msg);
    saveCache();
    if(F.added.length){ writeDoc(); F.added.forEach(a => refresh(a.pubkey)); }
    paint();
  }

  async function removeHost(pk){
    const h = hostOf(pk);
    if(!h || h.source === 'instance' || h.source === 'local') return;
    if(!await PC.uiConfirm('Remove ' + (h.name || short(pk)) + ' from your list? Its VMs are not touched.', { ok: 'Remove' })) return;
    S.hosts = S.hosts.filter(x => x.pubkey !== pk);
    delete S.data[pk];
    saveCache();
    S.screen = 'hosts'; S.host = '';
    paint();
    writeDoc();
  }

  async function power(pk, uuid, action){
    if(action === 'destroy' && !await PC.uiConfirm('Force off this VM? It is like pulling the plug — unsaved work inside it is lost.', { ok: 'Force off', danger: true })) return;
    const key = uuid + ':' + action;
    if(S.busy[key]) return;
    S.busy[key] = true; paint();
    try{
      const r = await call(pk, 'vm.power', { vm: uuid, action });
      if(r.noAnswer) toast('No answer from the host — it may be offline. Nothing was confirmed.');
      else if(!r.ok) toast(r.error.message || r.error.code);
      else{ upsertVm(pk, r.result.vm); toast({ start: 'Starting', shutdown: 'Shutting down', reboot: 'Rebooting', destroy: 'Forced off' }[action] || 'Done'); }
    }finally{
      delete S.busy[key];
      paint();
      setTimeout(() => refresh(pk), 1000);
      setTimeout(() => refresh(pk), 4000);
    }
  }

  function upsertVm(pk, vm){
    if(!vm) return;
    const d = dataOf(pk);
    d.vms = (d.vms || []).filter(v => v.uuid !== vm.uuid).concat([vm]);
    saveCache();
  }

  async function assign(pk, uuid){
    const who = await PC.uiPrompt('Assign this VM to (npub)', { placeholder: 'npub1…', ok: 'Assign' });
    if(!who) return;
    const r = await call(pk, 'vm.assign', { vm: uuid, pubkey: who.trim() });
    if(r.noAnswer) return toast('No answer from the host');
    if(!r.ok) return toast(r.error.message || r.error.code);
    upsertVm(pk, r.result.vm); paint(); toast('Assigned');
  }

  async function unassign(pk, uuid, target){
    if(!await PC.uiConfirm('Remove ' + short(target) + ' from this VM? Their open console closes.', { ok: 'Remove', danger: true })) return;
    const r = await call(pk, 'vm.unassign', { vm: uuid, pubkey: target });
    if(r.noAnswer) return toast('No answer from the host');
    if(!r.ok) return toast(r.error.message || r.error.code);
    upsertVm(pk, r.result.vm); paint();
  }

  async function del(pk, vm){
    const typed = await PC.uiPrompt('Type the VM’s name (' + vm.name + ') to delete it', { placeholder: vm.name, ok: 'Next' });
    if(typed == null) return;
    if(typed !== vm.name) return toast('the name did not match — nothing was deleted');
    const disks = await PC.uiConfirm('Also delete its disks? This cannot be undone.', { ok: 'Delete disks too', cancel: 'Keep the disks', danger: true });
    const r = await call(pk, 'vm.delete', { vm: vm.uuid, confirm_name: typed, delete_disks: !!disks });
    if(r.noAnswer) return toast('No answer from the host — the VM may or may not be deleted; refresh to see');
    if(!r.ok) return toast(r.error.message || r.error.code);
    const d = dataOf(pk); d.vms = (d.vms || []).filter(v => v.uuid !== vm.uuid); saveCache();
    S.screen = 'host'; S.vm = ''; paint(); toast('Deleted');
  }

  /* WHERE A VM'S NETWORK CARD PLUGS IN. A server host says which libvirt networks and Linux bridges it
   * has (host.info, admin) and refuses any other name; "This computer" lists the bridges System
   * Settings → Network made (pcNet.bridges), plus user-mode NAT, which needs no setup. A bridge puts
   * the VM on the LAN with its own address; NAT keeps it behind the host. The value is `type:name`. */
  const netValue = n => n && n.type ? n.type + ':' + (n.type === 'user' ? '' : (n.name || '')) : '';
  const parseNet = v => { const t = String(v || ''), i = t.indexOf(':'); if(i < 1) return null;
    const type = t.slice(0, i), name = t.slice(i + 1); return type === 'user' ? { type } : { type, name }; };
  const nm = x => typeof x === 'string' ? x : (x && x.name) || '';
  async function localBridges(){
    try{ if(window.pcNet && typeof window.pcNet.bridges === 'function'){ const r = await window.pcNet.bridges();
      return ((r && r.bridges) || []).filter(b => b && b.name); } }catch(_){}
    return [];
  }
  function netChoices(pk, bridges){
    if(pk === LOCAL_PK) return [{ v: 'user:', l: 'NAT — private network behind this computer (no setup)' }]
      .concat((bridges || []).map(b => ({ v: 'bridge:' + b.name, off: b.vmReady === false,
        l: 'Bridge ' + b.name + ' — on your LAN' + (b.vmReady === false ? ' (not set up for VMs — System Settings → Network)' : '') })));
    const info = (S.data[pk] || {}).info || {};
    const nets = (Array.isArray(info.networks) ? info.networks : []).map(nm).filter(Boolean);
    const brs = (Array.isArray(info.bridges) ? info.bridges : []).map(nm).filter(Boolean);
    return nets.map(n => ({ v: 'network:' + n, l: 'NAT network ' + n })).concat(brs.map(b => ({ v: 'bridge:' + b, l: 'Bridge ' + b + ' — on the host’s LAN' })));
  }
  function netDefault(pk){
    if(pk === LOCAL_PK) return 'user:';
    const info = (S.data[pk] || {}).info || {};
    return info.bridge ? 'bridge:' + info.bridge : 'network:' + (info.default_network || 'default');
  }
  function netSelect(choices, cur){
    if(!choices.length) return '';
    const has = choices.some(c => c.v === cur);
    return `<label>Network<select class="input" name="net">${has ? '' : `<option value="" selected>${esc(cur ? 'Keep ' + cur.replace(':', ' ') : 'Host default')}</option>`}${choices.map(c => `<option value="${esc(c.v)}" ${c.v === cur ? 'selected' : ''} ${c.off ? 'disabled' : ''}>${esc(c.l)}</option>`).join('')}</select></label>`;
  }

  async function openCreate(pk){
    S.screen = 'create'; S.create = { isos: null, msg: '', busy: false, localIso: '' };
    paint();
    if(pk === LOCAL_PK){ const C = S.create; C.isos = []; paint(); C.bridges = await localBridges(); if(S.create === C) paint(); return; }
    const r = await call(pk, 'iso.list');
    if(S.screen !== 'create' || !S.create) return;
    S.create.isos = r.ok ? (r.result.isos || []) : [];
    if(r.noAnswer) S.create.msg = 'No answer from the host — the ISO list could not be read.';
    paint();
  }

  async function submitCreate(pk){
    const f = document.querySelector('#vms-create');
    if(!f || !S.create || S.create.busy) return;
    const val = n => (f.querySelector('[name="' + n + '"]') || {}).value;
    const chk = n => !!(f.querySelector('[name="' + n + '"]') || {}).checked;
    const args = { name: val('name'), guest: val('guest'), firmware: val('firmware'),
                   vcpus: Number(val('vcpus')), ram_mib: Number(val('ram_mib')), disk_gib: Number(val('disk_gib')),
                   iso: pk === LOCAL_PK ? (S.create.localIso || '') : (val('iso') || ''), autostart: chk('autostart'), start: chk('start') };
    const net = parseNet(val('net'));
    if(net) args.network = net;
    S.create.busy = true; S.create.msg = 'Asking the host…'; paint();
    const r = await call(pk, 'vm.create', args, { timeout: 90000, onProgress: p => {
      if(S.create){ S.create.msg = p.msg || p.phase || 'Working…'; paintCreateMsg(); } } });
    if(!S.create) return;
    S.create.busy = false;
    if(r.noAnswer){ S.create.msg = 'No answer from the host — check the host before trying again (the same request is safe to repeat).'; paint(); return; }
    if(!r.ok){ S.create.msg = r.error.message || r.error.code; paint(); return; }
    upsertVm(pk, r.result.vm);
    S.create = null; S.screen = 'vm'; S.vm = r.result.vm.uuid;
    paint(); toast('VM created');
    refresh(pk);
  }

  // ---------------------------------------------------------------- VM settings (vm.update)
  const vmOf = (pk, uuid) => ((S.data[pk] || {}).vms || []).find(x => x.uuid === uuid);

  async function openSettings(pk, uuid){
    S.screen = 'settings';
    const st = S.settings = { pk, uuid, hw: null, vm: null, isos: null, busy: false, msg: '', clean: null, pick: '' };
    paint();
    const [g, l] = await Promise.all([call(pk, 'vm.get', { vm: uuid }), pk === LOCAL_PK ? Promise.resolve({ ok: true, result: { isos: [] } }) : call(pk, 'iso.list')]);
    if(S.settings !== st) return;
    if(!g.ok){ st.msg = g.noAnswer ? 'No answer from the host — the settings could not be read.' : (g.error.message || g.error.code); paint(); return; }
    st.vm = g.result.vm; st.hw = g.result.vm.hardware || {};
    upsertVm(pk, Object.assign({}, g.result.vm));
    st.isos = l.ok ? (l.result.isos || []) : [];
    if(pk === LOCAL_PK) st.bridges = await localBridges();
    if(S.settings !== st) return;
    paint();
  }

  /* WHAT SAVE WRITES, IN ONE PLACE — read from the form, compared with what the host last said. Only the
   * fields that differ are sent, so a Save never "changes" something to the value it already had. */
  function settingsFields(){
    const f = document.querySelector('#vms-settings');
    const st = S.settings;
    if(!f || !st || !st.vm) return null;
    const val = n => (f.querySelector('[name="' + n + '"]') || {}).value;
    const chk = n => !!(f.querySelector('[name="' + n + '"]') || {}).checked;
    return { vcpus: Number(val('vcpus')), ram_mib: Number(val('ram_mib')), autostart: chk('autostart'), boot: val('boot'),
             media: val('media') || '__keep', add_disk_gib: Number(val('add_disk_gib') || 0), add_nic: chk('add_nic'),
             input: val('input'), pick: st.pick || '', net: val('net') || '' };
  }
  function settingsDelta(x){
    const st = S.settings, v = st.vm, hw = st.hw || {};
    const out = {};
    if(x.vcpus && x.vcpus !== v.vcpus) out.vcpus = x.vcpus;
    if(x.ram_mib && x.ram_mib !== v.ram_mib) out.ram_mib = x.ram_mib;
    if(x.autostart !== !!v.autostart) out.autostart = x.autostart;
    if(x.boot && x.boot !== hw.boot) out.boot = x.boot;
    if(x.input && x.input !== hw.input) out.input = x.input;
    if(x.add_disk_gib > 0) out.add_disk_gib = x.add_disk_gib;
    if(x.add_nic) out.add_nic = true;
    if(x.net && x.net !== netValue(hw.net)){ const n = parseNet(x.net); if(n) out.network = n; }
    if(st.pk === LOCAL_PK){
      if(x.media === '__eject' && hw.media) out.media = 'eject';
      else if(x.pick) out.media = { path: x.pick };
    }else if(x.media === '__eject'){ if(hw.media) out.media = 'eject'; }
    else if(x.media !== '__keep' && x.media !== hw.media) out.media = { iso: x.media };
    return out;
  }
  function settingsDirty(){ const x = settingsFields(); return !!(x && Object.keys(settingsDelta(x)).length); }
  function syncSettingsSave(){
    const b = document.querySelector('#vms-settings [data-act="settings-save"]');
    if(b && S.settings) b.disabled = !!S.settings.busy || !settingsDirty();
  }

  async function saveSettings(){
    const st = S.settings;
    if(!st || st.busy) return;
    const x = settingsFields();
    const delta = x ? settingsDelta(x) : {};
    if(!Object.keys(delta).length) return;
    // Disabled for the whole round trip: `vm.update` redefines the domain, and two racing Saves are an
    // error people read as "saving is broken".
    st.busy = true; st.msg = 'Saving…'; paint();
    const r = await call(st.pk, 'vm.update', Object.assign({ vm: st.uuid }, delta), { timeout: 90000, retries: 0,
      onProgress: p => { if(S.settings === st){ st.msg = p.msg || 'Working…'; const m = document.querySelector('#vms-settings .vms-createmsg'); if(m) m.textContent = st.msg; } } });
    if(S.settings !== st) return;
    st.busy = false;
    if(r.noAnswer){ st.msg = 'No answer from the host — nothing was confirmed. Check the VM before saving again.'; paint(); return; }
    if(!r.ok){ st.msg = r.error.message || r.error.code; paint(); syncSettingsSave(); return; }
    st.vm = r.result.vm; st.hw = r.result.vm.hardware || st.hw; st.pick = '';
    upsertVm(st.pk, Object.assign({}, r.result.vm));
    st.msg = 'VM settings saved';
    const f = document.querySelector('#vms-settings'); if(f) f.reset();
    st.fresh = true;
    paint();
  }

  async function leaveSettings(){
    const st = S.settings;
    if(st && !st.busy && settingsDirty()){
      if(!await PC.uiConfirm('Leave without saving your changes to this VM?', { ok: 'Discard changes', danger: true })) return;
    }
    S.settings = null; S.screen = 'vm'; paint();
  }

  // ---------------------------------------------------------------- snapshots
  async function loadSnaps(pk, uuid){
    const k = pk + ':' + uuid;
    const cur = S.snaps[k] = Object.assign(S.snaps[k] || {}, { loading: true });
    paint();
    const r = await call(pk, 'vm.snapshot.list', { vm: uuid });
    cur.loading = false;
    if(r.ok){ cur.list = r.result.snapshots || []; cur.err = ''; }
    else cur.err = r.noAnswer ? 'No answer from the host' : (r.error.message || r.error.code);
    if(inView()) paint();
  }
  async function snapCreate(pk, uuid){
    const name = await PC.uiPrompt('Name the snapshot (letters, digits, . _ -)', { placeholder: 'before-upgrade', ok: 'Take snapshot' });
    if(!name) return;
    const k = pk + ':' + uuid; const cur = S.snaps[k] = S.snaps[k] || {};
    cur.busy = true; cur.err = 'Taking the snapshot…'; paint();
    const r = await call(pk, 'vm.snapshot.create', { vm: uuid, name: name.trim() }, { timeout: 300000, retries: 0 });
    cur.busy = false;
    if(r.ok){ cur.list = r.result.snapshots || []; cur.err = ''; toast('Snapshot taken'); }
    else cur.err = r.noAnswer ? 'No answer from the host — the snapshot may or may not exist; refresh to see' : (r.error.message || r.error.code);
    paint();
  }
  async function snapRevert(pk, uuid, name){
    if(!await PC.uiConfirm('Revert this VM to “' + name + '”? Everything that changed since that snapshot is lost.', { ok: 'Revert', danger: true })) return;
    const k = pk + ':' + uuid; const cur = S.snaps[k] = S.snaps[k] || {};
    cur.busy = true; cur.err = 'Reverting…'; paint();
    const r = await call(pk, 'vm.snapshot.revert', { vm: uuid, name, confirm: true }, { timeout: 300000, retries: 0 });
    cur.busy = false;
    if(r.ok){ cur.err = ''; upsertVm(pk, r.result.vm); toast('Reverted to ' + name); refresh(pk); }
    else cur.err = r.noAnswer ? 'No answer from the host — check the VM before trying again' : (r.error.message || r.error.code);
    paint();
  }
  async function snapDelete(pk, uuid, name){
    if(!await PC.uiConfirm('Delete the snapshot “' + name + '”? The VM itself is not changed.', { ok: 'Delete snapshot', danger: true })) return;
    const k = pk + ':' + uuid; const cur = S.snaps[k] = S.snaps[k] || {};
    cur.busy = true; paint();
    const r = await call(pk, 'vm.snapshot.delete', { vm: uuid, name }, { timeout: 300000, retries: 0 });
    cur.busy = false;
    if(r.ok){ cur.list = r.result.snapshots || []; cur.err = ''; }
    else cur.err = r.noAnswer ? 'No answer from the host' : (r.error.message || r.error.code);
    paint();
  }

  // ---------------------------------------------------------------- ISO library
  const mib = n => (Number(n || 0) / 1048576).toFixed(n > 10485760 ? 0 : 1) + ' MiB';
  async function openIsos(pk){
    S.screen = 'isos';
    const I = S.iso = S.iso && S.iso.pk === pk ? Object.assign(S.iso, { loading: true }) : { pk, list: null, jobs: [], msg: '', loading: true, busy: false, fetchEnabled: true };
    paint();
    const r = await call(pk, 'iso.list');
    if(S.iso !== I) return;
    I.loading = false;
    if(r.ok){ I.list = r.result.isos || []; I.jobs = r.result.jobs || []; I.fetchEnabled = r.result.fetch_enabled !== false; }
    else I.msg = r.noAnswer ? 'No answer from the host — the library could not be read.' : (r.error.message || r.error.code);
    paint();
  }
  async function isoFetch(pk){
    const I = S.iso;
    const url = await PC.uiPrompt('Download an installer ISO to this host from a URL (http/https, public addresses only)', { placeholder: 'https://cdimage.debian.org/…/debian.iso', ok: 'Download' });
    if(!url || S.iso !== I) return;
    I.busy = true; I.msg = 'Asking the host to download…'; paint();
    const r = await call(pk, 'iso.fetch', { url: url.trim() }, { retries: 0,
      onProgress: p => { if(S.iso === I && p && p.bytes != null){ I.msg = isoProgressText(p); paintIsoMsg(); } } });
    if(S.iso !== I) return;
    I.busy = false;
    if(!r.ok){ I.msg = r.noAnswer ? 'No answer from the host — the download may still have started; refresh the library.' : (r.error.message || r.error.code); paint(); return; }
    if(r.result.iso){ isoAdded(pk, I, r.result.iso); return; }          // a host from before background downloads
    const job = r.result.job || {};
    I.msg = 'Downloading on the host…'; paint();
    watchIsoJob(pk, I, job.id);
  }
  const isoProgressText = p => 'Downloading… ' + mib(p.bytes) + (p.total ? ' of ' + mib(p.total) + (p.pct != null ? ' (' + p.pct + '%)' : '') : '');
  function isoAdded(pk, I, iso){ I.msg = 'Added ' + iso.name + ' (sha256 ' + String(iso.sha256 || '').slice(0, 16) + '…)'; openIsos(pk); }
  // A download is a job on the HOST: this page only watches it (and may go away and come back — iso.list shows it).
  async function watchIsoJob(pk, I, id){
    let misses = 0;
    while(S.iso === I && id){
      await new Promise(res => setTimeout(res, 2000));
      if(S.iso !== I) return;
      const r = await call(pk, 'iso.fetch.status', { job: id }, { retries: 0 });
      if(S.iso !== I) return;
      if(!r.ok){
        if(r.noAnswer && ++misses < 30) continue;
        I.msg = r.noAnswer ? 'No answer from the host — refresh the library to see the download.' : (r.error.message || r.error.code);
        paint(); return;
      }
      misses = 0;
      const j = r.result.job || {};
      if(j.state === 'running'){ I.msg = isoProgressText(j); paintIsoMsg(); continue; }
      if(j.state === 'done' && j.iso){ isoAdded(pk, I, j.iso); return; }
      I.msg = j.state === 'cancelled' ? 'The download was cancelled.' : ('The download failed: ' + (j.error || 'unknown error'));
      paint(); return;
    }
  }
  async function isoCancel(pk, id){
    const I = S.iso;
    const r = await call(pk, 'iso.fetch.cancel', { job: id }, { retries: 0 });
    if(S.iso !== I) return;
    if(!r.ok){ toast(r.noAnswer ? 'No answer from the host' : (r.error.message || r.error.code)); return; }
    openIsos(pk);
  }
  function httpBase(h){
    if(h && /^https?:\/\//.test(h.https || '')) return h.https.replace(/\/+$/, '');
    try{ const u = new URL(h.relay); return (u.protocol === 'ws:' ? 'http:' : 'https:') + '//' + u.host; }catch(_){ return ''; }
  }
  async function isoUpload(pk, file){
    // ISOs go through BLOSSOM, not a per-host upload endpoint: the client uploads to the media store
    // it already uses (public, 5 GB blobs), and the host PULLS the blob from its OWN Blossom by sha256
    // (iso.fetch {blob}). That needs no inbound URL to the host — the reason the old ticket PUT stalled
    // for a host (e.g. nas) whose app is not publicly reachable.
    const I = S.iso;
    if(!file || !I) return;
    I.busy = true; I.msg = 'Uploading ' + file.name + ' to your media store…'; paint();
    let sha = '';
    try{
      const url = await PC.uploadBlob(file, { noCompress: true, folder: 'ISOs',   // never re-encode an ISO; stream (no full buffer)
        onProgress: e => { if(S.iso === I){ I.msg = 'Uploading ' + file.name + '… ' + mib(e.loaded) + (e.total ? ' of ' + mib(e.total) : ''); paintIsoMsg(); } } });
      const m = String(url || '').match(/([0-9a-f]{64})/i);
      sha = m ? m[1].toLowerCase() : '';
      if(!sha) throw new Error('the media store did not return a blob hash');
    }catch(e){
      if(S.iso !== I) return;
      I.busy = false; I.msg = 'Could not upload the ISO to your media store: ' + ((e && e.message) || e); paint(); return;
    }
    if(S.iso !== I) return;
    I.msg = 'Asking the host to import the ISO…'; paint();
    const r = await call(pk, 'iso.fetch', { blob: sha, name: file.name }, { retries: 0,
      onProgress: p => { if(S.iso === I && p && p.bytes != null){ I.msg = isoProgressText(p); paintIsoMsg(); } } });
    if(S.iso !== I) return;
    I.busy = false;
    if(!r.ok){ I.msg = r.noAnswer ? 'No answer from the host — it may still be importing; refresh the library.' : (r.error.message || r.error.code); paint(); return; }
    if(r.result.iso){ isoAdded(pk, I, r.result.iso); return; }
    const job = r.result.job || {};
    I.msg = 'Importing on the host…'; paint();
    watchIsoJob(pk, I, job.id);
  }
  async function isoDelete(pk, id){
    if(!await PC.uiConfirm('Delete ' + id + ' from this host’s ISO library?', { ok: 'Delete', danger: true })) return;
    const I = S.iso;
    const r = await call(pk, 'iso.delete', { iso: id }, { retries: 0 });
    if(S.iso !== I) return;
    if(r.ok){ I.msg = 'Deleted ' + id; openIsos(pk); return; }
    I.msg = r.noAnswer ? 'No answer from the host' : (r.error.message || r.error.code);
    paint();
  }
  function paintIsoMsg(){ const m = document.querySelector('.vms-isomsg'); if(m && S.iso) m.textContent = S.iso.msg || ''; }

  // ---------------------------------------------------------------- access (allowed npubs)
  async function openAccess(pk){
    S.screen = 'access';
    const A = S.access = { pk, allowed: null, admins: [], clean: '', busy: false, msg: '' };
    paint();
    const r = await call(pk, 'host.access.get');
    if(S.access !== A) return;
    if(!r.ok){ A.msg = r.noAnswer ? 'No answer from the host' : (r.error.message || r.error.code); paint(); return; }
    A.allowed = (r.result.allowed || []).map(x => x.pubkey);
    A.admins = (r.result.admins || []).map(x => x.pubkey);
    A.clean = JSON.stringify(A.allowed);
    paint();
  }
  async function accessAdd(){
    const A = S.access; if(!A || !A.allowed) return;
    const who = await PC.uiPrompt('Allow an npub to use this host (they see only VMs assigned to them)', { placeholder: 'npub1…', ok: 'Add' });
    if(!who || S.access !== A) return;
    let pk = null;
    try{ const s2 = who.trim(); pk = /^[0-9a-f]{64}$/i.test(s2) ? s2.toLowerCase() : (window.NostrTools.nip19.decode(s2).type === 'npub' ? window.NostrTools.nip19.decode(s2).data : null); }catch(_){}
    if(!pk){ toast('that is not an npub'); return; }
    if(!A.allowed.includes(pk)) A.allowed.push(pk);
    paint();
  }
  async function accessSave(){
    const A = S.access; if(!A || A.busy || !A.allowed) return;
    A.busy = true; A.msg = 'Saving…'; paint();
    const r = await call(A.pk, 'host.access.set', { allowed: A.allowed }, { retries: 0 });
    if(S.access !== A) return;
    A.busy = false;
    if(r.ok){ A.allowed = (r.result.allowed || []).map(x => x.pubkey); A.clean = JSON.stringify(A.allowed); A.msg = 'Saved'; }
    else A.msg = r.noAnswer ? 'No answer from the host — nothing was confirmed' : (r.error.message || r.error.code);
    paint();
  }

  // ---------------------------------------------------------------- console
  /* The console overlay, built in ONE place so the layout check (scripts/check_vm_console_layout.py)
   * measures the markup that ships. Its toolbar must never sit under a drag region: in a popped-out
   * PosterChanOS window the top 38px is #pc-oswin-chrome, which is `-webkit-app-region: drag`, and
   * Electron decides "drag the window" from the DRAG REGIONS, not from z-index — an element painted on
   * top of a drag region still loses its clicks to the window move unless it is itself `no-drag`. So a
   * console that merely out-z-indexed the title bar showed its ✕ Close exactly where a click moved the
   * window instead ("Close button at top does nothing"). The overlay therefore starts BELOW the title
   * bar in a popped-out window (the window keeps its own ✕/□/− too) and is `no-drag` throughout. */
  function consoleShell(name){
    const el = document.createElement('div');
    el.id = 'vms-console'; el.className = 'vmc';
    el.innerHTML = `<div class="vmc-bar">
        <button class="btn small" data-c="close" aria-label="Close console">✕ Close</button>
        <b class="vmc-name">${esc(name)}</b><span class="vmc-st">Requesting a console…</span>
        <span class="vmc-sp"></span>
        <button class="btn small" data-c="grab" disabled title="Keep the mouse inside the VM — Esc releases it">Grab mouse</button>
        <button class="btn small" data-c="cad">Ctrl+Alt+Del</button>
        <button class="btn small" data-c="fit">1:1</button>
        <button class="btn small" data-c="kbd">Keyboard</button>
        <button class="btn small" data-c="fs">Full screen</button>
      </div><div class="vmc-screen"></div><textarea class="vmc-kbd" autocapitalize="off" autocomplete="off" spellcheck="false" aria-label="On-screen keyboard"></textarea>`;
    return el;
  }
  async function openConsole(pk, vm){
    if(pk === LOCAL_PK){
      const r = await call(pk, 'console.open', { vm: vm.uuid });
      if(!r.ok) toast(r.error.message || r.error.code);
      return;
    }
    if(S.console) closeConsole();
    const h = hostOf(pk);
    const el = consoleShell(vm.name);
    document.body.appendChild(el);
    const st = el.querySelector('.vmc-st');
    const grabBtn = el.querySelector('[data-c=grab]');
    const setSt = (s, m) => {
      if(st) st.textContent = m || { connecting: 'Connecting…', connected: 'Connected', closed: 'Closed', error: 'Error' }[s] || s;
      el.dataset.state = s;
      const on = !!(C.handle && C.handle.grabbed && C.handle.grabbed());
      el.classList.toggle('vmc-on', on);
      if(grabBtn){ grabBtn.disabled = s !== 'connected'; grabBtn.textContent = on ? 'Mouse grabbed (Esc)' : 'Grab mouse'; }
    };
    const C = S.console = { el, handle: null, pk, uuid: vm.uuid, fit: true };
    el.querySelector('[data-c=close]').onclick = () => closeConsole();
    el.querySelector('[data-c=cad]').onclick = () => { C.handle && C.handle.ctrlAltDel(); };
    el.querySelector('[data-c=fit]').onclick = (e) => { C.fit = !C.fit; C.handle && C.handle.setFit(C.fit); e.currentTarget.textContent = C.fit ? '1:1' : 'Fit'; };
    el.querySelector('[data-c=fs]').onclick = () => { try{ document.fullscreenElement ? document.exitFullscreen() : el.requestFullscreen(); }catch(_){} };
    // The grab is ALSO a button: a click on the VM's screen takes it (vmconsole.js), but a control you
    // can see is how anybody finds out the console can keep the mouse at all.
    if(grabBtn) grabBtn.onclick = () => { C.handle && C.handle.grab && C.handle.grab(); };
    const kbd = el.querySelector('.vmc-kbd');
    el.querySelector('[data-c=kbd]').onclick = () => { try{ kbd.focus(); }catch(_){} };
    kbd.addEventListener('input', () => {
      const rfb = C.handle && C.handle.rfb; const v = kbd.value; kbd.value = '';
      if(!rfb) return;
      for(const ch of v){ const cp = ch.codePointAt(0); const ks = ch === '\n' ? 0xff0d : (cp < 256 ? cp : 0x01000000 + cp); try{ rfb.sendKey(ks); }catch(_){} }
    });
    kbd.addEventListener('keydown', e => {
      const map = { Backspace: 0xff08, Enter: 0xff0d, Tab: 0xff09, Escape: 0xff1b };
      const rfb = C.handle && C.handle.rfb;
      if(rfb && map[e.key]){ e.preventDefault(); try{ rfb.sendKey(map[e.key]); }catch(_){} }
    });
    const t = await call(pk, 'console.ticket', { vm: vm.uuid }, { retries: 0 });
    if(S.console !== C) return;
    if(t.noAnswer){ setSt('error', 'No answer from the host'); return; }
    if(!t.ok){ setSt('error', t.error.message || t.error.code); return; }
    const mod = await need('vmconsole.js', 'PCVmConsole');
    if(S.console !== C) return;
    if(!mod){ setSt('error', 'The console viewer could not be loaded'); return; }
    const url = mod.wsUrlFor(t.result.ws, h);
    C.handle = await mod.open({ target: el.querySelector('.vmc-screen'), url, ticket: t.result.ticket,
                                password: t.result.vnc_password, onStatus: setSt });
    if(S.console !== C && C.handle) C.handle.close();
  }

  function closeConsole(){
    const C = S.console;
    if(!C) return false;
    S.console = null;
    try{ C.handle && C.handle.close(); }catch(_){}
    try{ if(document.fullscreenElement) document.exitFullscreen(); }catch(_){}
    try{ C.el.remove(); }catch(_){}
    return true;
  }

  // ---------------------------------------------------------------- migration (phase 3)
  // A COLD migration to another host where this user is ALSO an admin: the source shuts the VM down,
  // the target pulls its disks over HTTPS and defines it. Two things only this screen can do right:
  //   * the authorization is signed HERE, encrypted to the TARGET (vmrpc.authorize) — the source only
  //     carries it, so the target checks for itself that this person is one of its admins;
  //   * progress arrives from BOTH hosts as 7310 events tagged to that authorization, and the status
  //     is also POLLED on both, because a dropped socket must not freeze the bar at 43% for ever.
  // A LOCKED migration (the hosts lost each other at the handoff) is shown with its split-brain risk
  // spelled out, and force_reclaim needs a confirm plus the VM's name typed.
  const MIG_FINAL = ['done', 'aborted', 'reclaimed', 'released'];
  const MIG_CANCELLABLE = ['planned', 'quiescing', 'exporting', 'transferring'];
  const fmtBytes = n => { n = Number(n) || 0; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
    while(n >= 1024 && i < u.length - 1){ n /= 1024; i++; } return (i ? n.toFixed(1) : String(n)) + ' ' + u[i]; };

  function readMigForm(){
    const M = S.mig, f = document.querySelector('#vms-mig');
    if(!M || !f) return;
    const sel = f.querySelector('[name=target]');
    const t = sel ? sel.value : M.target;
    if(t !== M.target){ M.target = t; M.pre = null; M.preErr = ''; }
    const sa = f.querySelector('[name=start_after]'), fo = f.querySelector('[name=force_shutdown]');
    if(sa) M.startAfter = !!sa.checked;
    if(fo) M.force = !!fo.checked;
  }

  async function openMigrate(pk, v){
    stopMigWatch();
    const M = S.mig = { vm: v.uuid, name: v.name, assigned: (v.assigned || []).slice(), target: '', startAfter: v.state === 'running', force: false,
                        pre: null, preErr: '', busy: false, id: '', authzId: '', src: null, dst: null, peers: null, msg: '' };
    S.screen = 'migrate';
    paint();
    for(const h of S.hosts) if(h.pubkey !== pk && !(S.data[h.pubkey] || {}).whoami) refresh(h.pubkey);
    const r = await call(pk, 'vm.migrate.status', { vm: v.uuid }, { retries: 0 });
    if(S.mig !== M) return;
    if(r.ok){
      M.peers = r.result.peers || [];
      const active = (r.result.migrations || []).find(m => m.role === 'source' && !MIG_FINAL.includes(m.state));
      if(active){ M.id = active.id; M.target = active.target; M.src = active; startMigWatch(pk); }
    }else M.msg = r.noAnswer ? 'No answer from the host.' : (r.error.message || r.error.code);
    paint();
  }

  async function migAuthz(pk, M){
    const rr = await getRpc();
    // `assigned` is what the TARGET will grant: it carries only the pubkeys this signature names, never the
    // source's own copy of the list (a source host could otherwise hand anybody access on the target).
    try{ return rr && await rr.authorize(M.target, 'vm.migrate.authorize', { source: pk, target: M.target, vm: M.vm, assigned: M.assigned || [] }); }
    catch(_){ return null; }
  }

  async function migPrecheck(pk){
    const M = S.mig;
    readMigForm();
    if(!M || M.busy) return;
    if(!M.target) return toast('choose the host to move it to');
    M.busy = true; M.pre = null; M.preErr = ''; M.msg = 'Asking both hosts…'; paint();
    const authz = await migAuthz(pk, M);
    let r;
    if(!authz) r = { ok: false, error: { code: 'signer', message: 'the authorization could not be signed' } };
    else r = await call(pk, 'vm.migrate.precheck', { vm: M.vm, target: M.target, authz, start_after: M.startAfter },
                        { timeout: 60000, retries: 0 });
    if(S.mig !== M) return;
    M.busy = false; M.msg = '';
    if(r.noAnswer) M.preErr = 'No answer from this host.';
    else if(!r.ok) M.preErr = r.error.message || r.error.code;
    else M.pre = r.result;
    paint();
  }

  async function migStart(pk){
    const M = S.mig;
    readMigForm();
    if(!M || M.busy || !M.pre) return;
    const th = hostOf(M.target);
    const tname = (th && th.name) || short(M.target);
    const size = M.pre.source && M.pre.source.total_bytes;
    const msg = 'Move ' + M.name + ' to ' + tname + '? It is shut down first' +
      (M.force ? ' (forced off if it does not stop in time)' : ' (the move is cancelled if it does not stop in time)') +
      ', its disks' + (size ? ' (' + fmtBytes(size) + ')' : '') + ' are copied, and it cannot be used until the copy is done.' +
      (M.startAfter ? ' It starts on ' + tname + ' when it arrives.' : '');
    if(!await PC.uiConfirm(msg, { ok: 'Migrate' })) return;
    if(S.mig !== M) return;
    M.busy = true; M.msg = 'Starting the migration…'; paint();
    const authz = await migAuthz(pk, M);
    const r = authz ? await call(pk, 'vm.migrate', { vm: M.vm, target: M.target, authz, start_after: M.startAfter,
                                                     force_shutdown: M.force }, { timeout: 90000 })
                    : { ok: false, error: { code: 'signer', message: 'the authorization could not be signed' } };
    if(S.mig !== M) return;
    M.busy = false;
    if(r.noAnswer){ M.msg = 'No answer from the host — open Migrate again to see whether it started.'; paint(); return; }
    if(!r.ok){ M.msg = r.error.message || r.error.code; paint(); return; }
    M.id = r.result.migration.id; M.authzId = authz.id; M.src = r.result.migration; M.msg = '';
    toast('Migration started');
    startMigWatch(pk);
    paint();
  }

  async function startMigWatch(pk){
    const M = S.mig;
    if(!M || !M.id) return;
    stopMigWatch();
    M.poll = setInterval(() => migPoll(pk), 3000);
    migPoll(pk);
    const rr = await getRpc();
    if(S.mig !== M || !rr || !M.authzId) return;
    const urls = [hostOf(pk), hostOf(M.target)].filter(Boolean).map(h => h.relay);
    const un = await rr.watch(urls, { kinds: [7310], authors: [pk, M.target], '#e': [M.authzId] }, async (ev) => {
      if(S.mig !== M || !ev || !ev.pubkey) return;
      try{
        if(window.NostrTools && !window.NostrTools.verifyEvent(ev)) return;
        const b = JSON.parse(await PC.nip44dec(ev.pubkey, ev.content));
        if(!b || b.id !== M.id || !b.progress) return;
        if(ev.pubkey === pk) M.src = Object.assign({}, M.src, b.progress);
        else if(ev.pubkey === M.target) M.dst = Object.assign({}, M.dst, b.progress);
        if(S.screen === 'migrate') paint();
      }catch(_){}
    });
    if(S.mig !== M){ try{ un(); }catch(_){} return; }
    M.unwatch = un;
  }

  function stopMigWatch(){
    const M = S.mig;
    if(!M) return;
    if(M.unwatch){ try{ M.unwatch(); }catch(_){} M.unwatch = null; }
    if(M.poll){ clearInterval(M.poll); M.poll = null; }
  }

  async function migPoll(pk){
    const M = S.mig;
    if(!M || !M.id || M.polling) return;
    if(!inView() || S.screen !== 'migrate'){ stopMigWatch(); return; }
    M.polling = true;
    try{
      const one = (host) => hostOf(host) ? call(host, 'vm.migrate.status', { migration: M.id }, { retries: 0, timeout: 8000 })
                                         : Promise.resolve({ ok: false, noAnswer: true });
      const [a, b] = await Promise.all([one(pk), M.target ? one(M.target) : Promise.resolve({ ok: false, noAnswer: true })]);
      if(S.mig !== M) return;
      const first = r => r.ok && r.result && (r.result.migrations || [])[0];
      if(first(a)) M.src = Object.assign({}, M.src, first(a));
      if(first(b)) M.dst = Object.assign({}, M.dst, first(b));
      const srcDone = M.src && MIG_FINAL.includes(M.src.state);
      const dstDone = !M.dst || MIG_FINAL.includes(M.dst.state) || (!b.ok && !b.noAnswer);
      if(srcDone && dstDone){ stopMigWatch(); refresh(pk); if(M.target && hostOf(M.target)) refresh(M.target); }
      if(S.screen === 'migrate') paint();
    }finally{ M.polling = false; }
  }

  async function migCancel(pk){
    const M = S.mig;
    if(!M || !M.id) return;
    if(!await PC.uiConfirm('Cancel moving ' + M.name + '? The copy stops, the other host throws away what it received, and the VM stays here (restarted if it was running).',
                           { ok: 'Cancel migration', cancel: 'Keep going', danger: true })) return;
    const r = await call(pk, 'vm.migrate.cancel', { migration: M.id }, { timeout: 30000, retries: 0 });
    if(S.mig !== M) return;
    if(r.noAnswer) toast('No answer from the host — nothing was confirmed');
    else if(!r.ok) toast(r.error.message || r.error.code);
    else{ M.src = Object.assign({}, M.src, r.result.migration); toast('Migration cancelled'); }
    paint();
    migPoll(pk);
  }

  async function migReclaim(pk, side){
    const M = S.mig;
    if(!M || !M.id || (side !== 'source' && side !== 'target')) return;
    const th = hostOf(M.target);
    const keeper = side === 'source' ? 'this host (' + ((hostOf(pk) || {}).name || short(pk)) + ')'
                                     : 'the target (' + ((th && th.name) || short(M.target)) + ')';
    if(!await PC.uiConfirm('SPLIT-BRAIN WARNING. You are deciding, WITHOUT the two hosts agreeing, that ' + keeper +
        ' keeps ' + M.name + '. If the other host also holds a working copy and it gets started, two machines with the same identity will run and their disks will diverge — whatever is written to one is missing from the other. Continue only if you know where the VM really is. Make the same choice on both hosts.',
        { ok: 'I understand the risk', danger: true })) return;
    const typed = await PC.uiPrompt('Type the VM’s name (' + M.name + ') to force this decision', { placeholder: M.name, ok: 'Force reclaim' });
    if(typed == null) return;
    if(typed !== M.name) return toast('the name did not match — nothing was changed');
    if(S.mig !== M) return;
    const args = { migration: M.id, side, confirm: 'split-brain' };
    const hosts = [pk].concat(M.target && th ? [M.target] : []);
    const res = await Promise.all(hosts.map(h => call(h, 'vm.migrate.force_reclaim', args, { timeout: 60000, retries: 0 })));
    const say = res.map((r, i) => (i === 0 ? 'This host: ' : 'Target: ') +
      (r.noAnswer ? 'no answer — try again when it is reachable' : r.ok ? r.result.migration.state : (r.error.message || r.error.code)));
    toast(say.join(' · '));
    if(S.mig !== M) return;
    migPoll(pk);
  }

  function migProgressHtml(pk, M){
    const src = M.src || {}, dst = M.dst || {};
    const th = hostOf(M.target);
    const tname = (th && th.name) || short(M.target);
    const total = dst.bytes_total || src.bytes_total || 0;
    const done = dst.bytes_done || 0;
    const pct = Math.max(0, Math.min(100, Math.round(dst.pct != null ? Number(dst.pct) : (total ? 100 * done / total : 0)) || 0));
    const side = (label, x) => `<div class="vms-mig-side"><b>${esc(label)}</b><span class="vms-pill">${esc(x.state || '…')}</span><span class="vms-seen">${esc(x.error || x.msg || '')}</span></div>`;
    let out = `<div class="vms-mig-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></div>
      <div class="vms-seen vms-mig-pct">${pct}%${total ? ' · ' + esc(fmtBytes(done)) + ' of ' + esc(fmtBytes(total)) : ''}</div>
      ${side('This host', src)}${side(tname, dst)}`;
    if(src.state === 'done' || (dst.state === 'done' && src.state !== 'reclaimed'))
      out += `<div class="vms-mig-done">Migrated — ${esc(M.name)} now lives on ${esc(tname)}.${src.retained ? ' This host keeps its old copy for a while.' : ''}</div>`;
    if(src.state === 'aborted')
      out += `<div class="vms-noanswer">The migration was stopped${src.error ? ': ' + esc(src.error) : ''}. The VM stays on this host.</div>`;
    if(src.state === 'reclaimed' || dst.state === 'released')
      out += `<div class="vms-noanswer">Force-reclaimed: the VM is kept on this host.</div>`;
    if(src.state === 'locked' || dst.state === 'locked')
      out += `<div class="vms-mig-locked"><b>The two hosts lost contact at the handoff.</b> Neither can tell whether the other still holds the VM, so both are locked and it cannot be started on either. If contact comes back this resolves itself. Otherwise decide which host KEEPS the VM — a forced decision risks split-brain: two running copies with diverging disks.
        <div class="vms-actions"><button class="btn btn-red small" data-act="mig-reclaim" data-side="source">Keep it on this host</button>
        <button class="btn btn-red small" data-act="mig-reclaim" data-side="target">Keep it on ${esc(tname)}</button></div></div>`;
    out += `<div class="vms-actions">${MIG_CANCELLABLE.includes(src.state) ? '<button class="btn" data-act="mig-cancel">Cancel migration</button>' : ''}
      <button class="btn small" data-act="mig-refresh">↻ Status</button></div>`;
    return `<div id="vms-migprog" class="vms-mig" data-src="${esc(src.state || '')}" data-dst="${esc(dst.state || '')}">${out}</div>`;
  }

  function migrateScreen(pk){
    const M = S.mig || {};
    const head = `<div class="vms-head"><button class="btn small vms-back" data-act="back">‹ ${esc(M.name || 'VM')}</button><h2>Migrate ${esc(M.name || '')}</h2></div>`;
    if(M.id) return head + migProgressHtml(pk, M);
    const cands = S.hosts.filter(h => h.pubkey !== pk && h.pubkey !== LOCAL_PK);   // "This computer" is not a VM host
    const opts = cands.map(h => {
      const role = roleOf(h.pubkey);
      const paired = !M.peers || M.peers.some(p => p.pubkey === h.pubkey);
      const why = role !== 'admin' ? (role ? ' — you are not an admin there' : ' — checking…') : (!paired ? ' — not paired with this host' : '');
      return `<option value="${esc(h.pubkey)}" ${role === 'admin' && paired ? '' : 'disabled'} ${M.target === h.pubkey ? 'selected' : ''}>${esc(h.name || short(h.pubkey))}${esc(why)}</option>`;
    }).join('');
    const pre = M.pre ? `<div class="vms-mig-pre">Ready: the target needs ${esc(M.pre.target.need_gib)} GiB and has ${esc(M.pre.target.free_gib)} GiB free · ${esc(M.pre.source.files)} file(s), ${esc(fmtBytes(M.pre.source.total_bytes))} to copy${M.pre.target.iso_available === false ? ' · its installer ISO is not on the target and will be detached' : ''}.</div>`
      : M.preErr ? `<div class="vms-noanswer">${esc(M.preErr)}</div>` : '';
    return head + `<form id="vms-mig" class="vms-form" onsubmit="return false">
      <div class="vms-seen">A cold migration: the VM is shut down, its disks are copied to the other host, and it is defined there with its assignments, snapshots and settings. You must be an admin of both hosts. VMs with a TPM (Windows) cannot be moved yet.</div>
      ${cands.length ? `<label>Move to<select class="input" name="target"><option value="">Choose a host…</option>${opts}</select></label>`
        : `<div class="empty">Add the other host to your list first — you must be its admin too.</div>`}
      <label class="vms-check"><input type="checkbox" name="start_after" ${M.startAfter ? 'checked' : ''}> Start it on the new host when it arrives</label>
      <label class="vms-check"><input type="checkbox" name="force_shutdown" ${M.force ? 'checked' : ''}> Force it off if it does not shut down in time</label>
      ${pre}
      <div class="vms-createmsg">${esc(M.msg || '')}</div>
      <div class="vms-actions"><button class="btn" data-act="mig-check" ${M.busy || !M.target ? 'disabled' : ''}>Check target</button>
        <button class="btn btn-neon" data-act="mig-start" ${M.busy || !M.pre ? 'disabled' : ''}>Migrate</button></div>
    </form>`;
  }

  // ---------------------------------------------------------------- painting
  /* THE SCREEN IS AN EXPLORER, the way Proxmox's is: a TREE on the left (All hosts → each host → its
   * VMs, "This computer" first on the desktop app), a SUMMARY + TOOLBAR + sortable DETAILS TABLE for
   * whatever is selected, and a VM's own page with its action toolbar on top. It used to be a column of
   * cards that showed a name, a state and one line of specs — nothing you could compare at a glance, and
   * nothing to say where a VM lived or what it was reachable at.
   *
   * Every number here is what the host ANSWERED (host.info / vm.list, cached); a column the host does
   * not report (an older host, or a field that is admin-only) shows "—", never a guess. Below the wide
   * breakpoint (a phone, or a desktop window too narrow for two panes) the same data is a list of cards
   * with the same details, because a nine-column table on a 390px screen is a horizontal scroll bar. */
  const ic = (id, cls) => `<svg class="ic${cls ? ' ' + cls : ''}" aria-hidden="true"><use href="#${id}"/></svg>`;
  function bar(label, used, total, committed){
    if(!total) return '';
    const pct = x => Math.max(0, Math.min(100, Math.round(100 * x / total)));
    return `<div class="vms-bar"><div class="vms-bar-l"><span>${esc(label)}</span><span>${esc(used)} / ${esc(total)}</span></div>
      <div class="vms-bar-t"><i class="vms-bar-used" style="width:${pct(used)}%"></i>${committed != null ? `<i class="vms-bar-com" style="width:${pct(committed)}%"></i>` : ''}</div></div>`;
  }
  const stateLabel = s => ({ running: 'Running', shutoff: 'Off', paused: 'Paused', stopping: 'Stopping', other: 'Unknown' }[s] || s);
  const roleOf = pk => { const d = S.data[pk]; return d && d.whoami && d.whoami.role; };
  const hostName = pk => { const h = hostOf(pk) || {}; const d = S.data[pk] || {};
    return h.name || (d.whoami && d.whoami.host && d.whoami.host.name) || 'VM host'; };
  const fmtMib = n => { n = Number(n) || 0; return n >= 1024 ? (n / 1024).toFixed(n % 1024 ? 1 : 0) + ' GiB' : n + ' MiB'; };
  const fmtDur = s => { s = Number(s); if(!isFinite(s) || s < 0) return '—';
    const d = Math.floor(s / 86400), h = Math.floor(s / 3600) % 24, m = Math.floor(s / 60) % 60;
    return d ? d + 'd ' + h + 'h' : h ? h + 'h ' + m + 'm' : m ? m + 'm' : Math.floor(s) + 's'; };
  // The primary NIC as the host described it: `net` ({type,name}) from vm.list, or the local bridge's nic.
  const netOf = v => { const n = v && v.net; if(!n || !n.type) return '';
    return n.type === 'bridge' ? 'Bridge ' + (n.name || '') : n.type === 'user' ? 'NAT (user)' : 'NAT ' + (n.name || ''); };
  const ipsOf = v => (v && Array.isArray(v.ips) ? v.ips : []).filter(x => typeof x === 'string');
  const upOf = v => v && v.state === 'running' && v.uptime_s != null ? fmtDur(v.uptime_s) : '—';

  function statusLine(pk){
    const d = S.data[pk] || {};
    if(d.status === 'noanswer') return `<div class="vms-noanswer">No answer — the host is offline or you’re not on its list.${d.at ? ' Showing what it said ' + ago(d.at) + '.' : ''} <button class="btn small" data-act="retry" data-host="${esc(pk)}">Retry</button></div>`;
    if(d.status === 'error') return `<div class="vms-noanswer">The host answered with an error: ${esc((d.err && (d.err.message || d.err.code)) || 'unknown')} <button class="btn small" data-act="retry" data-host="${esc(pk)}">Retry</button></div>`;
    if(d.status === 'loading' && !d.at) return `<div class="vms-seen"><span class="spinner spinner-inline"></span>Asking the host…</div>`;
    return `<div class="vms-seen">${d.status === 'stale' || d.inflight ? '<span class="spinner spinner-inline"></span>' : ''}Last answered ${esc(ago(d.at))}</div>`;
  }
  const dotOf = pk => { const d = S.data[pk] || {}; return d.status === 'ok' ? 'ok' : d.status === 'noanswer' || d.status === 'error' ? 'bad' : 'wait'; };

  // A host as a CARD — the phone's host list and "Find hosts" results.
  function hostCard(h){
    const d = S.data[h.pubkey] || {};
    const info = d.info || {};
    const role = roleOf(h.pubkey);
    const vms = info.vms ? `${info.vms.running}/${info.vms.total} running` : '';
    const caps = info.cpu ? `<div class="vms-host-cap">${esc(info.cpu.cores)} cores · load ${esc(info.cpu.load1)}</div>
      ${bar('RAM MiB', (info.ram.total_mib - info.ram.free_mib), info.ram.total_mib, info.ram.committed_mib)}
      ${bar('Disk GiB', (info.disk.total_gib - info.disk.free_gib), info.disk.total_gib, info.disk.committed_gib)}` : '';
    return `<button class="vms-host${S.host === h.pubkey ? ' sel' : ''}" data-host="${esc(h.pubkey)}">
      <div class="vms-host-top"><span class="vms-dot vms-dot-${dotOf(h.pubkey)}"></span><b>${esc(hostName(h.pubkey))}</b>
      ${role ? `<span class="vms-role vms-role-${esc(role)}">${esc(role)}</span>` : ''}</div>
      <div class="vms-host-sub">${h.source === 'local' ? 'VMs in this account on this computer' : esc(short(h.pubkey)) + (h.source === 'instance' ? ' · this server' : '')}</div>
      ${caps}${vms ? `<div class="vms-host-cap">${esc(vms)}</div>` : ''}
      ${statusLine(h.pubkey).replace(/<button[^>]*>Retry<\/button>/, '')}
    </button>`;
  }

  function hostsScreen(){
    if(!S.hosts.length){
      if(!S.doc.read) return `<div class="vms-cold"><div class="spinner"></div><div class="vms-seen">Looking for your VM hosts…</div></div>`;
      return `<div class="empty vms-empty">No VM hosts yet.<br><small>A host is a PosterChan server with VM hosting turned on (Admin → VMs).</small><br><button class="btn btn-ghost" data-act="add">Add a host</button> <button class="btn btn-ghost" data-act="find">Find hosts</button></div>`;
    }
    return `<div class="vms-hosts">${S.hosts.map(hostCard).join('')}</div>
      <div class="vms-actions"><button class="btn btn-ghost small" data-act="add">+ Add host</button><button class="btn btn-ghost small" data-act="find">Find hosts</button></div>`;
  }

  // ---- the tree (wide screens): All hosts → host → VM
  function tree(){
    const total = S.hosts.reduce((a, h) => a + (((S.data[h.pubkey] || {}).vms) || []).length, 0);
    const running = S.hosts.reduce((a, h) => a + (((S.data[h.pubkey] || {}).vms) || []).filter(v => v.state === 'running').length, 0);
    const rows = S.hosts.map(h => {
      const d = S.data[h.pubkey] || {};
      const open = !S.collapsed[h.pubkey];
      const vms = (d.vms || []).slice().sort((a, b) => a.name.localeCompare(b.name));
      const hostSel = S.host === h.pubkey && S.screen !== 'vm' && S.screen !== 'all';
      return `<div class="vmx-node">
        <div class="vmx-hostrow"><button class="vmx-twisty" data-twisty="${esc(h.pubkey)}" aria-label="${open ? 'Collapse' : 'Expand'}" aria-expanded="${open}"><i class="vmx-chev${open ? ' open' : ''}"></i></button>
        <button class="vms-host vmx-treehost${hostSel ? ' sel' : ''}" data-host="${esc(h.pubkey)}" title="${esc(h.source === 'local' ? 'VMs in this account on this computer' : short(h.pubkey))}">
          <span class="vms-dot vms-dot-${dotOf(h.pubkey)}"></span>${ic(h.source === 'local' ? 'i-monitor' : 'i-drive')}<b>${esc(hostName(h.pubkey))}</b>
          <span class="vmx-count">${d.vms ? (d.vms.filter(v => v.state === 'running').length + '/' + d.vms.length) : ''}</span></button></div>
        ${open && vms.length ? `<div class="vmx-leaves">${vms.map(v => `<button class="vmx-leaf${S.screen === 'vm' && S.host === h.pubkey && S.vm === v.uuid ? ' sel' : ''}" data-leaf-host="${esc(h.pubkey)}" data-leaf-vm="${esc(v.uuid)}">
          <span class="vmx-st vmx-st-${esc(v.state)}" aria-hidden="true"></span><span class="vmx-leafname">${esc(v.name)}</span></button>`).join('')}</div>` : ''}
      </div>`;
    }).join('');
    return `<aside class="vms-rail vmx-tree" aria-label="VM hosts">
      <div class="vms-head"><h2>VM hosts</h2><span class="vms-sp"></span><button class="btn small" data-act="find">Find</button><button class="btn small" data-act="add">+ Add</button></div>
      ${S.hosts.length > 1 ? `<button class="vmx-dc${S.screen === 'all' ? ' sel' : ''}" data-act="all">${ic('i-drive')}<b>All hosts</b><span class="vmx-count">${running}/${total}</span></button>` : ''}
      ${S.hosts.length ? rows : (S.doc.read ? '<div class="vms-seen">No hosts yet.</div>' : '<div class="spinner"></div>')}
    </aside>`;
  }

  // ---- the details table
  const COLS = [
    { k: 'state', l: 'Status' }, { k: 'name', l: 'Name' }, { k: 'vcpus', l: 'vCPU', n: 1 }, { k: 'ram', l: 'Memory', n: 1 },
    { k: 'disk', l: 'Disk', n: 1 }, { k: 'net', l: 'Network / IP' }, { k: 'up', l: 'Uptime', n: 1 },
    { k: 'host', l: 'Host', all: 1 }, { k: 'assigned', l: 'Assigned', admin: 1 },
  ];
  const STATE_ORDER = { running: 0, paused: 1, stopping: 2, other: 3, shutoff: 4 };
  function sortKey(k, v){
    switch(k){
      case 'state': return STATE_ORDER[v.state] != null ? STATE_ORDER[v.state] : 9;
      case 'vcpus': return Number(v.vcpus) || 0;
      case 'ram': return Number(v.ram_mib) || 0;
      case 'disk': return Number(v.disk_gib) || 0;
      case 'net': return (ipsOf(v)[0] || netOf(v) || '~').toLowerCase();
      case 'up': return v.state === 'running' && v.uptime_s != null ? Number(v.uptime_s) : -1;
      case 'host': return hostName(v._pk).toLowerCase();
      case 'assigned': return (v.assigned || []).length;
      default: return String(v.name || '').toLowerCase();
    }
  }
  function sortedFiltered(list){
    let out = list.slice();
    if(S.filter === 'running') out = out.filter(v => v.state === 'running');
    if(S.filter === 'stopped') out = out.filter(v => v.state !== 'running');
    if(S.filter === 'mine') out = out.filter(v => (v.assigned || []).includes(S.pk));
    const q = (S.q || '').trim().toLowerCase();
    if(q) out = out.filter(v => (v.name || '').toLowerCase().includes(q) || ipsOf(v).some(ip => ip.includes(q)));
    const k = S.sort.key, dir = S.sort.dir;
    out.sort((a, b) => { const x = sortKey(k, a), y = sortKey(k, b);
      return (x < y ? -1 : x > y ? 1 : 0) * dir || String(a.name).localeCompare(String(b.name)); });
    return out;
  }
  function vmTable(list, opts){
    const cols = COLS.filter(c => (!c.all || opts.all) && (!c.admin || opts.admin));
    const head = cols.map(c => `<th scope="col" class="vmx-c-${c.k}${c.n ? ' vmx-num' : ''}"><button class="vmx-sort${S.sort.key === c.k ? ' on' : ''}" data-sort="${c.k}">${esc(c.l)}${S.sort.key === c.k ? `<span class="fx-arrow">${S.sort.dir > 0 ? '▲' : '▼'}</span>` : ''}</button></th>`).join('');
    const cell = (c, v) => {
      switch(c.k){
        case 'state': return `<span class="vmx-stcell"><span class="vmx-st vmx-st-${esc(v.state)}" aria-hidden="true"></span><span class="vms-pill vms-st-${esc(v.state)}">${esc(stateLabel(v.state))}</span>${v.migration && v.migration.state ? '<span class="vms-pill vms-st-paused">migrating</span>' : ''}</span>`;
        case 'name': return `<b class="vmx-name">${esc(v.name)}</b>${v.guest ? `<small class="vms-seen"> ${esc(v.guest)}</small>` : ''}`;
        case 'vcpus': return esc(v.vcpus || '—');
        case 'ram': return v.ram_mib ? esc(fmtMib(v.ram_mib)) : '—';
        case 'disk': return v.disk_gib ? esc(v.disk_gib) + ' GiB' : '—';
        case 'net': { const ips = ipsOf(v); return `${ips.length ? `<span class="vmx-ip">${esc(ips.join(', '))}</span>` : ''}<small class="vms-seen">${esc(netOf(v) || (ips.length ? '' : '—'))}</small>`; }
        case 'up': return esc(upOf(v));
        case 'host': return esc(hostName(v._pk));
        case 'assigned': return (v.assigned || []).length ? esc((v.assigned || []).length === 1 ? short(v.assigned[0]) : v.assigned.length + ' people') : '<span class="vms-seen">—</span>';
      }
      return '';
    };
    const rows = list.map(v => `<tr class="vms-vm vmx-row" data-vm="${esc(v.uuid)}" data-vm-host="${esc(v._pk)}" tabindex="0">${cols.map(c => `<td class="vmx-c-${c.k}${c.n ? ' vmx-num' : ''}">${cell(c, v)}</td>`).join('')}</tr>`).join('');
    return `<div class="vmx-tablewrap"><table class="vmx-table"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  // The same rows as cards, below the wide breakpoint.
  function vmCard(v, opts){
    const ips = ipsOf(v);
    const bits = [v.vcpus ? v.vcpus + ' vCPU' : '', v.ram_mib ? fmtMib(v.ram_mib) : '', v.disk_gib ? v.disk_gib + ' GiB' : '',
                  ips[0] || netOf(v), v.state === 'running' && v.uptime_s != null ? 'up ' + fmtDur(v.uptime_s) : '',
                  opts.all ? hostName(v._pk) : ''].filter(Boolean);
    return `<button class="vms-vm" data-vm="${esc(v.uuid)}" data-vm-host="${esc(v._pk)}">
      <span class="vms-pill vms-st-${esc(v.state)}">${esc(stateLabel(v.state))}</span>
      <b>${esc(v.name)}</b><span class="vms-spec">${esc(bits.join(' · '))}</span></button>`;
  }
  function listToolbar(opts){
    const chips = [['all', 'All'], ['running', 'Running'], ['stopped', 'Stopped']].concat(opts.mine ? [['mine', 'Mine']] : []);
    return `<div class="vmx-listbar"><div class="vms-chips">${chips.map(([k, l]) => `<button class="vms-chip${S.filter === k ? ' on' : ''}" data-filter="${k}">${l}</button>`).join('')}</div>
      <label class="vmx-search">${ic('i-search')}<input class="input" type="search" data-vmq placeholder="Filter by name or IP" value="${esc(S.q || '')}" aria-label="Filter VMs"></label></div>`;
  }
  function tiles(items){
    return `<div class="vmx-tiles">${items.filter(Boolean).map(t => `<div class="vmx-tile"><span>${esc(t.l)}</span><b>${t.html != null ? t.html : esc(t.v)}</b>${t.sub ? `<small>${t.sub}</small>` : ''}</div>`).join('')}</div>`;
  }
  function hostSummary(pk){
    const d = S.data[pk] || {};
    const info = d.info || {};
    const vms = d.vms || [];
    const running = vms.filter(v => v.state === 'running').length;
    const out = [{ l: 'Virtual machines', html: `${esc(info.vms ? info.vms.total : vms.length)}`, sub: `${esc(info.vms ? info.vms.running : running)} running` }];
    if(info.cpu) out.push({ l: 'CPU', v: info.cpu.cores + ' cores', sub: 'load ' + esc(info.cpu.load1) });
    if(info.ram && info.ram.total_mib) out.push({ l: 'Memory', v: fmtMib(info.ram.total_mib - info.ram.free_mib) + ' used',
      sub: bar('RAM MiB', info.ram.total_mib - info.ram.free_mib, info.ram.total_mib, info.ram.committed_mib) + `committed ${esc(fmtMib(info.ram.committed_mib))} of ${esc(fmtMib(info.ram.total_mib))}` });
    if(info.disk && info.disk.total_gib) out.push({ l: 'Storage', v: (info.disk.total_gib - info.disk.free_gib) + ' GiB used',
      sub: bar('Disk GiB', info.disk.total_gib - info.disk.free_gib, info.disk.total_gib, info.disk.committed_gib) + `${esc(info.disk.free_gib)} GiB free` });
    if(info.kvm != null || info.libvirt != null) out.push({ l: 'Hypervisor', html: info.kvm === false ? '<span class="vmx-warn">no KVM</span>' : 'KVM',
      sub: esc(info.libvirt_version || (info.libvirt === false ? 'libvirt unreachable' : 'libvirt')) });
    return tiles(out);
  }

  function hostScreen(pk, wide){
    const h = hostOf(pk);
    if(!h) return hostsScreen();
    const d = S.data[pk] || {};
    const role = roleOf(pk);
    const feat = featuresOf(pk);
    const list = sortedFiltered((d.vms || []).map(v => Object.assign({ _pk: pk }, v)));
    let body;
    if(!d.vms){
      body = d.status === 'noanswer' || d.status === 'error' ? '' : `<div class="spinner"></div>`;
    }else if(!list.length){
      body = `<div class="empty vms-empty">${d.vms.length ? 'Nothing matches this filter.'
        : role === 'admin' ? 'No virtual machines on this host yet.' : 'No VMs are assigned to you on this host.'}</div>`;
    }else body = wide ? vmTable(list, { admin: role === 'admin' && !feat.local })
                      : `<div class="vms-list">${list.map(v => vmCard(v, {})).join('')}</div>`;
    return `<div class="vms-head">${wide ? '' : '<button class="btn small vms-back" data-act="back">‹ Hosts</button>'}
        <span class="vms-dot vms-dot-${dotOf(pk)}"></span><h2>${esc(hostName(pk))}</h2>${role ? `<span class="vms-role vms-role-${esc(role)}">${esc(role)}</span>` : ''}
        <span class="vms-sp"></span>
        ${h.source !== 'instance' && h.source !== 'local' ? `<button class="btn small" data-act="remove-host" aria-label="Remove host" title="Remove this host from your list">✕</button>` : ''}</div>
      <div class="vms-host-sub">${h.source === 'local' ? 'VMs in this account on this computer' : esc(short(pk)) + (h.source === 'instance' ? ' · this server' : '')}</div>
      ${statusLine(pk)}
      ${hostSummary(pk)}
      <div class="vmx-toolbar">
        ${role === 'admin' ? `<button class="btn btn-neon small" data-act="create">${ic('i-plus')} Create VM</button>
          ${feat.local ? '' : `<button class="btn btn-ghost small" data-act="isos">ISO library</button>`}
          ${feat.access ? `<button class="btn btn-ghost small" data-act="access">Access</button>` : ''}` : ''}
        <span class="vms-sp"></span><button class="btn small" data-act="retry" data-host="${esc(pk)}" aria-label="Refresh" title="Refresh">${ic('i-refresh')}</button>
      </div>
      ${d.vms && d.vms.length ? listToolbar({ mine: role === 'admin' && !feat.local }) : ''}
      ${body}`;
  }

  // "All hosts" — Proxmox's Datacenter view: every VM on every host in one table, with a Host column.
  function allScreen(wide){
    const all = [];
    let total = 0, running = 0, answered = 0;
    for(const h of S.hosts){
      const d = S.data[h.pubkey] || {};
      if(d.vms) answered++;
      for(const v of d.vms || []){ all.push(Object.assign({ _pk: h.pubkey }, v)); total++; if(v.state === 'running') running++; }
    }
    const list = sortedFiltered(all);
    const silent = S.hosts.filter(h => ['noanswer', 'error'].includes((S.data[h.pubkey] || {}).status)).length;
    return `<div class="vms-head"><h2>All hosts</h2><span class="vms-sp"></span></div>
      ${tiles([{ l: 'Hosts', v: S.hosts.length, sub: answered + ' answered' + (silent ? ' · ' + silent + ' silent' : '') },
               { l: 'Virtual machines', v: total, sub: running + ' running' }])}
      ${silent ? '<div class="vms-noanswer">Some hosts did not answer — their VMs are missing from this list until they do.</div>' : ''}
      ${total ? listToolbar({}) : ''}
      ${!total ? '<div class="empty vms-empty">No virtual machines on any host yet.</div>'
        : !list.length ? '<div class="empty vms-empty">Nothing matches this filter.</div>'
        : wide ? vmTable(list, { all: true }) : `<div class="vms-list">${list.map(v => vmCard(v, { all: true })).join('')}</div>`}`;
  }

  function vmScreen(pk, uuid){
    const d = S.data[pk] || {};
    const v = (d.vms || []).find(x => x.uuid === uuid);
    const h = hostOf(pk);
    if(!v) return `<div class="vms-head"><button class="btn small vms-back" data-act="back">‹ ${esc((h && h.name) || 'Host')}</button></div><div class="empty">This VM is not in the host’s last answer.</div>`;
    const role = roleOf(pk);
    const feat = featuresOf(pk);
    const running = v.state === 'running' || v.state === 'paused';
    const off = v.state === 'shutoff';
    const b = (act, label, icon, cls, on) => `<button class="btn small ${cls || ''}" data-power="${act}" ${on && !S.busy[uuid + ':' + act] ? '' : 'disabled'}>${ic(icon)}${S.busy[uuid + ':' + act] ? '…' : esc(label)}</button>`;
    const ips = ipsOf(v);
    return `<div class="vms-head"><button class="btn small vms-back" data-act="back">‹ ${esc(hostName(pk))}</button></div>
      <div class="vms-vmhead"><span class="vmx-st vmx-st-${esc(v.state)}" aria-hidden="true"></span><h2>${esc(v.name)}</h2><span class="vms-pill vms-st-${esc(v.state)}">${esc(stateLabel(v.state))}</span>${v.migration && v.migration.state ? `<span class="vms-pill vms-st-paused">migrating (${esc(v.migration.state)})</span>` : ''}
        <span class="vms-sp"></span><small class="vms-seen">on ${esc(hostName(pk))}</small></div>
      <div class="vmx-toolbar vms-actions vms-power">
        ${b('start', 'Start', 'i-play', 'btn-neon', !running)}${b('shutdown', 'Shut down', 'i-power', '', running)}${b('reboot', 'Reboot', 'i-refresh', '', running)}${b('destroy', 'Force off', 'i-stop', 'btn-red', running)}
        <button class="btn small btn-cyan" data-act="console" ${v.state === 'running' ? '' : 'disabled'}>${ic('i-monitor')}${feat.console === 'spice' ? 'Open display' : 'Console'}</button>
        ${role === 'admin' ? '<span class="vmx-sep" aria-hidden="true"></span>' : ''}
        ${role === 'admin' && feat.hardware ? `<button class="btn small btn-ghost" data-act="settings" ${off ? '' : 'disabled title="Shut it down first"'}>${ic('i-gear')}Settings</button>` : ''}
        ${role === 'admin' && feat.local && off ? `<button class="btn small btn-ghost" data-act="boot-disk">Use installed system</button>` : ''}
        ${role === 'admin' && feat.migrate ? `<button class="btn small btn-ghost" data-act="migrate">${v.migration && v.migration.state ? 'Migration status' : 'Migrate…'}</button>` : ''}
        ${role === 'admin' ? `<button class="btn small btn-red" data-act="delete" ${off ? '' : 'disabled title="Shut it down first"'}>${ic('i-trash')}Delete</button>` : ''}
      </div>
      ${statusLine(pk)}
      ${tiles([{ l: 'Status', v: stateLabel(v.state), sub: v.state === 'running' && v.uptime_s != null ? 'up ' + esc(fmtDur(v.uptime_s)) : '' },
               { l: 'vCPUs', v: v.vcpus || '—' }, { l: 'Memory', v: v.ram_mib ? fmtMib(v.ram_mib) : '—' },
               { l: 'Disk', v: v.disk_gib ? v.disk_gib + ' GiB' : '—' },
               { l: 'Network', v: netOf(v) || '—', sub: ips.length ? esc(ips.join(', ')) : (running ? 'no address reported' : '') },
               { l: 'Guest', v: ((v.guest || '—') + ' ' + (v.firmware || '')).trim() }, { l: 'Autostart', v: v.autostart ? 'on' : 'off' }])}
      ${v.missing_media ? `<div class="vms-noanswer">Installer media moved or is missing: ${esc(v.missing_media)}. Open Settings to replace or eject it.</div>` : ''}
      ${role === 'admin' && feat.snapshots ? snapsBlock(pk, uuid, v) : ''}
      ${role === 'admin' && feat.assign ? `<div class="vms-assign vmx-section"><div class="vms-sub">Assigned to</div>
        ${(v.assigned || []).map(p => `<div class="vms-assignee"><span>${esc(short(p))}</span><button class="btn small" data-unassign="${esc(p)}">Remove</button></div>`).join('') || '<div class="vms-seen">Nobody — only admins can use it.</div>'}
        <button class="btn btn-ghost small" data-act="assign">+ Assign to an npub</button></div>` : ''}`;
  }

  function createScreen(pk){
    const d = S.data[pk] || {};
    const lim = (d.info && d.info.limits) || {};
    const C = S.create || {};
    const isos = C.isos;
    return `<div class="vms-head"><button class="btn small vms-back" data-act="back">‹ Cancel</button><h2>Create a VM</h2></div>
      <form id="vms-create" class="vms-form" onsubmit="return false">
        <label>Name<input class="input" name="name" maxlength="48" placeholder="web-1" autocomplete="off"></label>
        <div class="vms-row"><label>Operating system<select class="input" name="guest"><option value="linux">Linux</option><option value="windows">Windows</option></select></label>
          <label>Firmware<select class="input" name="firmware"><option value="efi">UEFI</option><option value="bios">Legacy BIOS</option></select></label></div>
        <div class="vms-row"><label>vCPUs<input class="input" name="vcpus" type="number" min="1" ${lim.max_vcpus ? `max="${esc(lim.max_vcpus)}"` : ''} value="2"></label>
          <label>Memory (MiB)<input class="input" name="ram_mib" type="number" min="256" step="256" ${lim.max_ram_mib ? `max="${esc(lim.max_ram_mib)}"` : ''} value="2048"></label>
          <label>Disk (GiB)<input class="input" name="disk_gib" type="number" min="1" ${lim.max_disk_gib ? `max="${esc(lim.max_disk_gib)}"` : ''} value="20"></label></div>
        ${d.info && d.info.ram ? `<div class="vms-seen">Uncommitted: ${esc(Math.max(0, d.info.ram.total_mib - (lim.reserve_ram_mib || 0) - d.info.ram.committed_mib))} MiB RAM · ${esc(Math.max(0, d.info.disk.free_gib - (lim.reserve_disk_gib || 0)))} GiB disk free</div>` : ''}
        ${pk === LOCAL_PK
          ? `<label>Installer ISO<span class="vms-pick"><span class="vms-seen">${esc(C.localIso || 'No file chosen')}</span><button class="btn btn-ghost small" data-act="local-pick-create">Choose file…</button></span></label>`
          : `<label>Installer ISO${isos == null ? ' <span class="spinner spinner-inline"></span>' : ''}<select class="input" name="iso"><option value="">No installer</option>${(isos || []).map(i => `<option value="${esc(i.id)}">${esc(i.name)}</option>`).join('')}</select></label>`}
        ${netSelect(netChoices(pk, C.bridges), netDefault(pk))}
        <label class="vms-check"><input type="checkbox" name="start" checked> Start it after creating</label>
        <label class="vms-check"><input type="checkbox" name="autostart"> Start with the host</label>
        <div class="vms-createmsg">${esc(C.msg || '')}</div>
        <div class="vms-actions"><button class="btn btn-neon" data-act="submit-create" ${C.busy ? 'disabled' : ''}>Create VM</button></div>
      </form>`;
  }

  // Snapshots are OFFLINE (the host takes them with qemu-img while the VM is shut off — see docs/VM_HOSTING.md): every
  // control that changes one is disabled, and says why, while the VM runs. `state` is what the host found on disk:
  // only an `ok` snapshot can be reverted; `disks_changed`/`incomplete`/`orphan` can only be deleted.
  const SNAP_STATE = { disks_changed: 'disks changed since — delete only', incomplete: 'incomplete — delete only', orphan: 'leftover data — delete only' };
  function snapsBlock(pk, uuid, v){
    const k = pk + ':' + uuid;
    const sn = S.snaps[k];
    const off = !v || v.state === 'shutoff';
    const why = off ? '' : ' title="Shut the VM down first"';
    const rows = sn && sn.list ? (sn.list.length ? sn.list.map(x => `<div class="vms-snap"><span><b>${esc(x.name)}</b><small class="vms-seen"> ${esc(x.created || '')}${x.description ? ' · ' + esc(x.description) : ''}${SNAP_STATE[x.state] ? ' · ' + esc(SNAP_STATE[x.state]) : ''}</small></span>
        <span class="vms-snap-b"><button class="btn small" data-snap-revert="${esc(x.name)}" ${sn.busy || !off || (x.state && x.state !== 'ok') ? 'disabled' : ''}${why}>Revert</button><button class="btn small btn-red" data-snap-delete="${esc(x.name)}" ${sn.busy || !off ? 'disabled' : ''}${why}>Delete</button></span></div>`).join('')
        : '<div class="vms-seen">No snapshots yet.</div>')
      : (sn && sn.loading ? '<div class="vms-seen"><span class="spinner spinner-inline"></span>Reading snapshots…</div>' : '');
    return `<div class="vms-snaps"><div class="vms-head"><div class="vms-sub">Snapshots</div><span class="vms-sp"></span>
        <button class="btn btn-ghost small" data-act="snap-create" ${sn && sn.busy || !off ? 'disabled' : ''}${why}>+ Take snapshot</button></div>
      ${off ? '' : '<div class="vms-seen vms-snap-off">Shut down to take a snapshot — snapshots save the disks and firmware settings of a stopped VM.</div>'}
      ${rows}${sn && sn.err ? `<div class="vms-seen">${esc(sn.err)}</div>` : ''}</div>`;
  }

  function settingsScreen(){
    const st = S.settings;
    const back = `<div class="vms-head"><button class="btn small vms-back" data-act="settings-leave">‹ Back</button><h2>${esc((st && st.vm && st.vm.name) || 'VM')} settings</h2></div>`;
    if(!st || !st.vm) return back + (st && st.msg ? `<div class="vms-noanswer">${esc(st.msg)}</div>` : '<div class="spinner"></div>');
    const v = st.vm, hw = st.hw || {}, local = st.pk === LOCAL_PK;
    const lim = ((S.data[st.pk] || {}).info || {}).limits || {};
    const mediaOpts = local
      ? `<option value="__keep">${esc(hw.media ? 'Keep ' + hw.media : 'No installer')}</option>${hw.media ? '<option value="__eject">Eject the installer</option>' : ''}`
      : `<option value="__keep">${esc(hw.media ? 'Keep ' + hw.media : 'No installer')}</option>${hw.media ? '<option value="__eject">Eject the installer</option>' : ''}${(st.isos || []).filter(i => i.id !== hw.media).map(i => `<option value="${esc(i.id)}">Insert ${esc(i.name)}</option>`).join('')}`;
    return `${back}
      <form id="vms-settings" class="vms-form vms-settingsform" onsubmit="return false">
        <div class="vms-seen">Turn the machine off before changing hardware.</div>
        <section class="vms-section"><h3>Performance</h3><div class="vms-row">
          <label>vCPUs<input class="input" name="vcpus" type="number" min="1" ${lim.max_vcpus ? `max="${esc(lim.max_vcpus)}"` : ''} value="${esc(v.vcpus)}"></label>
          <label>Memory (MiB)<input class="input" name="ram_mib" type="number" min="256" step="256" ${lim.max_ram_mib ? `max="${esc(lim.max_ram_mib)}"` : ''} value="${esc(v.ram_mib)}"></label></div>
          <label class="vms-check"><input type="checkbox" name="autostart" ${v.autostart ? 'checked' : ''}> Start automatically with the ${local ? 'computer' : 'host'}</label></section>
        <section class="vms-section"><h3>Startup and installation media</h3>
          <label>Start from<select class="input" name="boot"><option value="disk" ${hw.boot !== 'cdrom' ? 'selected' : ''}>Installed system (virtual disk)</option><option value="cdrom" ${hw.boot === 'cdrom' ? 'selected' : ''}>Installer ISO</option></select></label>
          <label>Installer disc<select class="input" name="media">${mediaOpts}</select></label>
          ${local ? `<div class="vms-pick"><span class="vms-seen">${esc(st.pick ? 'Insert ' + st.pick.split(/[\\/]/).pop() : '')}</span><button class="btn btn-ghost small" data-act="local-pick-settings">Choose ISO file…</button></div>` : ''}
          <div class="vms-seen">When installation finishes, eject the ISO and start from the installed system.</div></section>
        <section class="vms-section"><h3>Advanced hardware</h3><div class="vms-row">
          <label>Add a disk (GiB)<input class="input" name="add_disk_gib" type="number" min="0" ${lim.max_disk_gib ? `max="${esc(lim.max_disk_gib)}"` : ''} placeholder="0 = none" value=""></label>
          <label>Pointer<select class="input" name="input"><option value="tablet" ${hw.input !== 'mouse' ? 'selected' : ''}>Tablet (follows the cursor)</option><option value="mouse" ${hw.input === 'mouse' ? 'selected' : ''}>Relative mouse (games — Ctrl+Alt releases it)</option></select></label></div>
          ${netSelect(netChoices(st.pk, st.bridges), netValue(hw.net))}
          <label class="vms-check"><input type="checkbox" name="add_nic"> Add a network adapter (has ${esc(hw.nics || 0)})</label></section>
        <div class="vms-createmsg" aria-live="polite">${esc(st.msg || '')}</div>
        <div class="vms-formfoot"><button class="btn btn-ghost" data-act="settings-leave">Back</button><button class="btn btn-neon" data-act="settings-save" disabled>${st.busy ? 'Saving…' : 'Save settings'}</button></div>
      </form>`;
  }

  function isosScreen(){
    const I = S.iso || {};
    const h = hostOf(I.pk) || {};
    const feat = featuresOf(I.pk);
    const list = I.list == null ? (I.loading ? '<div class="spinner"></div>' : '')
      : (I.list.length ? `<div class="vms-list">${I.list.map(i => `<div class="vms-iso"><span><b>${esc(i.name)}</b><small class="vms-seen"> ${esc(mib(i.size))}</small></span><button class="btn small btn-red" data-iso-delete="${esc(i.id)}" ${I.busy ? 'disabled' : ''}>Delete</button></div>`).join('')}</div>`
        : '<div class="empty vms-empty">The library is empty.</div>');
    const jobs = (I.jobs || []).filter(j => !j.state || j.state === 'running').map(j => `<div class="vms-seen"><span class="spinner spinner-inline"></span>Downloading ${esc(j.url)} — ${esc(mib(j.bytes))}${j.total ? ' of ' + esc(mib(j.total)) : ''} <button class="btn btn-sm" data-act="iso-cancel" data-job="${esc(j.id || '')}">Cancel</button></div>`).join('');
    return `<div class="vms-head"><button class="btn small vms-back" data-act="to-host">‹ ${esc(h.name || 'Host')}</button><h2>ISO library</h2><span class="vms-sp"></span>
        <button class="btn small" data-act="isos-refresh" aria-label="Refresh">↻</button></div>
      ${jobs}${list}
      <div class="vms-isomsg vms-createmsg" aria-live="polite">${esc(I.msg || '')}</div>
      <div class="vms-actions">
        ${I.fetchEnabled !== false && feat.iso === 'library' ? `<button class="btn btn-neon" data-act="iso-fetch" ${I.busy ? 'disabled' : ''}>Download from URL</button>` : ''}
        ${feat.upload ? `<label class="btn btn-ghost vms-upload ${I.busy ? 'disabled' : ''}">Upload ISO<input type="file" accept=".iso,application/x-iso9660-image" data-act="iso-upload" hidden ${I.busy ? 'disabled' : ''}></label>` : ''}
      </div>`;
  }

  function accessScreen(){
    const A = S.access || {};
    const h = hostOf(A.pk) || {};
    const dirty = A.allowed && JSON.stringify(A.allowed) !== A.clean;
    const body = A.allowed == null ? (A.msg ? '' : '<div class="spinner"></div>')
      : `<div class="vms-sub">Allowed users — see only the VMs assigned to them</div>
         ${A.allowed.length ? A.allowed.map(pk => `<div class="vms-assignee"><span>${esc(short(pk))}</span><button class="btn small" data-access-remove="${esc(pk)}" ${A.busy ? 'disabled' : ''}>Remove</button></div>`).join('') : '<div class="vms-seen">Nobody yet. Assigning a VM to an npub also lets them in.</div>'}
         <button class="btn btn-ghost small" data-act="access-add" ${A.busy ? 'disabled' : ''}>+ Allow an npub</button>
         <div class="vms-sub">Host admins (read-only here — change them in that server's Admin → VMs)</div>
         ${A.admins.map(pk => `<div class="vms-assignee"><span>${esc(short(pk))}</span></div>`).join('') || '<div class="vms-seen">Only the site’s own admin accounts.</div>'}`;
    return `<div class="vms-head"><button class="btn small vms-back" data-act="to-host">‹ ${esc(h.name || 'Host')}</button><h2>Access</h2></div>
      <div class="vms-assign">${body}</div>
      <div class="vms-createmsg" aria-live="polite">${esc(A.msg || '')}</div>
      <div class="vms-formfoot"><button class="btn btn-neon" data-act="access-save" ${dirty && !A.busy ? '' : 'disabled'}>${A.busy ? 'Saving…' : 'Save access'}</button></div>`;
  }

  function findScreen(){
    const F = S.find || {};
    return `<div class="vms-head"><button class="btn small vms-back" data-act="find-back">‹ Hosts</button><h2>Find hosts</h2></div>
      <div class="vms-seen">${F.running ? '<span class="spinner spinner-inline"></span>' : ''}${esc(F.msg || '')}</div>
      ${F.candidates ? `<div class="vms-seen">Checked ${esc(F.checked)} of ${esc(F.candidates)}${F.silent ? ' · ' + esc(F.silent) + ' did not answer (offline, or you are not on their list)' : ''}</div>` : ''}
      ${(F.added || []).map(a => `<button class="vms-host" data-host="${esc(a.pubkey)}"><div class="vms-host-top"><span class="vms-dot vms-dot-ok"></span><b>${esc(a.name)}</b><span class="vms-role vms-role-${esc(a.role)}">${esc(a.role)}</span></div><div class="vms-host-sub">${esc(short(a.pubkey))}</div></button>`).join('')}
      <div class="vms-actions"><button class="btn btn-ghost small" data-act="find" ${F.running ? 'disabled' : ''}>Search again</button></div>`;
  }

  function paintCreateMsg(){ const m = document.querySelector('#vms-create .vms-createmsg'); if(m && S.create) m.textContent = S.create.msg || ''; }

  const STYLE = `
.vms{display:flex;flex-direction:column;gap:12px;padding:12px 16px 90px;min-height:100%;box-sizing:border-box}
.vms.vms-wide{flex-direction:row;align-items:flex-start;padding-bottom:24px}
.vms-rail{flex:0 0 300px;display:flex;flex-direction:column;gap:10px;position:sticky;top:0}
.vms-main{flex:1;min-width:0;display:flex;flex-direction:column;gap:12px}
.vms-hosts{display:flex;flex-direction:column;gap:10px}
.vms-host:not(.vmx-treehost),button.vms-vm{all:unset;box-sizing:border-box;display:block;cursor:pointer;background:linear-gradient(180deg,rgba(var(--accent-rgb),.05),var(--panel2) 60%);border:1px solid var(--line);border-radius:var(--r);padding:13px 15px;color:var(--text);width:100%;transition:border-color .15s ease,background .15s ease,transform .12s ease,box-shadow .15s ease}
.vms-host:not(.vmx-treehost):hover,button.vms-vm:hover{border-color:rgba(var(--accent-rgb),.55);background:var(--panel);transform:translateY(-1px);box-shadow:var(--sh-1)}
.vms-host:not(.vmx-treehost).sel{border-color:var(--neon);background:rgba(var(--accent-rgb),.10);box-shadow:inset 3px 0 0 var(--neon)}
.vms-host.sel:hover{transform:none}
.vms-host-top,.vms-head,.vms-vmhead{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.vms-host-top b{font-size:15px;letter-spacing:-.01em}
.vms-head h2,.vms-vmhead h2{margin:0;font-size:20px;overflow-wrap:anywhere}
.vms-sp{flex:1}
.vms-host-sub,.vms-host-cap,.vms-seen,.vms-spec,.vms-sub{color:var(--muted);font-size:13px}
.vms-dot{width:10px;height:10px;border-radius:50%;display:inline-block;background:var(--muted);box-shadow:0 0 0 3px rgba(255,255,255,.03)}
.vms-dot-ok{background:var(--green);box-shadow:0 0 8px var(--green)}.vms-dot-bad{background:var(--amber);box-shadow:0 0 8px var(--amber)}
.vms-role{font-size:11px;text-transform:uppercase;letter-spacing:.06em;border:1px solid var(--line);border-radius:99px;padding:1px 8px;color:var(--neon)}
.vms-bar{margin-top:6px}.vms-bar-l{display:flex;justify-content:space-between;font-size:12px;color:var(--muted)}
.vms-bar-t{position:relative;height:6px;border-radius:3px;background:var(--line);overflow:hidden}
.vms-bar-t i{position:absolute;left:0;top:0;bottom:0;border-radius:3px}
.vms-bar-used{background:var(--neon);z-index:2}.vms-bar-com{background:rgba(var(--accent2-rgb),.45);z-index:1}
.vms-noanswer{background:rgba(255,207,43,.1);border:1px solid rgba(255,207,43,.4);border-radius:var(--r-sm);padding:8px 10px;font-size:14px}
.vms-list{display:flex;flex-direction:column;gap:8px}
button.vms-vm{display:grid;grid-template-columns:auto 1fr;grid-template-rows:auto auto;column-gap:10px;align-items:center;border-left:3px solid var(--line)}
button.vms-vm:has(.vms-st-running){border-left-color:var(--green)}
button.vms-vm:has(.vms-st-paused),button.vms-vm:has(.vms-st-stopping){border-left-color:var(--amber)}
button.vms-vm .vms-pill{grid-row:1/3}button.vms-vm b{overflow-wrap:anywhere}
.vms-pill{font-size:12px;border-radius:99px;padding:2px 9px;border:1px solid var(--line);white-space:nowrap}
.vms-st-running{color:var(--green);border-color:var(--green);background:rgba(0,255,136,.10)}.vms-st-paused,.vms-st-stopping{color:var(--amber);border-color:var(--amber);background:rgba(255,207,43,.10)}
.vms-chips{display:flex;gap:6px;flex-wrap:wrap}
.vms-chip{all:unset;cursor:pointer;border:1px solid var(--line);border-radius:99px;padding:4px 12px;font-size:13px}
.vms-chip.on{border-color:var(--neon);color:var(--neon)}
.vms-specs{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px}
.vms-specs>div{background:var(--panel2);border:1px solid var(--line);border-radius:var(--r-sm);padding:9px 11px;display:flex;flex-direction:column;gap:2px;font-weight:600;transition:border-color .15s ease}
.vms-specs>div:hover{border-color:rgba(var(--accent-rgb),.4)}
.vms-specs span{color:var(--muted);font-size:12px}
.vms-assign{display:flex;flex-direction:column;gap:6px}.vms-assignee{display:flex;justify-content:space-between;align-items:center;gap:8px}
.vms-actions{display:flex;gap:8px;flex-wrap:wrap}
.vms-form{display:flex;flex-direction:column;gap:10px}.vms-form label{display:flex;flex-direction:column;gap:4px;font-size:14px;flex:1;min-width:0}
.vms-row{display:flex;gap:10px;flex-wrap:wrap}.vms-row label{min-width:120px}
.vms-form label.vms-check{flex-direction:row;align-items:center;gap:8px}
.vms-createmsg{color:var(--muted);min-height:1em}
.vms-cold{display:flex;flex-direction:column;align-items:center}
.vms-section{display:flex;flex-direction:column;gap:8px;border:1px solid var(--line);border-radius:var(--r-sm);padding:10px 12px}
.vms-section h3{margin:0;font-size:15px;color:var(--neon);letter-spacing:-.01em}
.vms-formfoot{position:sticky;bottom:0;z-index:4;display:flex;justify-content:flex-end;gap:8px;padding:10px 0;background:var(--canvas);border-top:1px solid var(--line)}
.vms-snaps{display:flex;flex-direction:column;gap:6px}
.vms-snap,.vms-iso{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;background:var(--panel2);border:1px solid var(--line);border-radius:var(--r-sm);padding:8px 10px}
.vms-snap span,.vms-iso span{min-width:0;overflow-wrap:anywhere}
.vms-snap-b{display:flex;gap:6px}
.vms-pick{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}
.vms-pick .vms-seen{overflow-wrap:anywhere;min-width:0}
.vms-upload{cursor:pointer}
.vms-tools{display:flex;gap:8px;flex-wrap:wrap}
@media (max-width:1023px){.vms:not(.vms-wide) .vms-actions{position:sticky;bottom:0;background:var(--canvas);padding:10px 0;z-index:3}}
.vms-mig{display:flex;flex-direction:column;gap:10px}
.vms-mig-bar{position:relative;height:10px;border-radius:5px;background:var(--line);overflow:hidden}
.vms-mig-bar i{position:absolute;left:0;top:0;bottom:0;background:var(--neon);border-radius:5px;transition:width .3s}
.vms-mig-side{display:flex;align-items:center;gap:8px;flex-wrap:wrap;overflow-wrap:anywhere}
.vms-mig-pre,.vms-mig-done{background:rgba(var(--accent2-rgb),.1);border:1px solid var(--line);border-radius:var(--r-sm);padding:8px 10px;font-size:14px}
.vms-mig-locked{background:rgba(255,80,80,.1);border:1px solid rgba(255,80,80,.5);border-radius:var(--r-sm);padding:10px;font-size:14px;display:flex;flex-direction:column;gap:8px}
.vmx.vms-wide{gap:14px}
.vmx .vmx-tree{flex:0 0 250px;gap:2px;max-height:calc(100dvh / var(--zf,1) - 40px);overflow:auto;padding-right:4px;border-right:1px solid var(--line)}
.vmx-tree .vms-head{margin-bottom:6px}.vmx-tree .vms-head h2{font-size:16px}
.vmx-dc,.vmx-treehost,.vmx-leaf{all:unset;box-sizing:border-box;display:flex;align-items:center;gap:7px;width:100%;cursor:pointer;border-radius:var(--r-sm);padding:5px 8px;color:var(--text);min-width:0}
.vmx-dc:hover,.vmx-treehost:hover,.vmx-leaf:hover{background:rgba(var(--accent-rgb),.08)}
.vmx-dc.sel,.vmx-treehost.sel,.vmx-leaf.sel{background:rgba(var(--accent-rgb),.16);box-shadow:inset 3px 0 0 var(--neon)}
.vmx-dc:focus-visible,.vmx-treehost:focus-visible,.vmx-leaf:focus-visible,.vmx-twisty:focus-visible,.vmx-sort:focus-visible,.vmx-row:focus-visible{outline:2px solid var(--neon);outline-offset:-2px}
.vmx-treehost b,.vmx-dc b{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px}
.vmx-treehost svg.ic,.vmx-dc svg.ic{width:16px;height:16px;color:var(--muted)}
.vmx-hostrow{display:flex;align-items:center;gap:2px}
.vmx-twisty{all:unset;cursor:pointer;width:18px;height:22px;display:flex;align-items:center;justify-content:center;flex:0 0 18px;border-radius:4px}
.vmx-chev{width:6px;height:6px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(-45deg);transition:transform .15s ease}
.vmx-chev.open{transform:rotate(45deg)}
.vmx-count{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.vmx-leaves{display:flex;flex-direction:column;margin:1px 0 4px 20px;border-left:1px solid var(--line);padding-left:4px}
.vmx-leaf{padding:4px 8px;font-size:13px}
.vmx-leafname{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vmx-st{flex:0 0 auto;width:9px;height:9px;border-radius:50%;background:var(--muted);display:inline-block;opacity:.8}
.vmx-st-running{background:var(--green);box-shadow:0 0 6px var(--green);opacity:1}
.vmx-st-paused,.vmx-st-stopping{background:var(--amber);opacity:1}
.vmx-tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:8px}
.vmx-tile{background:var(--panel2);border:1px solid var(--line);border-radius:var(--r-sm);padding:10px 12px;display:flex;flex-direction:column;gap:3px;min-width:0}
.vmx-tile>span{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.vmx-tile>b{font-size:17px;overflow-wrap:anywhere}
.vmx-tile>small{color:var(--muted);font-size:12px;overflow-wrap:anywhere}
.vmx-tile .vms-bar{margin:4px 0 2px}.vmx-tile .vms-bar-l{display:none}
.vmx-warn{color:var(--amber)}
.vmx-toolbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:6px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--panel2)}
.vmx-toolbar .btn{display:inline-flex;align-items:center;gap:6px}
.vmx-toolbar svg.ic{width:16px;height:16px}
.vmx-sep{width:1px;align-self:stretch;background:var(--line);margin:0 4px}
.vmx-listbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between}
.vmx-search{display:flex;align-items:center;gap:6px;flex:0 1 260px;min-width:0}
.vmx-search svg.ic{width:16px;height:16px;color:var(--muted)}
.vmx-search .input{margin:0;padding:6px 10px;min-width:0}
.vmx-tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--panel)}
.vmx-table{width:100%;border-collapse:collapse;font-size:14px}
.vmx-table th{position:sticky;top:0;z-index:1;background:var(--panel2);text-align:left;padding:0;border-bottom:1px solid var(--line);white-space:nowrap}
.vmx-sort{all:unset;box-sizing:border-box;cursor:pointer;display:flex;align-items:center;gap:4px;width:100%;padding:8px 10px;font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.vmx-sort:hover,.vmx-sort.on{color:var(--text)}
.vmx-num .vmx-sort{justify-content:flex-end}
.vmx-table td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:middle;white-space:nowrap}
.vmx-table td.vmx-num{text-align:right;font-variant-numeric:tabular-nums}
.vmx-stcell{display:inline-flex;align-items:center;gap:7px}
.vmx-table td.vmx-c-net{white-space:normal;min-width:120px}
.vmx-table td.vmx-c-net small{display:block}
.vmx-table tr.vmx-row{cursor:pointer}
.vmx-table tr.vmx-row:hover{background:rgba(var(--accent-rgb),.07)}
.vmx-table tbody tr:last-child td{border-bottom:0}
.vmx-name{font-weight:600}
.vmx-ip{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
.vmx-section{border:1px solid var(--line);border-radius:var(--r-sm);padding:10px 12px}
.vmc{position:fixed;inset:0;z-index:2147483646;background:#000;display:flex;flex-direction:column;-webkit-app-region:no-drag;app-region:no-drag}
html.pc-oswin .vmc{top:38px}
.vmc:fullscreen,html.pc-oswin .vmc:fullscreen{top:0}
.vmc-bar{display:flex;gap:6px;align-items:center;padding:6px 8px;background:var(--bg2);color:var(--text);flex-wrap:wrap;-webkit-app-region:no-drag;app-region:no-drag}
.vmc-bar .btn{-webkit-app-region:no-drag;app-region:no-drag}
.vmc.vmc-on [data-c=grab]{border-color:var(--neon);color:var(--neon)}
.vmc-screen.vmc-grabbed{cursor:none}
.vmc-st{color:var(--muted);font-size:13px}.vmc-sp{flex:1}
.vmc-screen{flex:1;min-height:0;position:relative;overflow:hidden}
.vmc-kbd{position:absolute;left:-9999px;top:0;width:1px;height:1px;opacity:0}
`;
  function injectStyle(){
    if(document.getElementById('pc-vms-style')) return;
    const st = document.createElement('style'); st.id = 'pc-vms-style'; st.textContent = STYLE;
    (document.head || document.documentElement).appendChild(st);
  }

  function isWide(feed){
    // ≥1024px screens get the host rail — but only when the feed column itself has room for two panes
    // (a desktop-mode window can be a narrow slice of a wide screen).
    let w = 0;
    try{ w = feed.getBoundingClientRect().width; }catch(_){}
    const vw = window.innerWidth || 0;
    return w ? (vw >= 1024 && w >= 720) : vw >= 1024;
  }

  function paint(){
    if(!PC || !inView()) return;
    const feed = document.querySelector('#feed');
    if(!feed) return;
    injectStyle();
    const wide = isWide(feed);
    if(wide && S.screen === 'hosts' && S.hosts.length && !S.host) S.host = S.hosts[0].pubkey;
    let main;
    if(S.screen === 'settings' && S.settings) main = settingsScreen();
    else if(S.screen === 'isos' && S.iso) main = isosScreen();
    else if(S.screen === 'access' && S.access) main = accessScreen();
    else if(S.screen === 'find') main = findScreen();
    else if(S.screen === 'all' && S.hosts.length) main = allScreen(wide);
    else if(S.screen === 'vm' && S.host) main = vmScreen(S.host, S.vm);
    else if(S.screen === 'create' && S.host) main = createScreen(S.host);
    else if(S.screen === 'migrate' && S.host && S.mig) main = migrateScreen(S.host);
    else if((S.screen === 'host' || wide) && S.host && hostOf(S.host)) main = hostScreen(S.host, wide);
    else main = hostsScreen();
    // Keep what somebody is typing in a form across a background repaint (create, settings). A Save that
    // just landed repaints from the host's answer instead (`fresh`).
    const keep = {};
    const fresh = S.settings && S.settings.fresh;
    if(S.settings) S.settings.fresh = false;
    const form = feed.querySelector('form.vms-form[id]');
    const formId = form ? form.id : '';
    if(form && !(fresh && formId === 'vms-settings')) form.querySelectorAll('[name]').forEach(el => { keep[el.name] = el.type === 'checkbox' ? el.checked : el.value; });
    const scroll = feed.scrollTop;
    // The filter box keeps its caret across a repaint (the list repaints as you type).
    const q = feed.querySelector('[data-vmq]');
    const qFocus = !!(q && document.activeElement === q), qPos = qFocus ? q.selectionStart : 0;
    feed.innerHTML = wide
      ? `<div class="vms vms-wide vmx">${tree()}
         <section class="vms-main">${S.hosts.length ? main : hostsScreen()}</section></div>`
      : `<div class="vms">${main}</div>`;
    if(qFocus){ const nq = feed.querySelector('[data-vmq]'); if(nq){ try{ nq.focus(); nq.setSelectionRange(qPos, qPos); }catch(_){} } }
    const nf = formId ? feed.querySelector('#' + formId) : null;
    if(nf) for(const k in keep){ const el = nf.querySelector('[name="' + k + '"]'); if(el){ if(el.type === 'checkbox') el.checked = keep[k]; else el.value = keep[k]; } }
    feed.scrollTop = scroll;
    bind(feed);
    syncSettingsSave();
  }

  function bind(feed){
    const on = (sel, fn) => feed.querySelectorAll(sel).forEach(el => { el.onclick = (e) => { e.preventDefault(); fn(el, e); }; });
    on('[data-host]:not([data-act])', el => { S.host = el.dataset.host; S.screen = 'host'; S.vm = ''; S.filter = 'all'; paint(); refresh(S.host); });
    on('[data-act=retry]', el => { refresh(el.dataset.host || S.host); paint(); });
    on('[data-act=add]', () => addHost());
    on('[data-act=remove-host]', () => removeHost(S.host));
    on('[data-act=back]', () => {
      if(S.screen === 'migrate'){ stopMigWatch(); S.mig = null; S.screen = 'vm'; }
      else if(S.screen === 'vm' && S.fromAll){ S.screen = 'all'; S.vm = ''; }
      else if(S.screen === 'vm' || S.screen === 'create'){ S.screen = 'host'; S.vm = ''; S.create = null; }
      else if(S.screen === 'isos' || S.screen === 'access'){ S.screen = 'host'; S.iso = null; S.access = null; }
      else if(S.screen === 'find'){ S.screen = 'hosts'; S.find = null; }
      else { S.screen = 'hosts'; if(!isWide(feed)) S.host = ''; }
      paint();
    });
    on('[data-filter]', el => { S.filter = el.dataset.filter; paint(); });
    const openVm = (pk, uuid) => { S.fromAll = S.screen === 'all'; if(pk) S.host = pk; S.vm = uuid; S.screen = 'vm'; paint();
      if(roleOf(S.host) === 'admin' && featuresOf(S.host).snapshots) loadSnaps(S.host, S.vm); };
    on('[data-vm]', el => openVm(el.dataset.vmHost, el.dataset.vm));
    feed.querySelectorAll('tr[data-vm]').forEach(el => { el.onkeydown = (e) => { if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); openVm(el.dataset.vmHost, el.dataset.vm); } }; });
    on('[data-leaf-vm]', el => openVm(el.dataset.leafHost, el.dataset.leafVm));
    on('[data-twisty]', el => { const k = el.dataset.twisty; S.collapsed[k] = !S.collapsed[k]; paint(); });
    on('[data-act=all]', () => { S.screen = 'all'; S.vm = ''; paint(); S.hosts.forEach(h => refresh(h.pubkey)); });
    on('[data-sort]', el => { const k = el.dataset.sort; S.sort = S.sort.key === k ? { key: k, dir: -S.sort.dir } : { key: k, dir: 1 }; paint(); });
    feed.querySelectorAll('[data-vmq]').forEach(el => { el.oninput = () => { S.q = el.value; paint(); }; });
    on('[data-power]', el => power(S.host, S.vm, el.dataset.power));
    on('[data-act=console]', () => { const v = ((S.data[S.host] || {}).vms || []).find(x => x.uuid === S.vm); if(v) openConsole(S.host, v); });
    on('[data-act=assign]', () => assign(S.host, S.vm));
    on('[data-unassign]', el => unassign(S.host, S.vm, el.dataset.unassign));
    on('[data-act=delete]', () => { const v = ((S.data[S.host] || {}).vms || []).find(x => x.uuid === S.vm); if(v) del(S.host, v); });
    on('[data-act=create]', () => openCreate(S.host));
    on('[data-act=submit-create]', () => submitCreate(S.host));
    // ---- phase 2
    on('[data-act=find]', () => findHosts());
    on('[data-act=find-back]', () => { S.find = null; S.screen = 'hosts'; paint(); });
    on('[data-act=settings]', () => openSettings(S.host, S.vm));
    on('[data-act=settings-leave]', () => leaveSettings());
    on('[data-act=settings-save]', () => saveSettings());
    const sf = feed.querySelector('#vms-settings');
    if(sf) sf.querySelectorAll('[name]').forEach(el => { el.addEventListener('input', syncSettingsSave); el.addEventListener('change', syncSettingsSave); });
    on('[data-act=local-pick-settings]', async () => { const st = S.settings; const r = await call(LOCAL_PK, 'iso.pick');
      if(S.settings === st && r.ok && r.result.path){ st.pick = r.result.path; paint(); } });
    on('[data-act=local-pick-create]', async () => { const C = S.create; const r = await call(LOCAL_PK, 'iso.pick');
      if(S.create === C && C && r.ok && r.result.path){ C.localIso = r.result.path; paint(); } });
    on('[data-act=boot-disk]', async () => { const pk = S.host, uuid = S.vm; const r = await call(pk, 'vm.boot_disk', { vm: uuid });
      if(!r.ok) toast(r.error.message || r.error.code); refresh(pk); });
    on('[data-act=snap-create]', () => snapCreate(S.host, S.vm));
    on('[data-snap-revert]', el => snapRevert(S.host, S.vm, el.dataset.snapRevert));
    on('[data-snap-delete]', el => snapDelete(S.host, S.vm, el.dataset.snapDelete));
    on('[data-act=isos]', () => openIsos(S.host));
    on('[data-act=isos-refresh]', () => openIsos(S.host));
    on('[data-act=iso-fetch]', () => isoFetch(S.host));
    on('[data-act=iso-cancel]', el => isoCancel(S.host, el.dataset.job));
    on('[data-iso-delete]', el => isoDelete(S.host, el.dataset.isoDelete));
    feed.querySelectorAll('input[data-act=iso-upload]').forEach(el => { el.onchange = () => { const f = el.files && el.files[0]; if(f) isoUpload(S.host, f); }; });
    on('[data-act=to-host]', () => { S.iso = null; S.access = null; S.screen = 'host'; paint(); });
    on('[data-act=access]', () => openAccess(S.host));
    on('[data-act=access-add]', () => accessAdd());
    on('[data-access-remove]', el => { const A = S.access; if(A && A.allowed){ A.allowed = A.allowed.filter(x => x !== el.dataset.accessRemove); paint(); } });
    on('[data-act=access-save]', () => accessSave());
    // ---- phase 3
    on('[data-act=migrate]', () => { const v = ((S.data[S.host] || {}).vms || []).find(x => x.uuid === S.vm); if(v) openMigrate(S.host, v); });
    on('[data-act=mig-check]', () => migPrecheck(S.host));
    on('[data-act=mig-start]', () => migStart(S.host));
    on('[data-act=mig-cancel]', () => migCancel(S.host));
    on('[data-act=mig-refresh]', () => migPoll(S.host));
    on('[data-act=mig-reclaim]', el => migReclaim(S.host, el.dataset.side));
    feed.querySelectorAll('#vms-mig select, #vms-mig input').forEach(el => { el.onchange = () => { readMigForm(); paint(); }; });
  }

  // ---------------------------------------------------------------- entry
  function render(){
    PC = window.__PC;
    if(!PC){ return setTimeout(render, 50); }
    const pk = me();
    if(!S.purged){ S.purged = true; purgeStoredSessions(); }
    if(pk !== S.pk){
      SESS.clear();                                                  // sign-out or account switch: no key survives it
      closeConsole();
      stopMigWatch();
      Object.assign(S, { pk, hosts: [], data: {}, screen: 'hosts', host: '', vm: '', filter: 'all',
                         doc: { read: false, ok: false, at: 0 }, create: null, busy: {}, mig: null });
      rpc = null;
      if(pk) loadCache();
    }
    if(!pk){
      const feed = document.querySelector('#feed');
      if(feed) feed.innerHTML = '<div class="empty">Sign in to manage virtual machines.</div>';
      return;
    }
    const inst = instanceHost();
    if(inst) mergeHosts([inst], 'instance');
    if(hasLocal() && !hostOf(LOCAL_PK)) S.hosts.unshift({ pubkey: LOCAL_PK, relay: '', name: 'This computer', source: 'local' });
    paint();                                             // cache first, before any network
    startPoll();
    if(S.booted !== pk){
      S.booted = pk;
      for(const h of S.hosts) refresh(h.pubkey);
      readDoc();
    }else{
      for(const h of S.hosts) if(!(S.data[h.pubkey] || {}).inflight) refresh(h.pubkey);
    }
  }

  window.PCVms = {
    render,
    consoleOpen: () => !!S.console,
    closeConsole,
    _state: S,
    _setRpc: r => { rpc = r; },
    _addHost: addHost,
    _call: call,
    _local: LocalHost,
    _findHosts: findHosts,
    _settingsDelta: settingsDelta,
    _consoleShell: consoleShell,
    _net: { netChoices, netDefault, netValue, parseNet },
    _style: () => STYLE,
  };
})();
