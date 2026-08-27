import fs from 'node:fs';
import vm from 'node:vm';

const data = new Map();
globalThis.localStorage = {
  getItem: key => data.has(key) ? data.get(key) : null,
  setItem: (key, value) => data.set(key, String(value)),
};

const makeClassList = () => ({added:[],removed:[],add(...x){this.added.push(...x);},remove(...x){this.removed.push(...x);},toggle(){}});
const classList = makeClassList();
const feed = { innerHTML:'', classList, insertAdjacentHTML(){} };
const controls = new Map();
function control(id){
  if(!controls.has(id)) controls.set(id, { id, value:'', classList:makeClassList(), focus(){}, click(){ return this.onclick?.(); } });
  return controls.get(id);
}
const dollar = selector => selector === '#feed' ? feed : control(selector.slice(1));
const actionControls = new Map();
const dataKey = selector => selector.match(/^\[data-cc-([\w-]+)\]$/)?.[1];
const camel = value => value.replace(/-([a-z])/g,(_m,c)=>c.toUpperCase());
const dollars = selector => {
  const key=dataKey(selector); if(!key)return [];
  const attribute='data-cc-'+key,pattern=new RegExp(attribute+'="([^"]*)"(?:[^>]*data-cc-emoji="([^"]*)")?','g');
  const found=[]; let match;
  while((match=pattern.exec(feed.innerHTML))){
    const identity=attribute+':'+match[1]+':'+(match[2]||''),button=actionControls.get(identity)||{dataset:{},disabled:false};
    button.dataset[camel('cc-'+key)]=match[1]; if(match[2])button.dataset.ccEmoji=match[2];
    actionControls.set(identity,button); found.push(button);
  }
  return found;
};
const calls = {toasts:[], notified:0, group:0, mentions:[], wraps:[]};
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
window.__PC = {
  $:dollar, $$:dollars, enc:s=>String(s), niceNip05:s=>s,
  isView:view=>view===activeView,
  viewer:()=>({pubkey:'a'.repeat(64),npub:'npub1testidentity',profile:{name:'tester',display_name:'Test User'}}),
  toast:s=>calls.toasts.push(String(s)),
  openEmojiPopover:(_anchor,pick)=>pick('😀',()=>{}),
  insertAt:(input,value)=>{ input.value+=value; },
  uploadBlob:async file=>'https://files.example/'+file.name,
  askOsNotify:async()=>{ calls.notified++; return 'granted'; },
  startGroupCall:()=>{ calls.group++; },
  copyValue:value=>{ calls.copied=value; },
  osNotify:(title,body,opts)=>{ calls.mentions.push({title,body,opts}); },
  relaySubscribe:()=>({close(){}}),
  relayQuery:async filters=>relayFixtures(filters),
  relayQueryFrom:async(_relays,filters)=>relayFixtures(filters),
  relayUrls:()=>['wss://relay.example'], signTemplate:async template=>template,
  relayPublish:async()=>({ok:true}), relayPublishTo:async(_relays,event)=>{calls.wraps.push(event);return 1;},
  publish:async()=>({}),
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
  body:{classList}, head:{appendChild(){}}, documentElement:{appendChild(){}},
  createElement:()=>({dataset:{}}),
  querySelector:s=>s==='link[data-concord-css]'?{}:null,
  addEventListener(){},
};

vm.runInThisContext(fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8'),
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
const mentionRoom={naddr:'mention-room',channels:[{name:'general'},{name:'support',id:'support-id'}]};
data.set('pc.concord.test.mention-room',JSON.stringify([{id:'m1',pubkey:'b'.repeat(64),text:'general'}]));
data.set('pc.concord.test.mention-room.support-id',JSON.stringify([{id:'m2',pubkey:'c'.repeat(64),text:'support'}]));
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
PCConcord.render();

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

// Exercise the actual rendered action handlers and validate their CORD rumor semantics.
const permanentId='e'.repeat(64);
let quick=dollars('[data-cc-quick-react]').find(b=>b.dataset.ccQuickReact===permanentId);
if(!quick)throw new Error('rendered message has no quick-reaction control');
await quick.onclick();
let acted=JSON.parse(data.get(raceKey)).find(m=>m.id===permanentId);
if(!acted?.reactions?.['👍']?.includes('a'.repeat(64)) || calls.lastChat?.kind!==7 ||
   !calls.lastChat.tags.some(t=>t[0]==='e'&&t[1]===permanentId))
  throw new Error('quick reaction did not publish and persist');
// Treat the permanent fixture as another member's post for the reply-participant assertion.
acted.pubkey='b'.repeat(64); acted.by='Other User';
const afterReaction=JSON.parse(data.get(raceKey));
afterReaction[afterReaction.findIndex(m=>m.id===permanentId)]=acted;
data.set(raceKey,JSON.stringify(afterReaction));

PCConcord.render();
const reply=dollars('[data-cc-reply]').find(b=>b.dataset.ccReply===permanentId);
if(!reply)throw new Error('rendered message has no reply control');
reply.onclick();
input.value='thread response';
await input.onkeydown({key:'Enter',ctrlKey:true,metaKey:false,preventDefault(){}});
if(calls.lastChat?.kind!==1111 || !calls.lastChat.tags.some(t=>t[0]==='e'&&t[1]===permanentId) ||
   !calls.lastChat.tags.some(t=>t[0]==='p'&&t[1]==='b'.repeat(64)))
  throw new Error('reply did not publish thread context and participant tags');

PCConcord.render();
const sent=JSON.parse(data.get(raceKey)).find(m=>m.text==='thread response');
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
data.set('pc.concord.seen.'+rooms[0].naddr,'1');
data.set('pc.concord.test.'+rooms[0].naddr,JSON.stringify([{by:'Other User',pubkey:'b'.repeat(64),text:'hey @tester',at:2}]));
PCConcord.render();
if(calls.mentions.length!==1 || !calls.mentions[0].title.includes('#general') || calls.mentions[0].opts.route!=='concord') throw new Error('mention notification failed');
PCConcord.render();
if(calls.mentions.length!==1) throw new Error('mention notification was not deduplicated');

// A direct Armada invite must hydrate during the JOIN transaction.  Before this regression was
// fixed, the handler saved a one-channel placeholder and said "community joined"; icon, real
// channels and history appeared only after switching away and back.
control('cc-invite-url').value=JOIN_URL;
await control('cc-join-go').click();
const afterJoin=JSON.parse(data.get('pc.concord.invites'));
const joined=afterJoin.find(r=>r.communityId===JOIN_BUNDLE.community_id);
if(!joined || !joined.cord?.hydrated || joined.icon!=='🛸' || joined.channels.length!==2)
  throw new Error('direct invite did not hydrate metadata and channels before completing: '+JSON.stringify({joined,rooms:afterJoin,toasts:calls.toasts.slice(-4)}));
if(![...data.entries()].some(([key,value])=>key.startsWith('pc.concord.test.') && value.includes('joined history')))
  throw new Error('direct invite did not hydrate room history before completing');
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
