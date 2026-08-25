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
const dollars = () => [];
const calls = {toasts:[], notified:0, group:0, mentions:[]};
let activeView = 'concord';

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
  relayQueryFrom:async()=>[],
  relayUrls:()=>['wss://relay.example'], signTemplate:async template=>template,
  relayPublish:async()=>({ok:true}), relayPublishTo:async()=>1,
  publish:async()=>({}),
  profOf:()=>({}), LOGO:'', linkify:s=>String(s), linkCardHtml:()=>'', hydrateLinkCards:()=>{},
};
globalThis.location={origin:'https://poster.place'};
window.PosterCord={createCommunity:async()=>({communityId:'c'.repeat(64),generalChannelId:'d'.repeat(64),events:[{}],url:'https://poster.place/invite/naddr1qqqq#abc_DEF',secrets:{},bundle:{relays:['wss://relay.example']}})};
window.PosterCordReader={
  inspectControl:()=>({controlPubkeys:[],channels:[]}),
  createMetadataWrap:async()=>({wrap:{kind:1059}}),
  createChatWrap:async()=>({rumorId:'f'.repeat(64),wrap:{kind:1059},ms:1234}),
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
const participants=PCConcord.threadParticipants([alice,bob],bob,'a'.repeat(64));
if(participants.length!==2 || !participants.includes(alice.pubkey) || !participants.includes(bob.pubkey)) throw new Error('thread participant inheritance failed');
PCConcord.render();

control('cc-community-name').value='Runtime Test';
control('cc-community-icon').value='🚀';
await control('cc-create-go').click();
const rooms=JSON.parse(data.get('pc.concord.invites'));
if(rooms.length!==1 || rooms[0].local || !rooms[0].url || !rooms[0].channels || rooms[0].channels[0].private!==false || rooms[0].name!=='Runtime Test' || rooms[0].icon!=='🚀') throw new Error('relay create flow failed');

control('cc-edit-icon').click();
control('cc-icon-value').value='https://example.test/room.png';
control('cc-icon-save').click();
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
data.set('pc.concord.seen.'+rooms[0].naddr,'1');
data.set('pc.concord.test.'+rooms[0].naddr,JSON.stringify([{by:'Other User',pubkey:'b'.repeat(64),text:'hey @tester',at:2}]));
PCConcord.render();
if(calls.mentions.length!==1 || !calls.mentions[0].title.includes('#general') || calls.mentions[0].opts.route!=='concord') throw new Error('mention notification failed');
PCConcord.render();
if(calls.mentions.length!==1) throw new Error('mention notification was not deduplicated');

// A relay/deferred callback can render after the user has opened Code. It must not own the shared
// feed any more, nor restart Concord's live work or shell classes.
activeView='code';
feed.innerHTML='<div id="code-editor">working tree</div>';
const classesBefore=classList.added.length;
PCConcord.render();
if(feed.innerHTML!=='<div id="code-editor">working tree</div>') throw new Error('late Concord render replaced Code');
if(classList.added.length!==classesBefore) throw new Error('late Concord render changed Code shell classes');
console.log('concord runtime flow ok');
