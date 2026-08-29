import fs from 'fs';
import vm from 'vm';
const src=fs.readFileSync(new URL('../../static/js/client/concord.js',import.meta.url),'utf8');
const noop=()=>{};
const document={querySelector:()=>null,querySelectorAll:()=>[],createElement:()=>({dataset:{}}),head:{appendChild:noop},documentElement:{appendChild:noop},addEventListener:noop};
const window={document,addEventListener:noop};
vm.runInNewContext(src,{window,document,console,setTimeout:()=>0,clearTimeout:noop,URL,atob,crypto:{},localStorage:{getItem:()=>null,setItem:noop,removeItem:noop},sessionStorage:{getItem:()=>null,setItem:noop}});
const api=window.PCConcord,viewer={pubkey:'5e759c2ca4a4e222ba7af89e6ff315e1d27843fe8bd0a3e7e61e4ba5b1c07326'};
let nip04Calls=0,nip44Calls=0,verifyCalls=0,poolQueries=0,listedQueries=[];
const membership={id:'membership',kind:10009,created_at:20,tags:[['group','public','wss://groups.example/','Public'],['r','wss://all.example']],content:'nip44-ciphertext'};
const p={
  relayQuery:async()=>{poolQueries++;return [membership];},relayQueryFrom:async(relays,filters,opts)=>{listedQueries.push({relays,filters,opts});if(filters[0].kinds[0]===10009)return [membership];return [];},
  relayUrls:()=>['wss://current-app.example'],verifyRelayEvents:async events=>{verifyCalls++;return events;},nip04dec:async()=>{nip04Calls++;throw new Error('not nip04');},
  nip44dec:async(peer)=>{nip44Calls++;if(peer!==viewer.pubkey)throw new Error('wrong self peer');return JSON.stringify([['group','private','wss://groups.example','Private']]);}
};
const joined=await api.nip29Memberships(p,viewer);
if(nip44Calls!==1||nip04Calls!==0||joined.groups.length!==2||joined.groups[1].id!=='private')throw new Error('NIP-44 private self-list was not decoded');
if(poolQueries!==1||listedQueries[0].relays.includes('wss://current-app.example')||!listedQueries[0].relays.some(r=>/relay\.damus\.io/.test(r))||listedQueries[0].filters[0].kinds[0]!==10009||listedQueries[0].filters[0].authors[0]!==viewer.pubkey||!listedQueries[0].opts.exact||!listedQueries[0].opts.allowBlocked||listedQueries[0].opts.failureCooldown<1800000)throw new Error('kind-10009 lookup skipped connected/legacy membership sources or lacked a long failure circuit');
const metadataEvent={id:'meta',kind:39000,created_at:30,tags:[['d','private'],['name','Private Room'],['about','from tags'],['picture','https://example.test/icon.png']],content:''};
p.relayQueryFrom=async(relays,filters,opts)=>{listedQueries.push({relays,filters,opts});return [metadataEvent,{...metadataEvent,id:'forged-extra',tags:[['d','other']]}];};
const metadata=await api.nip29Metadata(p,'wss://groups.example',['private']);
if(metadata.length!==1||metadata[0].name!=='Private Room'||metadata[0].description!=='from tags')throw new Error('39000 tag metadata was not parsed or filtered');
const chat={id:'chat',kind:9,created_at:40,pubkey:'author',tags:[['h','private']],content:'hello'};
p.relayQueryFrom=async(relays,filters,opts)=>{listedQueries.push({relays,filters,opts});return [chat,{...chat,id:'wrong',tags:[['h','other']]}];};p.profOf=()=>({name:'Alice'});
const history=await api.nip29History(p,{relay:'wss://groups.example',groupId:'private'});
if(history.length!==1||history[0].text!=='hello'||history[0].by!=='Alice')throw new Error('NIP-29 history was not scoped to the h tag');
if(!listedQueries.slice(-2).every(q=>q.relays.length===1&&q.relays[0]==='wss://groups.example'&&q.opts.exact)||verifyCalls<3)throw new Error(`listed-relay reads were not signature verified (${verifyCalls})`);
let metadataReads=0;const rOnly={...membership,id:'r-only',created_at:50,tags:[['r','wss://all.example']],content:''};
await api.syncNip29Memberships({viewer:()=>viewer,relayUrls:()=>[],relayQuery:async()=>[rOnly],relayQueryFrom:async(_r,filters)=>{if(filters[0].kinds[0]===39000)metadataReads++;return [rOnly];},verifyRelayEvents:async events=>events},viewer);
if(metadataReads!==0)throw new Error('bare r tag enumerated every public group as joined');
const folded=api.foldNip29History([
  {id:'m1',kind:9,created_at:1,pubkey:'alice',tags:[['h','private']],content:'one'},
  {id:'rx',kind:7,created_at:2,pubkey:'bob',tags:[['h','private'],['e','m1']],content:'👍'},
  {id:'del-rx',kind:5,created_at:3,pubkey:'bob',tags:[['h','private'],['e','rx']],content:''},
  {id:'m2',kind:1111,created_at:4,pubkey:'alice',tags:[['h','private'],['e','m1']],content:'reply'},
  {id:'bad-del',kind:5,created_at:5,pubkey:'mallory',tags:[['h','private'],['e','m1']],content:''}
],{profOf:()=>({})},'private');
if(folded.length!==2||folded[0].reactions['👍']||folded.some(m=>[5,7].includes(m.kind))||!folded.find(m=>m.kind===1111).reply)throw new Error('NIP-29 delete/reaction/reply fold is incorrect');
const plusFold=api.foldNip29History([{id:'target',kind:9,created_at:1,pubkey:'alice',tags:[['h','private']],content:'x'},{id:'plus',kind:7,created_at:2,pubkey:'bob',tags:[['h','private'],['e','target']],content:'+'}],{profOf:()=>({})},'private');
if(!plusFold[0].reactions['👍'].includes('bob'))throw new Error('generic NIP-25 reaction was not normalized');
const previous=api.nip29PreviousTags([{id:'11111111old',remote:true,pubkey:'me'},{id:'pending',remote:false,pubkey:'x'},{id:'aaaaaaaa0',remote:true,pubkey:'x'},{id:'bbbbbbbb0',remote:true,pubkey:'y'},{id:'cccccccc0',remote:true,pubkey:'z'}],'me');
if(previous.length!==1||previous[0].length!==4||previous[0][1]!=='aaaaaaaa'||previous[0][3]!=='cccccccc')throw new Error('recommended previous tag was not bounded, shortened, or own-event filtered');
const publishCalls=[],made=await api.publishNip29Message({publishNip29Authed:async(relay,template)=>{publishCalls.push({relay,template});return{id:'signed',created_at:99,...template};}},
  {protocol:'nip29',relay:'wss://groups.example',groupId:'private',communityId:'nip29:wss://groups.example#private',channels:[{name:'general',id:'private'}]},'general','sent',[['e','m1']],1111);
if(made.rumorId!=='signed'||publishCalls[0].relay!=='wss://groups.example'||publishCalls[0].template.kind!==1111||!publishCalls[0].template.tags.some(t=>t[0]==='h'&&t[1]==='private'))throw new Error('native NIP-29 publish template was wrong');
let rejected=false;try{await api.publishNip29Message({publishNip29Authed:async()=>{throw new Error('blocked: not a member');}},{protocol:'nip29',relay:'wss://groups.example',groupId:'private'},'general','nope');}catch(e){rejected=e.message.includes('not a member');}if(!rejected)throw new Error('NIP-29 relay rejection was reported as success');
const legacy=api.nip29MembershipTags([['group','x','not-a-relay']]);if(legacy.groups.length)throw new Error('invalid relay accepted');
console.log('concord nip29 runtime ok');
