import fs from 'fs';
import vm from 'vm';
const src=fs.readFileSync(new URL('../../static/js/client/concord.js',import.meta.url),'utf8');
const noop=()=>{};
const document={querySelector:()=>null,querySelectorAll:()=>[],createElement:()=>({dataset:{}}),head:{appendChild:noop},documentElement:{appendChild:noop},addEventListener:noop};
const window={document,addEventListener:noop};
vm.runInNewContext(src,{window,document,console,setTimeout:()=>0,clearTimeout:noop,URL,atob,crypto:{},localStorage:{getItem:()=>null,setItem:noop,removeItem:noop},sessionStorage:{getItem:()=>null,setItem:noop}});
const api=window.PCConcord,viewer={pubkey:'5e759c2ca4a4e222ba7af89e6ff315e1d27843fe8bd0a3e7e61e4ba5b1c07326'};
let nip04Calls=0,nip44Calls=0,verifyCalls=0,listedQueries=[];
const membership={id:'membership',kind:10009,created_at:20,tags:[['group','public','wss://groups.example/','Public'],['r','wss://all.example']],content:'nip44-ciphertext'};
const p={
  relayQuery:async()=>[membership],relayQueryFrom:async(relays,filters,opts)=>{listedQueries.push({relays,filters,opts});if(filters[0].kinds[0]===10009)return [membership];return [];},
  relayUrls:()=>['wss://current-app.example'],verifyRelayEvents:async events=>{verifyCalls++;return events;},nip04dec:async()=>{nip04Calls++;throw new Error('not nip04');},
  nip44dec:async(peer)=>{nip44Calls++;if(peer!==viewer.pubkey)throw new Error('wrong self peer');return JSON.stringify([['group','private','wss://groups.example','Private']]);}
};
const joined=await api.nip29Memberships(p,viewer);
if(nip44Calls!==1||nip04Calls!==0||joined.groups.length!==2||joined.groups[1].id!=='private')throw new Error('NIP-44 private self-list was not decoded');
if(!listedQueries[0].relays.includes('wss://current-app.example')||listedQueries[0].filters[0].kinds[0]!==10009||listedQueries[0].filters[0].authors[0]!==viewer.pubkey||!listedQueries[0].opts.exact)throw new Error('current app relays were omitted from exact kind-10009 lookup');
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
const legacy=api.nip29MembershipTags([['group','x','not-a-relay']]);if(legacy.groups.length)throw new Error('invalid relay accepted');
console.log('concord nip29 runtime ok');
