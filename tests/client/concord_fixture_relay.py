"""A shared in-page CORD relay for full-app Concord tests (not a test module itself).

`INSTALL` replaces the four transport doors Concord uses -- `__PC.relayQuery`, `__PC.relayQueryFrom`,
`__PC.relayPublishRoom` and `Relay.subscribe/subscribeFrom` -- with one event store that behaves like
a relay where it matters for these tests: filters are honoured (kinds, authors, ids, since/until and
`#tag`s), live subscriptions receive matching events, and EPHEMERAL kinds (20000-29999, e.g. the
CORD-07 presence wrap 21059) are delivered live and never stored, which is what a real relay does
and what makes "a voice row shows who is connected" depend on the live path, not on history.

Two browsers share one community through `pump()`: every event a page publishes is queued in
`__relayOut`, and the pump hands it to the other page's `__relayIn`. Everything else in the pages
-- the minted community, the reader's encryption, the signer, the DOM -- is the real client.
"""
import asyncio

INSTALL = r'''(()=>{
  if(window.__relayInstalled)return;window.__relayInstalled=true;
  const store=[],subs=new Set();window.__relayStore=store;window.__relayOut=[];window.__relayPublished=[];window.__relayEphemeral=[];
  const ephemeral=k=>k>=20000&&k<30000;
  const match=(f,ev)=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.authors||f.authors.includes(ev.pubkey))
    &&(!f.ids||f.ids.includes(ev.id))&&(f.since==null||ev.created_at>=f.since)&&(f.until==null||ev.created_at<=f.until)
    &&Object.keys(f).filter(k=>k[0]==='#').every(k=>(ev.tags||[]).some(t=>t[0]===k.slice(1)&&f[k].includes(t[1])));
  const query=filters=>store.filter(ev=>(filters||[]).some(f=>match(f,ev)));
  function accept(ev,local){
    if(!ev||!ev.id)return;
    if(!ephemeral(ev.kind)){if(store.some(x=>x.id===ev.id))return;store.push(ev);}
    else{if(__relayEphemeral.some(x=>x.id===ev.id))return;__relayEphemeral.push(ev);}
    if(local){__relayOut.push(ev);__relayPublished.push(ev);}
    for(const s of [...subs])if(s.filters.some(f=>match({...f,since:undefined,until:undefined},ev)))try{s.onEvent(ev);}catch(_){}
  }
  window.__relaySeed=events=>{for(const ev of events||[])accept(ev,false);};
  window.__relayIn=ev=>accept(ev,false);
  __PC.relayQuery=async filters=>query(filters);
  __PC.relayQueryFrom=async(_relays,filters)=>query(filters);
  __PC.relayPublishRoom=async(_relays,ev)=>{accept(ev,true);return {ok:true};};
  const subscribe=(filters,opts={})=>{
    const s={filters:filters||[],onEvent:opts.onEvent||(()=>{})};subs.add(s);
    setTimeout(()=>{if(!subs.has(s))return;for(const ev of query(s.filters))try{s.onEvent(ev);}catch(_){}try{opts.onEose&&opts.onEose();}catch(_){}},0);
    const stop=()=>{subs.delete(s);};
    stop.publish=ev=>{accept(ev,true);return 1;};stop.ready=Promise.resolve(true);stop.hasTargets=true;
    return stop;
  };
  Relay.subscribe=(filters,opts)=>subscribe(filters,opts);
  Relay.subscribeFrom=(_relays,filters,opts)=>subscribe(filters,opts);
})()'''

# Mint a REAL community (PosterCord.createCommunity, the reader's own writer) owned by the signed-in
# account, seed the relay with its genesis, save it as this account's only room and render it.
MINT = r'''(async()=>{
  const made=await PosterCord.createCommunity({owner:__PC.me().pubkey,name:NAME,relays:['wss://cord.invalid'],base:'https://cord.invalid',signEvent:__PC.signTemplate});
  const bundle={...PosterCord.openInvite(made.url,made.events).bundle,control_root:made.secrets.controlRoot};
  __relaySeed(made.events);
  const controls=made.events.filter(e=>e.kind===1059);
  const info=PosterCordReader.inspectControl(bundle,controls);
  const room={name:NAME,communityId:made.communityId,naddr:'cord-fixture',url:made.url,channels:info.channels,cord:{bundle,hydrated:true}};
  window.__cordBundle=bundle;window.__cordRoom=room;
  return {room,events:made.events};
})()'''

OPEN_ROOM = r'''((room)=>{
  localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));
  localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
  window.__cordBundle=room.cord.bundle;window.__cordRoom=room;
  PCConcord.render();
})'''

CONTROLS = "__relayStore.filter(e=>e.kind===1059&&PosterCordReader.inspectControl(__cordBundle,[]).controlPubkeys.includes(e.pubkey))"


async def pump(a, b, stop):
    """Carry every event each page publishes to the other, until `stop` is set."""
    while not stop.is_set():
        for src, dst in ((a, b), (b, a)):
            out = await src.js('window.__relayOut?window.__relayOut.splice(0):[]')
            for ev in out or []:
                await dst.js('window.__relayIn&&__relayIn(' + __import__('json').dumps(ev) + ')')
        await asyncio.sleep(.1)
