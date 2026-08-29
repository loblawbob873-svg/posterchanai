import fs from 'node:fs';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';

const data = new Map();
globalThis.localStorage = {
  getItem: key => data.has(key) ? data.get(key) : null,
  setItem: (key, value) => data.set(key, String(value)),
};

const makeClassList = () => ({added:[],removed:[],values:new Set(),add(...x){this.added.push(...x);x.forEach(v=>this.values.add(v));},remove(...x){this.removed.push(...x);x.forEach(v=>this.values.delete(v));},contains(x){return this.values.has(x);},toggle(x,on){const yes=on===undefined?!this.values.has(x):!!on;yes?this.values.add(x):this.values.delete(x);return yes;}});
const classList = makeClassList();
let feedHtml='',replaceComposerOnWrite=false,feedWrites=0;
const feedListeners=new Map();
const feed = { classList, insertAdjacentHTML(){},contains(node){return [...controls.values()].includes(node);},
  addEventListener(name,fn){feedListeners.set(name,fn);},
  get innerHTML(){return feedHtml;}, set innerHTML(value){feedWrites++;feedHtml=String(value);if(replaceComposerOnWrite){const old=controls.get('cc-input');if(old){old.isConnected=false;if(document.activeElement===old)document.activeElement=document.body;}controls.delete('cc-input');const textarea=feedHtml.match(/<textarea id="cc-input" data-cc-draft-key="([^"]*)"[^>]*>([\s\S]*?)<\/textarea>/);if(textarea){const next=control('cc-input');next.dataset.ccDraftKey=textarea[1];next.value=textarea[2];}}} };
const controls = new Map();
function control(id){
  if(!controls.has(id)) controls.set(id, { id, value:'', dataset:{}, selectionStart:0,selectionEnd:0,selectionDirection:'none',isConnected:true,classList:makeClassList(),
    focus(){document.activeElement=this;},setSelectionRange(start,end,direction='none'){this.selectionStart=start;this.selectionEnd=end;this.selectionDirection=direction;},
    setRangeText(value,start,end,mode){this.value=this.value.slice(0,start)+value+this.value.slice(end);this.selectionStart=this.selectionEnd=mode==='end'?start+value.length:start;},
    addEventListener(name,fn){this['_listener_'+name]=fn;},dispatchEvent(event){this['_listener_'+event.type]?.(event);},click(){ return this.onclick?.(); } });
  return controls.get(id);
}
const dollar = selector => selector === '#feed' ? feed : control(selector.slice(1));
const actionControls = new Map();
const messageRows = new Map();
function messageRow(id){
  if(!messageRows.has(id))messageRows.set(id,{dataset:{messageId:id},classList:makeClassList(),querySelector(sel){return sel==='[data-cc-actions]'?actionControls.get('data-cc-actions:'+id+':'):null;}});
  return messageRows.get(id);
}
const dataKey = selector => selector.match(/^\[data-cc-([\w-]+)\]$/)?.[1];
const camel = value => value.replace(/-([a-z])/g,(_m,c)=>c.toUpperCase());
const dollars = selector => {
  if(selector==='.cc-message.cc-actions-open')return [...messageRows.values()].filter(row=>row.classList.contains('cc-actions-open'));
  const key=dataKey(selector); if(!key)return [];
  const attribute='data-cc-'+key,pattern=new RegExp(attribute+'="([^"]*)"(?:[^>]*data-cc-emoji="([^"]*)")?','g');
  const found=[]; let match;
  while((match=pattern.exec(feed.innerHTML))){
    const rowId=match[1],identity=attribute+':'+rowId+':'+(match[2]||''),button=actionControls.get(identity)||{dataset:{},disabled:false,attributes:{},setAttribute(k,v){this.attributes[k]=String(v);}};
    button.dataset[camel('cc-'+key)]=rowId; if(match[2])button.dataset.ccEmoji=match[2];
    button.closest=sel=>sel==='.cc-message'?messageRow(rowId):null;
    actionControls.set(identity,button); found.push(button);
  }
  return found;
};
const calls = {toasts:[], notified:0, group:0, mentions:[], wraps:[], publishTargets:[], queryTargets:[], profiles:[], discoveryOpened:0, discoveryClosed:0};
let activeView = 'concord';
const JOIN_URL='https://armada.buzz/invite/naddr1pppp#abc_DEF';
const JOIN_BUNDLE={community_id:'1'.repeat(64),owner:'2'.repeat(64),owner_salt:'3'.repeat(64),
  community_root:'4'.repeat(64),root_epoch:0,channels:[],relays:['wss://relay.example'],name:'Joined Armada Room'};
const relayFixtures=filters=>{
  const kinds=(Array.isArray(filters)?filters:[]).flatMap(f=>f.kinds||[]);
  if(kinds.includes(33301)) return [{id:'bundle-event',kind:33301,created_at:10}];
  if(kinds.includes(1059)) return [{id:'control-or-chat-wrap',kind:1059,created_at:11}];
  return [];
};

globalThis.window = globalThis;
const concordEnvelopeCache=new Map();
window.PCConcordCache={
  MAX_ICON_BYTES:5*1024*1024,
  async get(key){return concordEnvelopeCache.get(key)||[];},
  async page(key,{limit}={}){const events=concordEnvelopeCache.get(key)||[];return{events:events.slice(-Number(limit||300))};},
  async put(key,events){concordEnvelopeCache.set(key,[...(concordEnvelopeCache.get(key)||[]),...(events||[])]);},
  async getIcon(){return null;},async putIcon(){},
};
/* Production loads webxdc.js immediately before concord.js. This harness isolates Concord, so
 * provide that preceding module's public topic seam rather than testing an impossible load order. */
window.PCWebxdc={
  async deriveUrlTopic(url,messageId){
    const raw=String(url),lower=raw.toLowerCase();let source=raw;
    for(let at=lower.lastIndexOf('.xdc');at>=0;at=lower.lastIndexOf('.xdc',at-1)){const next=raw[at+4];if(next===undefined||next==='?'||next==='#'||/\s/.test(next)){source=raw.slice(0,at+4);break;}}
    const bytes=new Uint8Array(await webcrypto.subtle.digest('SHA-256',new TextEncoder().encode(`webxdc-url-realtime-v1:${source}:${messageId}`))),alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';let out='',buf=0,bits=0;
    for(const b of bytes){buf=(buf<<8)|b;bits+=8;while(bits>=5){bits-=5;out+=alphabet[(buf>>>bits)&31];}}if(bits)out+=alphabet[(buf<<(5-bits))&31];return out;
  },
  mintTopic(){return 'A'.repeat(52);},
};
const documentListeners=new Map(),windowListeners=new Map();
window.addEventListener=(name,fn)=>windowListeners.set(name,fn);
window.removeEventListener=(name,fn)=>{if(windowListeners.get(name)===fn)windowListeners.delete(name);};
window.__PC = {
  $:dollar, $$:dollars, enc:s=>String(s), niceNip05:s=>s,
  isView:view=>view===activeView,
  viewer:()=>({pubkey:'a'.repeat(64),npub:'npub1testidentity',profile:{name:'tester',display_name:'Test User'}}),
  toast:s=>calls.toasts.push(String(s)),
  openEmojiPopover:(_anchor,pick)=>pick('😀',()=>{}),
  insertAt:(input,value)=>{ input.value+=value; },
  uploadBlob:async file=>'https://files.example/'+file.name,
  askOsNotify:async()=>{ calls.notified++; return 'granted'; },
  startGroupCall:peers=>{ calls.group++; calls.groupPeers=peers; },
  copyValue:value=>{ calls.copied=value; },
  openProfile:pk=>{ calls.profiles.push(pk); },
  osNotify:(title,body,opts)=>{ calls.mentions.push({title,body,opts}); },
  relaySubscribe:(_filters,handlers)=>{calls.discoveryOpened++;calls.discoveryHandlers=handlers;return{close(){calls.discoveryClosed++;}};},
  relayQuery:async filters=>relayFixtures(filters),
  relayQueryFrom:async(relays,filters,options={})=>{calls.queryTargets.push([...relays]);calls.queryOptions=(calls.queryOptions||[]).concat(options);return relayFixtures(filters);},
  relayUrls:()=>['wss://relay.example'], signTemplate:async template=>template,
  relayPublish:async()=>({ok:true}), relayPublishTo:async(relays,event)=>{calls.publishTargets.push([...relays]);calls.wraps.push(event);return 1;},
  publish:async()=>({}),
  switchView:()=>{ throw new Error('Communities tab used the desktop app router'); },
  switchMessagesTab:view=>{ calls.messagesTab=view; activeView=view; },
  profOf:()=>({}), LOGO:'', linkify:s=>String(s), linkCardHtml:()=>'', hydrateLinkCards:()=>{},
};
globalThis.location={origin:'https://poster.place'};
window.PosterCord={
  createCommunity:async()=>({communityId:'c'.repeat(64),generalChannelId:'d'.repeat(64),events:[{}],url:'https://poster.place/invite/naddr1qqqq#abc_DEF',secrets:{},bundle:{relays:['wss://relay.example']}}),
  inviteDetails:()=>({linkSigner:'5'.repeat(64),bootstrapRelays:['wss://relay.example']}),
  openInvite:()=>({bundle:JOIN_BUNDLE,parsed:{linkSigner:'5'.repeat(64)}}),
};
window.PosterCordReader={
  inspectControl:(bundle,wraps)=>wraps.length
    ? {name:'Joined Armada Room',description:'Loaded immediately',icon:bundle&&bundle.slowIcon||'🛸',channels:bundle&&bundle.noGeneral?[
        {id:'joined-lounge',name:'lounge',private:false,streamPubkeys:['9'.repeat(64)]},
        {id:'joined-later',name:'later',private:false,streamPubkeys:['0'.repeat(64)]},
      ]:[
        {id:'joined-general',name:'general',private:false,streamPubkeys:['6'.repeat(64)]},
        {id:'joined-support',name:'support',private:false,streamPubkeys:['7'.repeat(64)]},
      ]}
    : {controlPubkeys:['8'.repeat(64)],channels:[]},
  inspectChat:async(_bundle,_controls,channel)=>({messages:[{id:'joined-message-'+channel,pubkey:'b'.repeat(64),text:'joined history '+channel,at:12,kind:9,tags:[]}],reactions:[],reactionIds:[]}),
  createMetadataWrap:async()=>({wrap:{kind:1059}}),
  createChatWrap:async(_bundle,_wraps,_channel,text,_author,_sign,tags,kind)=>{calls.lastChat={text,tags,kind};return {rumorId:'f'.repeat(64),wrap:{kind:1059},ms:1234};},
};
globalThis.document = {
  body:{classList,isConnected:true}, head:{appendChild(){}},
  documentElement:{appendChild(){},isConnected:true},activeElement:null,
  createElement:()=>({dataset:{}}),
  querySelector:s=>s==='link[data-concord-css]'?{}:(s==='#cc-input'?controls.get('cc-input')||null:null),
  addEventListener(name,fn){documentListeners.set(name,fn);},
  removeEventListener(name,fn){if(documentListeners.get(name)===fn)documentListeners.delete(name);},
};

const concordSource=process.argv[2]
  ? new URL(`file://${process.cwd()}/${process.argv[2].replace(/^\/+/, '')}`)
  : new URL('../../static/js/client/concord.js', import.meta.url);
vm.runInThisContext(fs.readFileSync(concordSource, 'utf8'),
                    {filename:'concord.js'});
const exactRoomRelays=PCConcord.roomRelays({relays:['wss://relay.poster.place']});
if(JSON.stringify(exactRoomRelays)!==JSON.stringify(['wss://relay.poster.place']))
  throw new Error('room relay was polluted with global Concord defaults: '+JSON.stringify(exactRoomRelays));
if(!PCConcord.roomRelays({relays:[]}).length)throw new Error('legacy room lost bootstrap fallback');
if(JSON.stringify(PCConcord.roomRelays({relays:['wss://relay.ditto.pub/','wss://relay.damus.io/']}))!==
   JSON.stringify(['wss://relay.ditto.pub','wss://relay.damus.io']))
  throw new Error('explicit invite/room relays were incorrectly filtered');

const publicLinks=PCConcord.discoverInvites('Join us https://armada.buzz/invite/naddr1qqqq#abc_DEF',{created_at:1});
if(publicLinks.length!==1 || publicLinks[0].name!=='Join us') throw new Error('public discovery parser failed');
const alice={id:'root',pubkey:'b'.repeat(64),tags:[]};
const bob={id:'child',pubkey:'c'.repeat(64),reply:{id:'root'},tags:[['e','root']]};
const carol={id:'sibling',pubkey:'d'.repeat(64),reply:{id:'root'},tags:[['e','root']]};
const stranger={id:'elsewhere',pubkey:'e'.repeat(64),tags:[]};
const participants=PCConcord.threadParticipants([alice,bob,carol,stranger],bob,'a'.repeat(64));
if(participants.length!==3 || !participants.includes(alice.pubkey) ||
   !participants.includes(bob.pubkey) || !participants.includes(carol.pubkey) ||
   participants.includes(stranger.pubkey))
  throw new Error('whole-thread participant inheritance failed');
if(PCConcord.conversationIsVisible(true,false,false) ||
   PCConcord.conversationIsVisible(true,true,true) ||
   !PCConcord.conversationIsVisible(true,true,false) ||
   !PCConcord.conversationIsVisible(false,false,false))
  throw new Error('mobile conversation visibility/read-state rule is wrong');
if(PCConcord.repaintScrollTop(false,420,2400)!==420 ||
   PCConcord.repaintScrollTop(true,420,2400)!==2400)
  throw new Error('Concord repaint moved an unpinned reader or failed to follow a pinned room');
const samePending=[
  {id:'pending-1',pending:true,pubkey:'a',text:'same',kind:9,at:1000},
  {id:'pending-2',pending:true,pubkey:'a',text:'same',kind:9,at:2000},
];
if(PCConcord.pendingEchoMatch([samePending[1]],{pubkey:'a',text:'same',kind:9,at:1900})!==samePending[1])
  throw new Error('relay echo did not reconcile one unambiguous pending send');
if(PCConcord.pendingEchoMatch(samePending,{pubkey:'a',text:'same',kind:9,at:1900})!==null)
  throw new Error('multiple identical pending sends were guessed and could collapse a real send');
if(PCConcord.pendingEchoMatch(samePending,{pubkey:'a',text:'same',kind:9,at:1500})!==null)
  throw new Error('ambiguous identical relay echo guessed and could collapse a real send');
const xdcUrl='https://files.example/game.xdc';
/* Armada/Vector's bare-link contract derives the Iroh topic from URL + message id. */
const armadaChannel='ab'.repeat(32),armadaRoom={cord:{bundle:{}},communityId:'community-1',naddr:'naddr-1',channels:[{id:armadaChannel,name:'general'}]};
const armadaRaw=PCConcord.webxdcOf({id:'raw-event',text:xdcUrl,tags:[['imeta',`url ${xdcUrl}`,'m application/x-webxdc']]},armadaRoom,'general');
if(armadaRaw.uuid!==''||!armadaRaw.urlTopicMessageId||armadaRaw.transport.channelId!==armadaChannel)
  throw new Error('raw Webxdc link was assigned a private room-scope topic');
const rawTopic=await PCConcord.deriveWebxdcUrlTopic(xdcUrl,'raw-event');
if(rawTopic!=='TXCRSLERCKAST3RD65EPSQ2EKHOMV32VBEY5HW7YXIIJX5T46BQA')
  throw new Error('raw Webxdc URL topic does not match Armada SHA-256 fixture');
if(await PCConcord.deriveWebxdcUrlTopic('https://x.org/app.xdc','msg1')!=='QOLP2F6LAQZYTDGUKNGHFGESA5Q3P2HTWCTBZHBOQMLZ2DYHQDPQ')
  throw new Error('raw topic does not match Vector fixture');
const bare=await PCConcord.deriveWebxdcUrlTopic('https://x.org/app.xdc','msg1');
if(await PCConcord.deriveWebxdcUrlTopic('https://x.org/app.xdc?v=2','msg1')!==bare||await PCConcord.deriveWebxdcUrlTopic('https://x.org/app.xdc#top','msg1')!==bare)
  throw new Error('query/hash changed the Vector bare-link topic');
if(await PCConcord.deriveWebxdcUrlTopic('https://cdn.xdc.io/game.xdc','msg1')===await PCConcord.deriveWebxdcUrlTopic('https://cdn.xdc','msg1'))
  throw new Error('URL topic did not use the last .xdc delimiter');
const armadaUploaded=PCConcord.webxdcOf({id:'upload-event',text:xdcUrl,tags:[['imeta',`url ${xdcUrl}`,'m application/x-webxdc','webxdc fixture-uuid']]},armadaRoom,'general');
if(armadaUploaded.uuid!=='fixture-uuid')throw new Error('explicit Armada Webxdc UUID was replaced');
/* Captured from Armada's live Gamers/#xdc Quake post. Armada uses the registered vendor MIME,
   not PosterChan's older x-webxdc alias; losing this topic puts ioquake in a different room. */
const liveArmadaTopic='FBSTBOCHXLXTUPWPCLPAGBZDFNBSK5HKMTTMTVAKHY2EGNJFYMLQ';
const liveArmada=PCConcord.webxdcOf({id:'75d8530b4eace74e24abd58092bb855dc42408077f91e9a025cd8b65aaa8119b',text:xdcUrl,tags:[['imeta',`url ${xdcUrl}`,'m application/vnd.webxdc+zip',`webxdc-topic ${liveArmadaTopic}`,`webxdc ${liveArmadaTopic}`,'summary Quake III Arena (OpenArena)']]},armadaRoom,'general');
if(!liveArmada||liveArmada.uuid!==liveArmadaTopic||liveArmada.name!=='Quake III Arena (OpenArena)')
  throw new Error('live Armada vendor-MIME attachment lost its shared Webxdc topic');
const xdcMessage={id:'xdc-message',text:'play this '+xdcUrl,tags:[['imeta',`url ${xdcUrl}`,'m application/x-webxdc','webxdc game-1','summary Game']]};
window.PCWebxdc={cardHtml:app=>`<div class="xdc-card" data-topic="${app.uuid}">Game</div>`};
const playableXdc=PCConcord.messageContentHtml({enc:String,linkify:String,linkCardHtml:s=>'PREVIEW:'+s},xdcMessage,armadaRoom,'general');
if(playableXdc.includes(xdcUrl)||!playableXdc.includes('play this'))
  throw new Error('playable Webxdc duplicated or erased its message text');
const liveCard=PCConcord.messageContentHtml({enc:String,linkify:String,linkCardHtml:()=>''},{id:'75d8530b4eace74e24abd58092bb855dc42408077f91e9a025cd8b65aaa8119b',text:xdcUrl,tags:[['imeta',`url ${xdcUrl}`,'m application/vnd.webxdc+zip',`webxdc-topic ${liveArmadaTopic}`,`webxdc ${liveArmadaTopic}`]]},armadaRoom,'general');
if(!liveCard.includes(`data-topic="${liveArmadaTopic}"`))throw new Error('rendered live Armada card did not retain explicit lobby topic');
delete window.PCWebxdc;
const fallbackXdc=PCConcord.messageContentHtml({enc:String,linkify:String,linkCardHtml:()=>''},xdcMessage);
if(!fallbackXdc.includes(xdcUrl))throw new Error('Webxdc URL fallback disappeared without a card renderer');
const blossomImage='https://blossom.example/'+ '9'.repeat(64);
const imageMessage={id:'plain-image',text:'photo '+blossomImage,tags:[['imeta',`url ${blossomImage}`,'m image/png','name pasted.png']]};
const imageHtml=PCConcord.messageContentHtml({enc:String,linkify:String,linkCardHtml:()=>''},imageMessage);
if(!imageHtml.includes('class="cc-plain-attachment"')||!imageHtml.includes('<img ')||imageHtml.includes('<p>photo '+blossomImage))
  throw new Error('extensionless Blossom image did not render from imeta metadata');
const customReaction=PCConcord.reactionSummary({enc:String,viewer:()=>({pubkey:'a'})},{id:'custom-react',reactions:{':carlJAM:':['b']},reactionUrls:{':carlJAM:':'https://emoji.example/carlJAM.gif'}});
if(!customReaction.includes('class="cc-reaction-emoji"')||!customReaction.includes('https://emoji.example/carlJAM.gif')||!customReaction.includes('alt=":carlJAM:"'))
  throw new Error('NIP-30 Concord reaction rendered as a raw shortcode');
const missingCustomReaction=PCConcord.reactionSummary({enc:String,viewer:()=>({pubkey:'a'})},{id:'custom-fallback',reactions:{':missing:':['b']},reactionUrls:{}});
if(!missingCustomReaction.includes(':missing:')||missingCustomReaction.includes('cc-reaction-emoji'))
  throw new Error('custom reaction fallback is not readable without its asset');
const iconRoom={icon:'https://old.example/icon.png'};
if(!await PCConcord.applyRoomIconMetadata(iconRoom,{icon:''},'icon-clear') || iconRoom.icon!=='')
  throw new Error('explicit community icon removal was ignored');
if(!await PCConcord.applyRoomIconMetadata(iconRoom,{icon:'🛸'},'icon-plain') || iconRoom.icon!=='🛸')
  throw new Error('plain community icon update was ignored');
const iconPlain=new Uint8Array([0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a,1,2,3,4]),iconKey=crypto.getRandomValues(new Uint8Array(32)),iconNonce=crypto.getRandomValues(new Uint8Array(16));
const iconCryptoKey=await crypto.subtle.importKey('raw',iconKey,'AES-GCM',false,['encrypt']);
const iconCipher=await crypto.subtle.encrypt({name:'AES-GCM',iv:iconNonce},iconCryptoKey,iconPlain);
const hex=a=>[...new Uint8Array(a)].map(x=>x.toString(16).padStart(2,'0')).join('');
const iconPointer={url:'https://icons.example/encrypted',key:hex(iconKey),nonce:hex(iconNonce),hash:hex(await crypto.subtle.digest('SHA-256',iconPlain))};
globalThis.fetch=async()=>new Response(iconCipher,{status:200});
iconRoom.communityId='icon-durable';iconRoom.icon='blob:dead-from-previous-renderer';
if(!await PCConcord.applyRoomIconMetadata(iconRoom,{icon:iconPointer},'icon-durable') ||
   JSON.stringify(iconRoom.iconPointer)!==JSON.stringify(iconPointer) || iconRoom.icon!=='')
  throw new Error('encrypted community icon pointer was not stored durably');
const rehydratedIcon=PCConcord.roomIcon({enc:String},iconRoom,0);
if(!rehydratedIcon.includes('blob:')||rehydratedIcon.includes('blob:dead-from-previous-renderer'))
  throw new Error('durable community icon pointer did not use the decrypted renderer cache');
// Exercise the cold path used after a renderer reload/re-enter, rather than reading the cache
// populated by applyRoomIconMetadata above. roomIcon starts hydration without blocking the room
// list; the subsequent render reads the newly decrypted blob URL from renderer-only memory.
const coldIconRoom={communityId:'icon-after-reload',name:'Cold icon',icon:'',iconPointer};
data.set('pc.concord.invites',JSON.stringify([coldIconRoom]));
const coldFirst=PCConcord.roomIcon({enc:String},coldIconRoom,0);
if(coldFirst.includes('blob:'))throw new Error('cold community icon reused another room cache entry');
await new Promise(resolve=>setTimeout(resolve,20));
const coldHydrated=PCConcord.roomIcon({enc:String},coldIconRoom,0);
if(!coldHydrated.includes('blob:'))throw new Error('encrypted community icon did not rehydrate after reload/re-enter');
const coldStored=JSON.parse(data.get('pc.concord.invites'))[0];
if(!coldStored.iconPointer||String(coldStored.icon||'').startsWith('blob:'))
  throw new Error('community icon persisted an ephemeral blob instead of its encrypted pointer');
data.delete('pc.concord.invites');

// A remote encrypted community icon is decoration. Its fetch used to sit in applyControl's await
// chain ahead of cachedEnvelopePage, so a slow/down icon host made a healthy cached room appear
// empty for the full network timeout. Hold the icon response open and require cached history to
// paint before it resolves.
let releaseSlowIcon,slowIconStarted=false;
const slowIconPointer={...iconPointer,url:'https://icons.example/intentionally-slow'};
globalThis.fetch=async url=>String(url).includes('intentionally-slow')
  ? (slowIconStarted=true,await new Promise(resolve=>{releaseSlowIcon=()=>resolve(new Response(iconCipher,{status:200}));}))
  : new Response(iconCipher,{status:200});
const slowRoom={communityId:'slow-room',naddr:'slow-room',name:'Slow icon room',channels:[{name:'general',id:'slow-general'}],cord:{bundle:{...JOIN_BUNDLE,slowIcon:slowIconPointer}}};
data.set('pc.concord.invites',JSON.stringify([slowRoom]));
concordEnvelopeCache.set(JSON.stringify(['slow-room','control']),[{id:'cached-control',kind:1059,created_at:1}]);
concordEnvelopeCache.set(JSON.stringify(['slow-room','slow-general']),[{id:'cached-chat',kind:1059,created_at:2}]);
const slowHydration=PCConcord.activateJoinedRoom(window.__PC,0,false,'slow-room');
for(let i=0;i<20&&!feed.innerHTML.includes('joined history');i++)await new Promise(resolve=>setTimeout(resolve,5));
if(!slowIconStarted||!feed.innerHTML.includes('joined history'))
  throw new Error('cached Concord history waited for the encrypted community icon');
releaseSlowIcon();await slowHydration;
data.delete('pc.concord.invites');concordEnvelopeCache.clear();

// Armada rooms do not have to call their first channel #general. On a cold renderer, paint the
// cached control list and its real selected channel before another channel cache or relay backfill
// completes. This is the exact first-entry regression that previously appeared fixed on re-entry.
const noGeneralRoom={communityId:'no-general',naddr:'no-general',name:'Cached room',channels:[{name:'general'}],cord:{bundle:{...JOIN_BUNDLE,noGeneral:true}}};
data.set('pc.concord.invites',JSON.stringify([noGeneralRoom]));concordEnvelopeCache.clear();
concordEnvelopeCache.set(JSON.stringify(['no-general','control']),[{id:'no-general-control',kind:1059,created_at:1}]);
concordEnvelopeCache.set(JSON.stringify(['no-general','joined-lounge']),[{id:'lounge-chat',kind:1059,created_at:2}]);
concordEnvelopeCache.set(JSON.stringify(['no-general','joined-later']),[{id:'later-chat',kind:1059,created_at:3}]);
const originalCachePage=window.PCConcordCache.page;let releaseLaterPage;
window.PCConcordCache.page=async(key,opts)=>String(key).includes('joined-later')
  ? await new Promise(resolve=>{releaseLaterPage=()=>resolve({events:concordEnvelopeCache.get(key)||[]});})
  : originalCachePage.call(window.PCConcordCache,key,opts);
let coldRelayQueries=0;const coldPC={...window.__PC,relayQuery:async()=>{coldRelayQueries++;return[];},relayQueryFrom:async()=>{coldRelayQueries++;return[];}};
const coldOpen=PCConcord.activateJoinedRoom(coldPC,0,false,'no-general');
for(let i=0;i<20&&!feed.innerHTML.includes('joined history joined-lounge');i++)await new Promise(resolve=>setTimeout(resolve,5));
if(!feed.innerHTML.includes('#lounge')||!feed.innerHTML.includes('joined history joined-lounge'))
  throw new Error('cold cached Armada room did not paint its real first channel immediately');
if(coldRelayQueries!==0)throw new Error('relay backfill started before cached room history painted');
releaseLaterPage();await coldOpen;window.PCConcordCache.page=originalCachePage;
data.delete('pc.concord.invites');concordEnvelopeCache.clear();

// Opening a room is still successful when its encrypted control/history cache is valid but every
// live relay path fails synchronously (the desktop bridge's disconnected/not-ready failure shape).
// The failed refresh must remain retryable, while a room with no usable cache must stay honest.
const offlineRoom={communityId:'offline-cached',naddr:'offline-cached',name:'Offline cached room',channels:[{name:'general',id:'joined-general'}],cord:{bundle:{...JOIN_BUNDLE}}};
data.set('pc.concord.invites',JSON.stringify([offlineRoom]));
concordEnvelopeCache.set(JSON.stringify(['offline-cached','control']),[{id:'offline-control',kind:1059,created_at:1}]);
concordEnvelopeCache.set(JSON.stringify(['offline-cached','joined-general']),[{id:'offline-chat',kind:1059,created_at:2}]);
let offlineQueries=0;
const offlinePC={...window.__PC,
  relayQuery(){offlineQueries++;throw new Error('relay bridge offline');},
  relayQueryFrom(){offlineQueries++;throw new Error('relay bridge offline');},
};
const cachedToastCount=calls.toasts.length;
if(!await PCConcord.activateJoinedRoom(offlinePC,0,false,'offline-cached') || !feed.innerHTML.includes('joined history'))
  throw new Error('valid cached Concord room did not open through a failed live refresh');
if(calls.toasts.length!==cachedToastCount)
  throw new Error('failed live refresh showed a false room-history warning over valid cache');
const firstOfflineQueries=offlineQueries;
if(!await PCConcord.activateJoinedRoom(offlinePC,0,false,'offline-cached') || offlineQueries<=firstOfflineQueries)
  throw new Error('cached Concord room did not retry its failed live refresh on the next click');

const uncachedRoom={communityId:'offline-uncached',naddr:'offline-uncached',name:'Offline uncached room',channels:[{name:'general',id:'joined-general'}],cord:{bundle:{...JOIN_BUNDLE}}};
data.set('pc.concord.invites',JSON.stringify([uncachedRoom]));concordEnvelopeCache.clear();
const uncachedToastCount=calls.toasts.length;
if(await PCConcord.activateJoinedRoom(offlinePC,0,false,'offline-uncached'))
  throw new Error('uncached Concord room claimed a successful open while relays were unavailable');
if(calls.toasts.length!==uncachedToastCount+1 || !calls.toasts.at(-1).includes('relay bridge offline'))
  throw new Error('uncached Concord relay failure was hidden from the user');
data.delete('pc.concord.invites');concordEnvelopeCache.clear();
const legacyNonce=crypto.getRandomValues(new Uint8Array(12));
const legacyCipher=await crypto.subtle.encrypt({name:'AES-GCM',iv:legacyNonce},iconCryptoKey,iconPlain);
const legacyPointer={...iconPointer,nonce:hex(legacyNonce)};
globalThis.fetch=async()=>new Response(legacyCipher,{status:200});
const legacyIconRoom={communityId:'legacy-icon',icon:''};
if(!await PCConcord.applyRoomIconMetadata(legacyIconRoom,{icon:legacyPointer},'legacy-icon') || JSON.stringify(legacyIconRoom.iconPointer)!==JSON.stringify(legacyPointer))
  throw new Error('legacy 12-byte encrypted community icon stopped loading');
const priorFetch=globalThis.fetch;
globalThis.fetch=async()=>{throw new Error('offline icon host');};
const beforeBadIcon=JSON.stringify(iconRoom);
if(await PCConcord.applyRoomIconMetadata(iconRoom,{icon:{url:'https://bad.example/icon',key:'00',nonce:'00',hash:'00'}},'icon-bad') || JSON.stringify(iconRoom)!==beforeBadIcon)
  throw new Error('failed encrypted icon damaged room metadata');
const beforeOddNonce=JSON.stringify(iconRoom);
if(await PCConcord.applyRoomIconMetadata(iconRoom,{icon:{...iconPointer,nonce:'00'.repeat(8)}},'icon-odd-nonce') || JSON.stringify(iconRoom)!==beforeOddNonce)
  throw new Error('non-CORD encrypted icon nonce was accepted');
globalThis.fetch=priorFetch;
const leaveFixture=[{communityId:'first'},{communityId:'leave-me'},{communityId:'last'}];
const left=PCConcord.removeCommunityByIdentity(leaveFixture,'leave-me');
if(left.index!==1 || left.rooms.map(x=>x.communityId).join(',')!=='first,last' || leaveFixture.length!==3)
  throw new Error('community leave did not remove exactly the durable identity');
const missingLeave=PCConcord.removeCommunityByIdentity(leaveFixture,'not-present');
if(missingLeave.index!==-1 || missingLeave.rooms.length!==3)
  throw new Error('missing community leave removed another room');
const mentionRoom={naddr:'mention-room',channels:[{name:'general'},{name:'support',id:'support-id'}]};
data.set('pc.concord.test.mention-room',JSON.stringify([{id:'m1',pubkey:'b'.repeat(64),text:'general'}]));
data.set('pc.concord.test.mention-room.support-id',JSON.stringify([{id:'m2',pubkey:'c'.repeat(64),text:'support'}]));
data.set('pc.concord.star.mention-room:support','1');
const channelSections=PCConcord.channelSectionsHtml({enc:String},mentionRoom,mentionRoom.channels);
if(!channelSections.includes('STARRED') ||
   channelSections.indexOf('STARRED')>channelSections.indexOf('TEXT CHANNELS') ||
   (channelSections.match(/data-cc-channel="support"/g)||[]).length!==1 ||
   (channelSections.match(/data-cc-channel="general"/g)||[]).length!==1)
  throw new Error('starred channels were not grouped once above regular channels');
const roomPeople=PCConcord.roomParticipants(mentionRoom,'a'.repeat(64));
if(roomPeople.length!==3 || !roomPeople.includes('c'.repeat(64)))
  throw new Error('community member pool omitted a participant from another channel');
const profiles=new Map([
  ['b'.repeat(64),{display_name:'Other User'}],
  ['c'.repeat(64),{name:'support.mod'}],
]);
const typed=PCConcord.typedMentionRecipients('hello @Other_User and @support.mod',roomPeople,pk=>profiles.get(pk));
if(typed.length!==2 || !typed.includes('b'.repeat(64)) || !typed.includes('c'.repeat(64)))
  throw new Error('typed community mentions were not resolved to Nostr recipients');
if(!PCConcord.textMentionsViewer('hello @Other_User',['Other User']) ||
   PCConcord.textMentionsViewer('hello @Other_Username',['Other User']) ||
   PCConcord.textMentionsViewer('email Other_User@example.test',['Other User']))
  throw new Error('notification mention matching is not token-exact');

// A busy/corrupt persisted room used to synchronously construct all 5,000 message DOM rows and
// their link previews before the composer could accept input. This is the production launch-freeze
// shape: a valid room plus the maximum retained history and unusually large relay-controlled text.
const stressRoom={local:true,naddr:'launch-stress',name:'Busy room',channels:[{name:'general',private:false}]};
const stressMessages=Array.from({length:5000},(_,i)=>({id:'stress-'+i,pubkey:'b'.repeat(64),by:'member',at:i,text:'x'.repeat(1024)}));
data.set('pc.concord.invites',JSON.stringify([stressRoom]));
data.set('pc.concord.active','0');
data.set('pc.concord.test.launch-stress',JSON.stringify(stressMessages));
const launchStarted=performance.now();
PCConcord.render();
const launchElapsed=performance.now()-launchStarted,painted=(feed.innerHTML.match(/class="cc-message"/g)||[]).length;
if(!feed.innerHTML.includes('id="cc-input"')||painted!==300||launchElapsed>2000)
  throw new Error(`pathological Concord launch stayed blocked: ${painted} rows in ${launchElapsed.toFixed(0)}ms`);
data.delete('pc.concord.invites');data.delete('pc.concord.active');data.delete('pc.concord.test.launch-stress');

// A malformed encrypted icon is persisted by older clients. Opening that room must try once and
// settle; the former finally{} repaint retriggered roomIcon immediately and grew the renderer by
// gigabytes while clicks appeared dead.
const brokenIconRoom={local:true,communityId:'broken-icon-room',naddr:'broken-icon-room',name:'Broken icon',channels:[{name:'general'}],iconPointer:{url:'https://bad.example/icon',key:'00',nonce:'00',hash:'00'}};
data.set('pc.concord.invites',JSON.stringify([brokenIconRoom]));data.set('pc.concord.active','0');
const writesBeforeBrokenIcon=feedWrites;PCConcord.render();
await new Promise(resolve=>setTimeout(resolve,30));
if(feedWrites-writesBeforeBrokenIcon!==1)throw new Error('failed Concord icon caused a repaint loop');
data.delete('pc.concord.invites');data.delete('pc.concord.active');
PCConcord.render();
control('messages-direct').click();
if(calls.messagesTab!=='messages' || activeView!=='messages')
  throw new Error('Direct Messages tab did not repaint its owning Messages frame');
activeView='concord';

control('cc-community-name').value='Runtime Test';
control('cc-community-icon').value='🚀';
await control('cc-create-go').click();
const rooms=JSON.parse(data.get('pc.concord.invites'));
if(rooms.length!==1 || rooms[0].local || !rooms[0].url || !rooms[0].channels || rooms[0].channels[0].private!==false || rooms[0].name!=='Runtime Test' || rooms[0].icon!=='🚀') throw new Error('relay create flow failed');

// Discover owns its live public subscription only while that surface is visible. Entering a room
// must close it, and all ensuing history traffic must name only the relay carried by the bundle.
const openedBeforeDiscover=calls.discoveryOpened,closedBeforeDiscover=calls.discoveryClosed;
const ordinaryRelayQueryFrom=window.__PC.relayQueryFrom;let abortedDiscoveryReads=0;
const queriesBeforeDiscover=calls.queryTargets.length;
window.__PC.relayQueryFrom=(relays,filters,options={})=>{
  calls.queryTargets.push([...relays]);
  calls.queryOptions=(calls.queryOptions||[]).concat(options);
  if(!options.signal)return Promise.resolve(relayFixtures(filters));
  return new Promise(resolve=>options.signal.addEventListener('abort',()=>{abortedDiscoveryReads++;resolve([]);},{once:true}));
};
control('cc-discovery').click();
if(calls.discoveryOpened!==openedBeforeDiscover+1)throw new Error('Discover did not open its public relay subscription');
await new Promise(resolve=>setTimeout(resolve,0));
const optionsBeforePassiveCard=(calls.queryOptions||[]).length;
calls.discoveryHandlers.onEvent({id:'public-card',created_at:99,pubkey:'b'.repeat(64),content:'Public https://armada.buzz/invite/naddr1qqqq#abc_DEF'});
await new Promise(resolve=>setTimeout(resolve,0));
const passiveOptions=(calls.queryOptions||[]).slice(optionsBeforePassiveCard);
if(passiveOptions.some(options=>/explicit invite|concord room$/.test(String(options.purpose||''))))
  throw new Error('passive public community card opened its invite/bootstrap relays');
const automaticQueries=calls.queryTargets.slice(queriesBeforeDiscover),automaticTargets=automaticQueries.flat();
for(let i=0;i<automaticQueries.length;i++)if(automaticQueries[i].some(relay=>/relay\.(?:ditto\.pub|damus\.io)/.test(relay))){
  const options=calls.queryOptions[calls.queryOptions.length-automaticQueries.length+i]||{};
  if(options.allowBlocked!==true||Number(options.failureCooldown)<1800000)
    throw new Error('legacy membership recovery was not bounded: '+JSON.stringify({relays:automaticQueries[i],options}));
}
if(automaticTargets.length>12)
  throw new Error('one Discover paint fanned out across passive invite cards: '+JSON.stringify(automaticTargets));
const roomButton=dollars('[data-cc-server]').find(b=>b.dataset.ccServer==='0');
if(!roomButton)throw new Error('Discover did not render the joined room control');
const queriesBeforeRoom=calls.queryTargets.length;
await roomButton.onclick();
if(calls.discoveryClosed!==closedBeforeDiscover+1)throw new Error('active room left the Discover relay subscription open');
await new Promise(resolve=>setTimeout(resolve,10));
window.__PC.relayQueryFrom=ordinaryRelayQueryFrom;
if(!abortedDiscoveryReads)throw new Error('active room did not abort its open Discover one-shot relay sockets');
const roomRelaySet=PCConcord.roomRelays(rooms[0].cord.bundle);
const activeRoomQueries=calls.queryTargets.slice(queriesBeforeRoom);
if(activeRoomQueries.some(relays=>relays.some(relay=>!roomRelaySet.includes(relay))))
  throw new Error('active room queried outside its bundle relay: '+JSON.stringify(activeRoomQueries));

control('cc-edit-icon').click();
control('cc-icon-value').value='https://example.test/room.png';
await control('cc-icon-save').click();
const edited=JSON.parse(data.get('pc.concord.invites'));
if(edited[0].icon!=='https://example.test/room.png') throw new Error('icon edit flow failed');
control('cc-description-value').value='Editable room description';
control('cc-settings-icon').value='🌌';
control('cc-channel-visibility').value='private';
await control('cc-settings-save').click();
const configured=JSON.parse(data.get('pc.concord.invites'));
if(configured[0].description!=='Editable room description' || configured[0].icon!=='🌌' || configured[0].channels[0].private!==true) throw new Error('private channel settings flow failed');
control('cc-channel-visibility').value='public';
await control('cc-settings-save').click();
const madePublic=JSON.parse(data.get('pc.concord.invites'));
if(madePublic[0].channels[0].private!==false) throw new Error('public channel settings flow failed');
control('cc-copy-link').click();
if(calls.copied!=='https://poster.place/invite/naddr1qqqq#abc_DEF') throw new Error('relay invite copy failed');
await control('cc-publish-listing').click();
if(!calls.toasts.some(x=>x.includes('published to Armada Discover'))) throw new Error('Armada listing publish failed');

const input=control('cc-input');
control('cc-emoji').click();
if(input.value!=='😀') throw new Error('emoji control failed');
input.value='';
const file=control('cc-file'); file.files=[{name:'photo.png',size:1000}];
await file.onchange();
if(input.value!=='https://files.example/photo.png') throw new Error('attachment control failed');
input.value='';
let pastePrevented=false;
const pastedImage={name:'clipboard.png',type:'image/png',size:900};
input.onpaste({clipboardData:{items:[{kind:'file',type:'image/png',getAsFile:()=>pastedImage}]},preventDefault(){pastePrevented=true;}});
await new Promise(resolve=>setTimeout(resolve,0));
if(!pastePrevented || input.value!=='https://files.example/clipboard.png') throw new Error('clipboard image paste failed');
input.value=''; pastePrevented=false;
input.onpaste({clipboardData:{items:[{kind:'string',type:'text/plain'}]},preventDefault(){pastePrevented=true;}});
if(pastePrevented) throw new Error('plain text paste was intercepted');
control('cc-members').click();
if(!control('cc-members-dialog').classList.removed.includes('hidden')) throw new Error('members control failed');
await control('cc-notify').onclick();
if(calls.notified!==1) throw new Error('notification control failed');
const groupCallsBefore=calls.group;
control('cc-call').click();
if(calls.group===groupCallsBefore&&!calls.toasts.some(x=>x.includes('No other community members')))
  throw new Error('call control neither called hydrated members nor reported an empty room');
input.value='hello concord';
let prevented=false;
input.onkeydown({key:'Enter',ctrlKey:false,metaKey:false,preventDefault(){prevented=true;}});
if(prevented || data.has('pc.concord.test.'+rooms[0].naddr)) throw new Error('plain Enter sent a message');
await input.onkeydown({key:'Enter',ctrlKey:true,metaKey:false,preventDefault(){prevented=true;}});
const messages=JSON.parse(data.get('pc.concord.test.'+rooms[0].naddr));
if(!prevented || messages.length!==1 || messages[0].text!=='hello concord' || messages[0].by!=='Test User')
  throw new Error('Ctrl+Enter send flow failed');

// The relay can echo a rumor before publish() resolves.  Reproduce that ordering exactly: keep the
// optimistic continuation waiting, insert the permanent relay row, then let it rename its pending
// row to the same id.  Storage and the subsequent render must still contain one message.
let releaseRace;
PosterCordReader.createChatWrap=()=>new Promise(resolve=>{releaseRace=()=>resolve({
  rumorId:'e'.repeat(64),wrap:{kind:1059},ms:2345});});
input.value='race once';
const racing=input.onkeydown({key:'Enter',ctrlKey:true,metaKey:false,preventDefault(){}});
await new Promise(resolve=>setTimeout(resolve,0));
const raceKey='pc.concord.test.'+rooms[0].naddr;
const whilePending=JSON.parse(data.get(raceKey));
whilePending.push({id:'e'.repeat(64),by:'Test User',pubkey:'a'.repeat(64),text:'race once',
                   at:2345,kind:9,tags:[],reactions:{},remote:true});
data.set(raceKey,JSON.stringify(whilePending));
if(!releaseRace) throw new Error('publish race was not held');
releaseRace();
await racing;
PosterCordReader.createChatWrap=async(_bundle,_wraps,_channel,text,_author,_sign,tags,kind)=>{
  calls.lastChat={text,tags,kind};
  return {rumorId:'f'.repeat(64),wrap:{kind:1059},ms:3456};
};
const afterRace=JSON.parse(data.get(raceKey));
if(afterRace.filter(m=>m.id==='e'.repeat(64)).length!==1)
  throw new Error('relay echo duplicated optimistic message');
PCConcord.render();
if((feed.innerHTML.match(new RegExp('data-message-id="'+'e'.repeat(64)+'"','g'))||[]).length!==1)
  throw new Error('duplicate relay message rendered twice');

// Message actions stay collapsed until the user activates the post.  Reactions
// must not be permanently repeated beside every message on touch layouts.
const permanentId='e'.repeat(64);
let acted=JSON.parse(data.get(raceKey)).find(m=>m.id===permanentId);
const actionTrigger=dollars('[data-cc-actions]').find(b=>b.dataset.ccActions===permanentId);
if(!actionTrigger)throw new Error('rendered message has no collapsed action trigger');
if(dollars('[data-cc-quick-react]').length)
  throw new Error('rendered message still exposes permanent quick-reaction controls');
// Treat the permanent fixture as another member's post for the reply-participant assertion.
acted.pubkey='b'.repeat(64); acted.by='Other User';
const afterReaction=JSON.parse(data.get(raceKey));
afterReaction[afterReaction.findIndex(m=>m.id===permanentId)]=acted;
data.set(raceKey,JSON.stringify(afterReaction));

PCConcord.render();
control('cc-call').click();
if(calls.group!==1 || !calls.groupPeers.includes('b'.repeat(64)))
  throw new Error('community call omitted a known room participant');
if(PCConcord.memberTapAction(true,false)!=='profile' ||
   PCConcord.memberTapAction(false,false)!=='menu' ||
   PCConcord.memberTapAction(true,true)!=='consume')
  throw new Error('member tap/long-press action routing is wrong');
let memberViewportNarrow=false;
window.matchMedia=query=>({matches:query==='(max-width:820px)'&&memberViewportNarrow});
if(PCConcord.memberViewportIsNarrow())throw new Error('desktop member viewport was detected as mobile');
memberViewportNarrow=true;
if(!PCConcord.memberViewportIsNarrow())throw new Error('member viewport stayed desktop after a responsive resize');
memberViewportNarrow=false;
const picker={getBoundingClientRect:()=>({width:140,height:70})};
let pickerAt=PCConcord.reactionPickerPosition({getBoundingClientRect:()=>({right:790,top:730,bottom:760})},picker,{width:800,height:768});
if(pickerAt.left!==650||pickerAt.top!==654)
  throw new Error('desktop last-message reaction picker did not flip above its trigger: '+JSON.stringify(pickerAt));
pickerAt=PCConcord.reactionPickerPosition({getBoundingClientRect:()=>({right:358,top:590,bottom:624})},{getBoundingClientRect:()=>({width:172,height:92})},{width:360,height:640});
if(pickerAt.left!==180||pickerAt.top!==492)
  throw new Error('mobile bottom-edge reaction picker escaped the viewport: '+JSON.stringify(pickerAt));
PCConcord.render();
const disclosure=dollars('[data-cc-actions]').find(b=>b.dataset.ccActions===permanentId);
const disclosureRow=messageRow(permanentId);
disclosure.onclick({stopPropagation(){}});
if(!disclosureRow.classList.contains('cc-actions-open')||disclosure.attributes['aria-expanded']!=='true')
  throw new Error('ellipsis did not disclose message actions');
documentListeners.get('pointerdown')({target:{closest(){return null;}}});
if(disclosureRow.classList.contains('cc-actions-open')||disclosure.attributes['aria-expanded']!=='false')
  throw new Error('outside press did not immediately dismiss message actions');
disclosure.onclick({stopPropagation(){}});
documentListeners.get('keydown')({key:'Escape'});
if(disclosureRow.classList.contains('cc-actions-open'))
  throw new Error('Escape did not immediately dismiss message actions');
const reply=dollars('[data-cc-reply]').find(b=>b.dataset.ccReply===permanentId);
if(!reply)throw new Error('rendered message has no reply control');
disclosure.onclick({stopPropagation(){}});
reply.onclick();
if(disclosureRow.classList.contains('cc-actions-open')||disclosure.attributes['aria-expanded']!=='false')
  throw new Error('choosing a message action did not close its disclosure');
// A live relay message, profile/icon metadata hydration and attachment hydration each call the same
// whole-workspace render path. Exercise actual textarea replacement (not merely a source assertion)
// while a reply, attachment URL, mention query, focus and non-collapsed selection are active.
const draftInput=control('cc-input'),draftUrl='https://files.example/photo.png';
// Let the earlier settings-dialog focus timer settle; this scenario begins with the composer as the
// intentional active control, exactly like a person typing when relay/profile repaints arrive.
await new Promise(resolve=>setTimeout(resolve,25));
draftInput.value='thread response '+draftUrl+' @bb';
draftInput.setSelectionRange(draftInput.value.length-2,draftInput.value.length,'backward');
draftInput.focus();draftInput.dispatchEvent({type:'input'});
replaceComposerOnWrite=true;
PCConcord.backgroundRender(); // remote-message poll
PCConcord.backgroundRender(); // profile/community-icon hydration
PCConcord.backgroundRender(); // attachment-card hydration
await new Promise(resolve=>setTimeout(resolve,0));
const repaintedInput=control('cc-input');
if(repaintedInput!==draftInput || repaintedInput.value!=='thread response '+draftUrl+' @bb' ||
   repaintedInput.selectionStart!==repaintedInput.value.length-2 ||
   repaintedInput.selectionEnd!==repaintedInput.value.length || document.activeElement!==repaintedInput ||
   !feed.innerHTML.includes('Replying to'))
  throw new Error('Concord background repaint replaced or changed composer state: '+JSON.stringify({same:repaintedInput===draftInput,
    value:repaintedInput.value,start:repaintedInput.selectionStart,end:repaintedInput.selectionEnd,
    focused:document.activeElement===repaintedInput,active:document.activeElement&&document.activeElement.id,
    reply:feed.innerHTML.includes('Replying to')}));
// Focus restoration is a latch, not a focus trap. If the person deliberately focuses another
// control after a repaint but before its rAF, the pending composer callback must not steal it back.
const intentionalTarget=control('cc-emoji');
intentionalTarget.focus();
PCConcord.backgroundRender();
await new Promise(resolve=>setTimeout(resolve,0));
if(document.activeElement!==intentionalTarget||control('cc-input')!==repaintedInput)
  throw new Error('Concord repaint stole focus back from an intentional target');
const afterIntentionalFocus=control('cc-input');
afterIntentionalFocus.focus();
// The mention candidates live outside the replaced DOM as well. Tab must still accept the candidate
// selected before the repaint, and the eventual message must carry its Nostr p tag.
afterIntentionalFocus.setSelectionRange(afterIntentionalFocus.value.length,afterIntentionalFocus.value.length);
await afterIntentionalFocus.onkeydown({key:'Tab',ctrlKey:false,metaKey:false,preventDefault(){}});
const publishesBeforeReply=calls.publishTargets.length;
await afterIntentionalFocus.onkeydown({key:'Enter',ctrlKey:true,metaKey:false,preventDefault(){}});
if(calls.lastChat?.kind!==1111 || !calls.lastChat.tags.some(t=>t[0]==='e'&&t[1]===permanentId) ||
   !calls.lastChat.tags.some(t=>t[0]==='p'&&t[1]==='b'.repeat(64)) ||
   !calls.lastChat.tags.some(t=>t[0]==='imeta'&&t.some(x=>String(x).includes(draftUrl))) ||
   control('cc-input').value!=='')
  throw new Error('successful draft send did not preserve reply/mention/attachment tags or clear once');
if(!calls.publishTargets.slice(publishesBeforeReply).some(relays=>JSON.stringify(relays)===JSON.stringify(roomRelaySet)))
  throw new Error('reply send did not target the room bundle relay: '+JSON.stringify(calls.publishTargets.slice(publishesBeforeReply)));
replaceComposerOnWrite=false;

PCConcord.render();
const sent=JSON.parse(data.get(raceKey)).find(m=>m.text.startsWith('thread response'));
const deletion=dollars('[data-cc-delete]').find(b=>b.dataset.ccDelete===sent?.id);
if(!deletion)throw new Error('own rendered message has no delete control');
window.__PC.uiConfirm=async()=>true;
await deletion.onclick();
if(calls.lastChat?.kind!==5 || !calls.lastChat.tags.some(t=>t[0]==='e'&&t[1]===sent.id) ||
   JSON.parse(data.get(raceKey)).some(m=>m.id===sent.id))
  throw new Error('delete did not publish a tombstone and remove the message');

control('cc-back-channels').click();
if(!feed.innerHTML.includes('cc-app show-chat'))
  throw new Error('mobile room list did not open its active conversation');
control('cc-back-channels').click();
if(!feed.innerHTML.includes('cc-app show-chat drawer-open'))
  throw new Error('mobile rooms button did not open the OS-style channel drawer');
control('cc-drawer-backdrop').click();
if(feed.innerHTML.includes('drawer-open'))
  throw new Error('mobile channel drawer backdrop did not close it');
data.set('pc.concord.seen.'+rooms[0].naddr+':general','1');
data.set('pc.concord.test.'+rooms[0].naddr,JSON.stringify([{by:'Other User',pubkey:'b'.repeat(64),text:'hey @tester',at:2}]));
PCConcord.render();
if(calls.mentions.length!==1 || !calls.mentions[0].title.includes('#general') ||
   !calls.mentions[0].opts.route.startsWith('concord:'+encodeURIComponent(rooms[0].naddr)+':general:'))
  throw new Error('mention notification lost its exact room/channel/message route');
PCConcord.render();
if(calls.mentions.length!==1) throw new Error('mention notification was not deduplicated');
// Mention cursors are per channel. A newer #general timestamp must not suppress #support, and OS
// notification replacement tags must not make mentions from the two channels overwrite each other.
data.set('pc.concord.seen.'+rooms[0].naddr+':support','1');
PCConcord.notifyMentions(window.__PC,rooms[0],[{by:'Support User',pubkey:'c'.repeat(64),text:'hey @tester',at:2}],window.__PC.viewer(),'tester','support');
if(calls.mentions.length!==2 || !calls.mentions[1].title.includes('#support') ||
   calls.mentions[0].opts.tag===calls.mentions[1].opts.tag)
  throw new Error('mention notification cursors/tags collided across channels');

// A direct Armada invite must hydrate during the JOIN transaction.  Before this regression was
// fixed, the handler saved a one-channel placeholder and said "community joined"; icon, real
// channels and history appeared only after switching away and back.
control('cc-invite-url').value=JOIN_URL;
await control('cc-join-go').click();
const afterJoin=JSON.parse(data.get('pc.concord.invites'));
const joined=afterJoin.find(r=>r.communityId===JOIN_BUNDLE.community_id);
if(!joined || !joined.cord?.hydrated || joined.icon!=='🛸' || joined.channels.length!==2)
  throw new Error('direct invite did not hydrate metadata and channels before completing: '+JSON.stringify({joined,rooms:afterJoin,toasts:calls.toasts.slice(-4)}));
if(!feed.innerHTML.includes('joined history'))
  throw new Error('direct invite did not render room history before completing');
if([...data.entries()].some(([key,value])=>key.startsWith('pc.concord.test.') && value.includes('joined history')))
  throw new Error('remote decrypted history leaked into localStorage');
if(!calls.toasts.some(x=>x==='community joined')) throw new Error('hydrated join never completed');

// A relay/deferred callback can render after the user has opened Code. It must not own the shared
// feed any more, nor restart Concord's live work or shell classes.
activeView='code';
feed.innerHTML='<div id="code-editor">working tree</div>';
const classesBefore=classList.added.length;
PCConcord.render();
if(feed.innerHTML!=='<div id="code-editor">working tree</div>') throw new Error('late Concord render replaced Code');
if(classList.added.length!==classesBefore) throw new Error('late Concord render changed Code shell classes');
console.log('concord runtime flow ok');
process.exit(0);
