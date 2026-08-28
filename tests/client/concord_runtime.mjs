import fs from 'node:fs';
import vm from 'node:vm';

const data = new Map();
globalThis.localStorage = {
  getItem: key => data.has(key) ? data.get(key) : null,
  setItem: (key, value) => data.set(key, String(value)),
};

const makeClassList = () => ({added:[],removed:[],values:new Set(),add(...x){this.added.push(...x);x.forEach(v=>this.values.add(v));},remove(...x){this.removed.push(...x);x.forEach(v=>this.values.delete(v));},contains(x){return this.values.has(x);},toggle(x,on){const yes=on===undefined?!this.values.has(x):!!on;yes?this.values.add(x):this.values.delete(x);return yes;}});
const classList = makeClassList();
let feedHtml='',replaceComposerOnWrite=false;
const feedListeners=new Map();
const feed = { classList, insertAdjacentHTML(){},contains(node){return [...controls.values()].includes(node);},
  addEventListener(name,fn){feedListeners.set(name,fn);},
  get innerHTML(){return feedHtml;}, set innerHTML(value){feedHtml=String(value);if(replaceComposerOnWrite){const old=controls.get('cc-input');if(old){old.isConnected=false;if(document.activeElement===old)document.activeElement=document.body;}controls.delete('cc-input');const textarea=feedHtml.match(/<textarea id="cc-input" data-cc-draft-key="([^"]*)"[^>]*>([\s\S]*?)<\/textarea>/);if(textarea){const next=control('cc-input');next.dataset.ccDraftKey=textarea[1];next.value=textarea[2];}}} };
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
const calls = {toasts:[], notified:0, group:0, mentions:[], wraps:[], profiles:[]};
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
  relaySubscribe:()=>({close(){}}),
  relayQuery:async filters=>relayFixtures(filters),
  relayQueryFrom:async(_relays,filters)=>relayFixtures(filters),
  relayUrls:()=>['wss://relay.example'], signTemplate:async template=>template,
  relayPublish:async()=>({ok:true}), relayPublishTo:async(_relays,event)=>{calls.wraps.push(event);return 1;},
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
  inspectControl:(_bundle,wraps)=>wraps.length
    ? {name:'Joined Armada Room',description:'Loaded immediately',icon:'🛸',channels:[
        {id:'joined-general',name:'general',private:false,streamPubkeys:['6'.repeat(64)]},
        {id:'joined-support',name:'support',private:false,streamPubkeys:['7'.repeat(64)]},
      ]}
    : {controlPubkeys:['8'.repeat(64)],channels:[]},
  inspectChat:async()=>({messages:[{id:'joined-message',pubkey:'b'.repeat(64),text:'joined history',at:12,kind:9,tags:[]}],reactions:[],reactionIds:[]}),
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
const xdcMessage={id:'xdc-message',text:'play this '+xdcUrl,tags:[['imeta',`url ${xdcUrl}`,'m application/x-webxdc','webxdc game-1','summary Game']]};
window.PCWebxdc={cardHtml:()=>'<div class="xdc-card">Game</div>'};
const playableXdc=PCConcord.messageContentHtml({enc:String,linkify:String,linkCardHtml:s=>'PREVIEW:'+s},xdcMessage);
if(playableXdc.includes(xdcUrl)||!playableXdc.includes('play this'))
  throw new Error('playable Webxdc duplicated or erased its message text');
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
const iconPlain=new Uint8Array([0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a,1,2,3,4]),iconKey=crypto.getRandomValues(new Uint8Array(32)),iconNonce=crypto.getRandomValues(new Uint8Array(12));
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
const priorFetch=globalThis.fetch;
globalThis.fetch=async()=>{throw new Error('offline icon host');};
const beforeBadIcon=JSON.stringify(iconRoom);
if(await PCConcord.applyRoomIconMetadata(iconRoom,{icon:{url:'https://bad.example/icon',key:'00',nonce:'00',hash:'00'}},'icon-bad') || JSON.stringify(iconRoom)!==beforeBadIcon)
  throw new Error('failed encrypted icon damaged room metadata');
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
control('cc-call').click();
if(!calls.toasts.some(x=>x.includes('No other community members'))) throw new Error('empty call state failed');
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
await afterIntentionalFocus.onkeydown({key:'Enter',ctrlKey:true,metaKey:false,preventDefault(){}});
if(calls.lastChat?.kind!==1111 || !calls.lastChat.tags.some(t=>t[0]==='e'&&t[1]===permanentId) ||
   !calls.lastChat.tags.some(t=>t[0]==='p'&&t[1]==='b'.repeat(64)) ||
   !calls.lastChat.tags.some(t=>t[0]==='imeta'&&t.some(x=>String(x).includes(draftUrl))) ||
   control('cc-input').value!=='')
  throw new Error('successful draft send did not preserve reply/mention/attachment tags or clear once');
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
