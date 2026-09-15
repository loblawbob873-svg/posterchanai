/* CORD-05 §6: person-addressed NIP-59 invitations. No subscriptions or membership writes. */
(function () {
  'use strict';
  const hexKey = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
  const bytes = value => new TextEncoder().encode(value);
  function current(context) {
    if (!context || !hexKey(context.pubkey) || (context.isCurrent && !context.isCurrent()))
      throw new Error('The signed-in account changed during the invitation');
  }
  function validate(bundle, options) {
    const reader = globalThis.PosterCordReader;
    if (!reader || typeof reader.validateInviteBundle !== 'function')
      throw new Error('Concord invitation validation is not ready');
    if(!bundle||typeof bundle!=='object'||!Array.isArray(bundle.channels)||bundle.channels.length>256)
      throw new Error('Invalid or oversized Concord invitation');
    // Invitations grant current capabilities, not internal membership history/provenance.
    const invite={};
    for(const field of ['community_id','owner','owner_salt','community_root','root_epoch','control_pk','channels','relays','name','icon','expires_at','creator_npub','label'])
      if(bundle[field]!==undefined)invite[field]=bundle[field];
    invite.channels=bundle.channels.map(channel=>{
      if(!channel||typeof channel!=='object')throw new Error('Invalid invitation channel');
      const result={};for(const field of ['id','key','epoch','name'])if(channel[field]!==undefined)result[field]=channel[field];return result;
    });
    return reader.validateInviteBundle(invite, options);
  }
  async function rumorId(event) {
    const wire = JSON.stringify([0, event.pubkey, event.created_at, event.kind, event.tags, event.content]);
    const digest = await crypto.subtle.digest('SHA-256', bytes(wire));
    return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('');
  }
  async function verified(event, context) {
    // A verifier's memo flag must never survive mutation of an event received from a caller.
    const copy = JSON.parse(JSON.stringify(event));
    const valid = await context.verify([copy]);
    current(context);
    if (!Array.isArray(valid) || !valid.some(row => row.id === copy.id))
      throw new Error('The invitation has an invalid event signature');
    return copy;
  }
  async function open(wrap, context, options = {}) {
    current(context);
    if (!wrap || wrap.kind !== 1059 || !Array.isArray(wrap.tags) ||
        !wrap.tags.some(tag => tag[0] === 'p' && tag[1] === context.pubkey) ||
        typeof wrap.content !== 'string' || bytes(wrap.content).length > 131072)
      throw new Error('This invitation is not addressed to the signed-in account');
    const outer = await verified(wrap, context);
    const seal = JSON.parse(await context.decrypt(outer.pubkey, outer.content));
    current(context);
    if (!seal || seal.kind !== 13 || !hexKey(seal.pubkey))
      throw new Error('The invitation is not a standard NIP-59 gift wrap');
    await verified(seal, context);
    const rumor = JSON.parse(await context.decrypt(seal.pubkey, seal.content));
    current(context);
    if (!rumor || rumor.kind !== 3313 || rumor.pubkey !== seal.pubkey ||
        !Number.isSafeInteger(rumor.created_at) || rumor.created_at < 0 ||
        !Array.isArray(rumor.tags) || typeof rumor.content !== 'string' ||
        rumor.id !== await rumorId(rumor))
      throw new Error('The invitation has an invalid signed rumor');
    current(context);
    const bundle = validate(globalThis.PosterCordReader.parseJoinMaterial(rumor.content), { forJoin: !!options.forJoin, now: options.now });
    return { id: outer.id, rumorId: rumor.id, inviter: seal.pubkey, createdAt: rumor.created_at, bundle };
  }
  async function create(input, recipient, context, options = {}) {
    current(context);
    if (!hexKey(recipient)) throw new Error('Choose a valid invitation recipient');
    // A membership vault is not an invite. Never accidentally hand a normal invitee staff keys.
    if (input && ['control_root', 'held_roots', 'seed', 'removed', 'removed_channels', 'root_refounder', 'held_keys'].some(key => Object.hasOwn(input, key)))
      throw new Error('Remove private membership and staff material before creating an invitation');
    const bundle = validate(input, { forJoin: true, now: options.now });
    const now = Math.floor((options.now ?? Date.now()) / 1000);
    const rumor = { pubkey: context.pubkey, created_at: now, kind: 3313, tags: [], content: globalThis.PosterCordReader.stringifyJoinMaterial(bundle) };
    rumor.id = await rumorId(rumor);
    const content = await context.encrypt(recipient, JSON.stringify(rumor));
    current(context);
    const seal = await context.sign({ kind: 13, created_at: now - Math.floor(Math.random() * 172800), tags: [], content });
    current(context);
    if (seal.pubkey !== context.pubkey) throw new Error('The signer returned a different invitation author');
    await verified(seal, context);
    const expiry = Number.isSafeInteger(bundle.expires_at) ? Math.ceil(bundle.expires_at / 1000) : undefined;
    const wrap = await context.wrapSeal(seal, recipient, { rumorKind: 3313, expiration: expiry });
    current(context);
    await verified(wrap, context);
    if (wrap.kind !== 1059 || !wrap.tags.some(t => t[0] === 'p' && t[1] === recipient) ||
        !wrap.tags.some(t => t[0] === 'k' && t[1] === '3313') ||
        (expiry !== undefined && !wrap.tags.some(t => t[0] === 'expiration' && t[1] === String(expiry))))
      throw new Error('The signer returned an invalid invitation envelope');
    return { wrap, rumorId: rumor.id, bundle };
  }
  function storageKey(context){ current(context); return 'pc.concord.direct.v1.'+context.pubkey; }
  function readPending(context){
    try{
      const text=localStorage.getItem(storageKey(context))||'';
      if(text.length>4*1024*1024)return {wraps:[],dismissed:[]};
      const value=JSON.parse(text||'{}');
      return {wraps:(Array.isArray(value.wraps)?value.wraps:[]).slice(0,32),
              dismissed:(Array.isArray(value.dismissed)?value.dismissed:[]).filter(hexKey).slice(0,200)};
    }catch(_){return {wraps:[],dismissed:[]};}
  }
  function pending(context){current(context);return readPending(context).wraps.filter(w=>w&&hexKey(w.id));}
  function dismiss(id,context){
    current(context);if(!hexKey(id))throw new Error('Invalid invitation ID');
    const state=readPending(context);
    state.wraps=state.wraps.filter(w=>w&&w.id!==id);
    state.dismissed=[id,...state.dismissed.filter(other=>other!==id)].slice(0,200);
    localStorage.setItem(storageKey(context),JSON.stringify(state));
  }
  async function park(wrap,context){
    current(context);
    if(!wrap||!hexKey(wrap.id))throw new Error('Invalid invitation ID');
    const before=readPending(context);
    if(before.dismissed.includes(wrap.id)||before.wraps.some(w=>w&&w.id===wrap.id))return false;
    await open(wrap,context);current(context);
    // Persist only the recipient-encrypted giftwrap, never plaintext community keys.
    const state=readPending(context);
    if(state.dismissed.includes(wrap.id)||state.wraps.some(w=>w&&w.id===wrap.id))return false;
    state.wraps=[JSON.parse(JSON.stringify(wrap)),...state.wraps].slice(0,32);
    while(JSON.stringify(state).length>3*1024*1024)state.wraps.pop();
    localStorage.setItem(storageKey(context),JSON.stringify(state));
    return true;
  }
  globalThis.PCCordDirectInvites = Object.freeze({ open, create, pending, park, dismiss });
})();
