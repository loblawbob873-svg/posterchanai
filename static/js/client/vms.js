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
 */
(function(){
  const DOC_D = 'pcai:vmhosts';
  const POLL_MS = 10000;
  const S = {
    pk: '', hosts: [], data: {}, screen: 'hosts', host: '', vm: '', filter: 'all',
    doc: { read: false, ok: false, at: 0 }, poll: null, booted: '', console: null, create: null,
    busy: {}, mig: null,
  };
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
    const hosts = S.hosts.filter(h => h.source !== 'instance').map(h => ({ pubkey: h.pubkey, relay: h.relay, name: h.name || '' }));
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

  async function call(pk, op, args, opts){
    const r = await getRpc();
    const h = hostOf(pk);
    if(!r || !h) return { ok: false, noAnswer: true, reason: 'unavailable' };
    return r.call(h, op, args || {}, opts || {});
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
    let pk = null;
    try{ const s = who.trim(); pk = /^[0-9a-f]{64}$/i.test(s) ? s.toLowerCase() : window.NostrTools.nip19.decode(s).data; }catch(_){}
    if(!pk || !/^[0-9a-f]{64}$/.test(pk)){ toast('that is not an npub'); return; }
    const relay = await PC.uiPrompt('The host’s relay', { placeholder: 'wss://example.com/relay', ok: 'Add host' });
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

  async function removeHost(pk){
    const h = hostOf(pk);
    if(!h || h.source === 'instance') return;
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

  async function openCreate(pk){
    S.screen = 'create'; S.create = { isos: null, msg: '', busy: false };
    paint();
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
                   iso: val('iso') || '', autostart: chk('autostart'), start: chk('start') };
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

  // ---------------------------------------------------------------- console
  async function openConsole(pk, vm){
    if(S.console) closeConsole();
    const h = hostOf(pk);
    const el = document.createElement('div');
    el.id = 'vms-console'; el.className = 'vmc';
    el.innerHTML = `<div class="vmc-bar">
        <button class="btn small" data-c="close" aria-label="Close console">✕ Close</button>
        <b class="vmc-name">${esc(vm.name)}</b><span class="vmc-st">Requesting a console…</span>
        <span class="vmc-sp"></span>
        <button class="btn small" data-c="cad">Ctrl+Alt+Del</button>
        <button class="btn small" data-c="fit">1:1</button>
        <button class="btn small" data-c="kbd">Keyboard</button>
        <button class="btn small" data-c="fs">Full screen</button>
      </div><div class="vmc-screen"></div><textarea class="vmc-kbd" autocapitalize="off" autocomplete="off" spellcheck="false" aria-label="On-screen keyboard"></textarea>`;
    document.body.appendChild(el);
    const st = el.querySelector('.vmc-st');
    const setSt = (s, m) => { if(st) st.textContent = m || { connecting: 'Connecting…', connected: 'Connected', closed: 'Closed', error: 'Error' }[s] || s; el.dataset.state = s; };
    const C = S.console = { el, handle: null, pk, uuid: vm.uuid, fit: true };
    el.querySelector('[data-c=close]').onclick = () => closeConsole();
    el.querySelector('[data-c=cad]').onclick = () => { C.handle && C.handle.ctrlAltDel(); };
    el.querySelector('[data-c=fit]').onclick = (e) => { C.fit = !C.fit; C.handle && C.handle.setFit(C.fit); e.currentTarget.textContent = C.fit ? '1:1' : 'Fit'; };
    el.querySelector('[data-c=fs]').onclick = () => { try{ document.fullscreenElement ? document.exitFullscreen() : el.requestFullscreen(); }catch(_){} };
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
    const M = S.mig = { vm: v.uuid, name: v.name, target: '', startAfter: v.state === 'running', force: false,
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
    try{ return rr && await rr.authorize(M.target, 'vm.migrate.authorize', { source: pk, target: M.target, vm: M.vm }); }
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
    const cands = S.hosts.filter(h => h.pubkey !== pk);
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
  function bar(label, used, total, committed){
    if(!total) return '';
    const pct = x => Math.max(0, Math.min(100, Math.round(100 * x / total)));
    return `<div class="vms-bar"><div class="vms-bar-l"><span>${esc(label)}</span><span>${esc(used)} / ${esc(total)}</span></div>
      <div class="vms-bar-t"><i class="vms-bar-used" style="width:${pct(used)}%"></i>${committed != null ? `<i class="vms-bar-com" style="width:${pct(committed)}%"></i>` : ''}</div></div>`;
  }
  const stateLabel = s => ({ running: 'Running', shutoff: 'Off', paused: 'Paused', stopping: 'Stopping', other: 'Unknown' }[s] || s);
  const roleOf = pk => { const d = S.data[pk]; return d && d.whoami && d.whoami.role; };

  function statusLine(pk){
    const d = S.data[pk] || {};
    if(d.status === 'noanswer') return `<div class="vms-noanswer">No answer — the host is offline or you’re not on its list.${d.at ? ' Showing what it said ' + ago(d.at) + '.' : ''} <button class="btn small" data-act="retry" data-host="${esc(pk)}">Retry</button></div>`;
    if(d.status === 'error') return `<div class="vms-noanswer">The host answered with an error: ${esc((d.err && (d.err.message || d.err.code)) || 'unknown')} <button class="btn small" data-act="retry" data-host="${esc(pk)}">Retry</button></div>`;
    if(d.status === 'loading' && !d.at) return `<div class="vms-seen"><span class="spinner spinner-inline"></span>Asking the host…</div>`;
    return `<div class="vms-seen">${d.status === 'stale' || d.inflight ? '<span class="spinner spinner-inline"></span>' : ''}Last answered ${esc(ago(d.at))}</div>`;
  }

  function hostCard(h){
    const d = S.data[h.pubkey] || {};
    const info = d.info || {};
    const role = roleOf(h.pubkey);
    const dot = d.status === 'ok' ? 'ok' : d.status === 'noanswer' || d.status === 'error' ? 'bad' : 'wait';
    const vms = info.vms ? `${info.vms.running}/${info.vms.total} running` : '';
    const caps = info.cpu ? `<div class="vms-host-cap">${esc(info.cpu.cores)} cores · load ${esc(info.cpu.load1)}</div>
      ${bar('RAM MiB', (info.ram.total_mib - info.ram.free_mib), info.ram.total_mib, info.ram.committed_mib)}
      ${bar('Disk GiB', (info.disk.total_gib - info.disk.free_gib), info.disk.total_gib, info.disk.committed_gib)}` : '';
    return `<button class="vms-host${S.host === h.pubkey ? ' sel' : ''}" data-host="${esc(h.pubkey)}">
      <div class="vms-host-top"><span class="vms-dot vms-dot-${dot}"></span><b>${esc(h.name || (d.whoami && d.whoami.host && d.whoami.host.name) || 'VM host')}</b>
      ${role ? `<span class="vms-role vms-role-${esc(role)}">${esc(role)}</span>` : ''}</div>
      <div class="vms-host-sub">${esc(short(h.pubkey))}${h.source === 'instance' ? ' · this server' : ''}</div>
      ${caps}${vms ? `<div class="vms-host-cap">${esc(vms)}</div>` : ''}
      ${statusLine(h.pubkey).replace(/<button[^>]*>Retry<\/button>/, '')}
    </button>`;
  }

  function hostsScreen(){
    if(!S.hosts.length){
      if(!S.doc.read) return `<div class="vms-cold"><div class="spinner"></div><div class="vms-seen">Looking for your VM hosts…</div></div>`;
      return `<div class="empty vms-empty">No VM hosts yet.<br><small>A host is a PosterChan server with VM hosting turned on (Admin → VMs).</small><br><button class="btn btn-ghost" data-act="add">Add a host</button></div>`;
    }
    return `<div class="vms-hosts">${S.hosts.map(hostCard).join('')}</div>
      <div class="vms-actions"><button class="btn btn-ghost small" data-act="add">+ Add host</button></div>`;
  }

  function vmRow(pk, v){
    return `<button class="vms-vm" data-vm="${esc(v.uuid)}">
      <span class="vms-pill vms-st-${esc(v.state)}">${esc(stateLabel(v.state))}</span>
      <b>${esc(v.name)}</b><span class="vms-spec">${esc(v.vcpus)} vCPU · ${esc(v.ram_mib)} MiB${v.disk_gib ? ' · ' + esc(v.disk_gib) + ' GiB' : ''}</span></button>`;
  }

  function hostScreen(pk, wide){
    const h = hostOf(pk);
    if(!h) return hostsScreen();
    const d = S.data[pk] || {};
    const role = roleOf(pk);
    const info = d.info || {};
    let list = (d.vms || []).slice().sort((a, b) => a.name.localeCompare(b.name));
    if(S.filter === 'running') list = list.filter(v => v.state === 'running');
    if(S.filter === 'mine') list = list.filter(v => (v.assigned || []).includes(S.pk));
    let body;
    if(!d.vms){
      body = d.status === 'noanswer' || d.status === 'error' ? '' : `<div class="spinner"></div>`;
    }else if(!list.length){
      body = `<div class="empty vms-empty">${d.vms.length ? 'Nothing matches this filter.'
        : role === 'admin' ? 'No virtual machines on this host yet.' : 'No VMs are assigned to you on this host.'}</div>`;
    }else body = `<div class="vms-list">${list.map(v => vmRow(pk, v)).join('')}</div>`;
    return `<div class="vms-head">${wide ? '' : '<button class="btn small vms-back" data-act="back">‹ Hosts</button>'}
        <h2>${esc(h.name || 'VM host')}</h2>${role ? `<span class="vms-role vms-role-${esc(role)}">${esc(role)}</span>` : ''}
        <span class="vms-sp"></span><button class="btn small" data-act="retry" data-host="${esc(pk)}" aria-label="Refresh">↻</button>
        ${h.source !== 'instance' ? `<button class="btn small" data-act="remove-host" aria-label="Remove host">✕</button>` : ''}</div>
      ${statusLine(pk)}
      ${info.cpu ? `<div class="vms-cap">${bar('RAM MiB', info.ram.total_mib - info.ram.free_mib, info.ram.total_mib, info.ram.committed_mib)}
        ${bar('Disk GiB', info.disk.total_gib - info.disk.free_gib, info.disk.total_gib, info.disk.committed_gib)}
        <div class="vms-host-cap">${esc(info.cpu.cores)} cores · load ${esc(info.cpu.load1)}${info.kvm === false ? ' · <b>no KVM</b>' : ''}</div></div>` : ''}
      <div class="vms-chips">${[['all', 'All'], ['running', 'Running']].concat(role === 'admin' ? [['mine', 'Mine']] : [])
        .map(([k, l]) => `<button class="vms-chip${S.filter === k ? ' on' : ''}" data-filter="${k}">${l}</button>`).join('')}</div>
      ${body}
      ${role === 'admin' ? `<div class="vms-actions"><button class="btn btn-neon" data-act="create">+ Create VM</button></div>` : ''}`;
  }

  function vmScreen(pk, uuid){
    const d = S.data[pk] || {};
    const v = (d.vms || []).find(x => x.uuid === uuid);
    const h = hostOf(pk);
    if(!v) return `<div class="vms-head"><button class="btn small vms-back" data-act="back">‹ ${esc((h && h.name) || 'Host')}</button></div><div class="empty">This VM is not in the host’s last answer.</div>`;
    const role = roleOf(pk);
    const running = v.state === 'running' || v.state === 'paused';
    const b = (act, label, cls, on) => `<button class="btn ${cls || ''}" data-power="${act}" ${on && !S.busy[uuid + ':' + act] ? '' : 'disabled'}>${S.busy[uuid + ':' + act] ? '…' : esc(label)}</button>`;
    return `<div class="vms-head"><button class="btn small vms-back" data-act="back">‹ ${esc((h && h.name) || 'Host')}</button></div>
      <div class="vms-vmhead"><h2>${esc(v.name)}</h2><span class="vms-pill vms-st-${esc(v.state)}">${esc(stateLabel(v.state))}</span>${v.migration && v.migration.state ? `<span class="vms-pill vms-st-paused">migrating (${esc(v.migration.state)})</span>` : ''}</div>
      ${statusLine(pk)}
      <div class="vms-specs"><div><span>vCPUs</span><b>${esc(v.vcpus)}</b></div><div><span>Memory</span><b>${esc(v.ram_mib)} MiB</b></div>
        <div><span>Disk</span><b>${v.disk_gib ? esc(v.disk_gib) + ' GiB' : '—'}</b></div><div><span>Guest</span><b>${esc(v.guest || '—')} ${esc(v.firmware || '')}</b></div>
        <div><span>Autostart</span><b>${v.autostart ? 'on' : 'off'}</b></div></div>
      ${role === 'admin' ? `<div class="vms-assign"><div class="vms-sub">Assigned to</div>
        ${(v.assigned || []).map(p => `<div class="vms-assignee"><span>${esc(short(p))}</span><button class="btn small" data-unassign="${esc(p)}">Remove</button></div>`).join('') || '<div class="vms-seen">Nobody — only admins can use it.</div>'}
        <button class="btn btn-ghost small" data-act="assign">+ Assign to an npub</button></div>
        <div class="vms-actions"><button class="btn btn-ghost small" data-act="migrate">${v.migration && v.migration.state ? 'Migration status' : 'Migrate…'}</button></div>
        <div class="vms-danger"><button class="btn btn-red small" data-act="delete" ${v.state === 'shutoff' ? '' : 'disabled title="Shut it down first"'}>Delete VM</button></div>` : ''}
      <div class="vms-actions vms-power">
        ${b('start', 'Start', 'btn-neon', !running)}${b('shutdown', 'Shut down', '', running)}${b('reboot', 'Reboot', '', running)}${b('destroy', 'Force off', 'btn-red', running)}
        <button class="btn btn-cyan" data-act="console" ${v.state === 'running' ? '' : 'disabled'}>Console</button>
      </div>`;
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
        <label>Installer ISO${isos == null ? ' <span class="spinner spinner-inline"></span>' : ''}<select class="input" name="iso"><option value="">No installer</option>${(isos || []).map(i => `<option value="${esc(i.id)}">${esc(i.name)}</option>`).join('')}</select></label>
        <label class="vms-check"><input type="checkbox" name="start" checked> Start it after creating</label>
        <label class="vms-check"><input type="checkbox" name="autostart"> Start with the host</label>
        <div class="vms-createmsg">${esc(C.msg || '')}</div>
        <div class="vms-actions"><button class="btn btn-neon" data-act="submit-create" ${C.busy ? 'disabled' : ''}>Create VM</button></div>
      </form>`;
  }

  function paintCreateMsg(){ const m = document.querySelector('#vms-create .vms-createmsg'); if(m && S.create) m.textContent = S.create.msg || ''; }

  const STYLE = `
.vms{display:flex;flex-direction:column;gap:12px;padding:12px 16px 90px;min-height:100%;box-sizing:border-box}
.vms.vms-wide{flex-direction:row;align-items:flex-start;padding-bottom:24px}
.vms-rail{flex:0 0 300px;display:flex;flex-direction:column;gap:10px;position:sticky;top:0}
.vms-main{flex:1;min-width:0;display:flex;flex-direction:column;gap:12px}
.vms-hosts{display:flex;flex-direction:column;gap:10px}
.vms-host,.vms-vm{all:unset;box-sizing:border-box;display:block;cursor:pointer;background:var(--panel2);border:1px solid var(--line);border-radius:var(--r);padding:12px 14px;color:var(--text);width:100%}
.vms-host.sel{border-color:var(--neon)}
.vms-host-top,.vms-head,.vms-vmhead{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.vms-head h2,.vms-vmhead h2{margin:0;font-size:20px;overflow-wrap:anywhere}
.vms-sp{flex:1}
.vms-host-sub,.vms-host-cap,.vms-seen,.vms-spec,.vms-sub{color:var(--muted);font-size:13px}
.vms-dot{width:10px;height:10px;border-radius:50%;display:inline-block;background:var(--muted)}
.vms-dot-ok{background:var(--green)}.vms-dot-bad{background:var(--amber)}
.vms-role{font-size:11px;text-transform:uppercase;letter-spacing:.06em;border:1px solid var(--line);border-radius:99px;padding:1px 8px;color:var(--neon)}
.vms-bar{margin-top:6px}.vms-bar-l{display:flex;justify-content:space-between;font-size:12px;color:var(--muted)}
.vms-bar-t{position:relative;height:6px;border-radius:3px;background:var(--line);overflow:hidden}
.vms-bar-t i{position:absolute;left:0;top:0;bottom:0;border-radius:3px}
.vms-bar-used{background:var(--neon);z-index:2}.vms-bar-com{background:rgba(var(--accent2-rgb),.45);z-index:1}
.vms-noanswer{background:rgba(255,207,43,.1);border:1px solid rgba(255,207,43,.4);border-radius:var(--r-sm);padding:8px 10px;font-size:14px}
.vms-list{display:flex;flex-direction:column;gap:8px}
.vms-vm{display:grid;grid-template-columns:auto 1fr;grid-template-rows:auto auto;column-gap:10px;align-items:center}
.vms-vm .vms-pill{grid-row:1/3}.vms-vm b{overflow-wrap:anywhere}
.vms-pill{font-size:12px;border-radius:99px;padding:2px 9px;border:1px solid var(--line);white-space:nowrap}
.vms-st-running{color:var(--green);border-color:var(--green)}.vms-st-paused,.vms-st-stopping{color:var(--amber);border-color:var(--amber)}
.vms-chips{display:flex;gap:6px;flex-wrap:wrap}
.vms-chip{all:unset;cursor:pointer;border:1px solid var(--line);border-radius:99px;padding:4px 12px;font-size:13px}
.vms-chip.on{border-color:var(--neon);color:var(--neon)}
.vms-specs{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px}
.vms-specs>div{background:var(--panel2);border:1px solid var(--line);border-radius:var(--r-sm);padding:8px 10px;display:flex;flex-direction:column}
.vms-specs span{color:var(--muted);font-size:12px}
.vms-assign{display:flex;flex-direction:column;gap:6px}.vms-assignee{display:flex;justify-content:space-between;align-items:center;gap:8px}
.vms-actions{display:flex;gap:8px;flex-wrap:wrap}
.vms-form{display:flex;flex-direction:column;gap:10px}.vms-form label{display:flex;flex-direction:column;gap:4px;font-size:14px;flex:1;min-width:0}
.vms-row{display:flex;gap:10px;flex-wrap:wrap}.vms-row label{min-width:120px}
.vms-form label.vms-check{flex-direction:row;align-items:center;gap:8px}
.vms-createmsg{color:var(--muted);min-height:1em}
.vms-cold{display:flex;flex-direction:column;align-items:center}
@media (max-width:1023px){.vms:not(.vms-wide) .vms-actions{position:sticky;bottom:0;background:var(--canvas);padding:10px 0;z-index:3}}
.vms-mig{display:flex;flex-direction:column;gap:10px}
.vms-mig-bar{position:relative;height:10px;border-radius:5px;background:var(--line);overflow:hidden}
.vms-mig-bar i{position:absolute;left:0;top:0;bottom:0;background:var(--neon);border-radius:5px;transition:width .3s}
.vms-mig-side{display:flex;align-items:center;gap:8px;flex-wrap:wrap;overflow-wrap:anywhere}
.vms-mig-pre,.vms-mig-done{background:rgba(var(--accent2-rgb),.1);border:1px solid var(--line);border-radius:var(--r-sm);padding:8px 10px;font-size:14px}
.vms-mig-locked{background:rgba(255,80,80,.1);border:1px solid rgba(255,80,80,.5);border-radius:var(--r-sm);padding:10px;font-size:14px;display:flex;flex-direction:column;gap:8px}
.vmc{position:fixed;inset:0;z-index:10050;background:#000;display:flex;flex-direction:column}
.vmc-bar{display:flex;gap:6px;align-items:center;padding:6px 8px;background:var(--bg2);color:var(--text);flex-wrap:wrap}
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
    if(S.screen === 'vm' && S.host) main = vmScreen(S.host, S.vm);
    else if(S.screen === 'create' && S.host) main = createScreen(S.host);
    else if(S.screen === 'migrate' && S.host && S.mig) main = migrateScreen(S.host);
    else if((S.screen === 'host' || wide) && S.host && hostOf(S.host)) main = hostScreen(S.host, wide);
    else main = hostsScreen();
    // Keep what somebody is typing in the create form across a background repaint.
    const keep = {};
    const form = feed.querySelector('#vms-create');
    if(form) form.querySelectorAll('[name]').forEach(el => { keep[el.name] = el.type === 'checkbox' ? el.checked : el.value; });
    const scroll = feed.scrollTop;
    feed.innerHTML = wide
      ? `<div class="vms vms-wide"><aside class="vms-rail"><div class="vms-head"><h2>VM hosts</h2><span class="vms-sp"></span><button class="btn small" data-act="add">+ Add</button></div>
           ${S.hosts.length ? S.hosts.map(hostCard).join('') : (S.doc.read ? '<div class="vms-seen">No hosts yet.</div>' : '<div class="spinner"></div>')}</aside>
         <section class="vms-main">${S.hosts.length ? main : hostsScreen()}</section></div>`
      : `<div class="vms">${main}</div>`;
    const nf = feed.querySelector('#vms-create');
    if(nf) for(const k in keep){ const el = nf.querySelector('[name="' + k + '"]'); if(el){ if(el.type === 'checkbox') el.checked = keep[k]; else el.value = keep[k]; } }
    feed.scrollTop = scroll;
    bind(feed);
  }

  function bind(feed){
    const on = (sel, fn) => feed.querySelectorAll(sel).forEach(el => { el.onclick = (e) => { e.preventDefault(); fn(el, e); }; });
    on('[data-host]:not([data-act])', el => { S.host = el.dataset.host; S.screen = 'host'; S.vm = ''; S.filter = 'all'; paint(); refresh(S.host); });
    on('[data-act=retry]', el => { refresh(el.dataset.host || S.host); paint(); });
    on('[data-act=add]', () => addHost());
    on('[data-act=remove-host]', () => removeHost(S.host));
    on('[data-act=back]', () => {
      if(S.screen === 'migrate'){ stopMigWatch(); S.mig = null; S.screen = 'vm'; }
      else if(S.screen === 'vm' || S.screen === 'create'){ S.screen = 'host'; S.vm = ''; S.create = null; }
      else { S.screen = 'hosts'; if(!isWide(feed)) S.host = ''; }
      paint();
    });
    on('[data-filter]', el => { S.filter = el.dataset.filter; paint(); });
    on('[data-vm]', el => { S.vm = el.dataset.vm; S.screen = 'vm'; paint(); });
    on('[data-power]', el => power(S.host, S.vm, el.dataset.power));
    on('[data-act=console]', () => { const v = ((S.data[S.host] || {}).vms || []).find(x => x.uuid === S.vm); if(v) openConsole(S.host, v); });
    on('[data-act=assign]', () => assign(S.host, S.vm));
    on('[data-unassign]', el => unassign(S.host, S.vm, el.dataset.unassign));
    on('[data-act=delete]', () => { const v = ((S.data[S.host] || {}).vms || []).find(x => x.uuid === S.vm); if(v) del(S.host, v); });
    on('[data-act=create]', () => openCreate(S.host));
    on('[data-act=submit-create]', () => submitCreate(S.host));
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
    if(pk !== S.pk){
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
  };
})();
