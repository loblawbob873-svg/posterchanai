import fs from 'node:fs';
import vm from 'node:vm';
globalThis.window=globalThis;
const load=(path,expose)=>vm.runInThisContext(
  fs.readFileSync(new URL(path,import.meta.url),'utf8')+`\nglobalThis.${expose}=${expose};`,
  {filename:path},
);
load('../../static/vendor/nostr/nostr.bundle.js','NostrTools');
load('../../static/js/client/cord-protocol.js','PosterCord');
load('../../static/js/client/cord-reader.js','PosterCordReader');

const ownerSk=Uint8Array.from({length:32},(_v,i)=>i+1);
const owner=NostrTools.getPublicKey(ownerSk);
const signEvent=async template=>NostrTools.finalizeEvent(template,ownerSk);
const made=await PosterCord.createCommunity({
  name:'Encrypted reaction fixture', owner, relays:['wss://relay.example'],
  base:'https://poster.place', signEvent,
});
const bundle={
  community_id:made.communityId, owner, owner_salt:made.secrets.ownerSalt,
  community_root:made.secrets.root, root_epoch:0, channels:[],
  relays:['wss://relay.example'], name:'Encrypted reaction fixture',
};
const controls=made.events.slice(0,2);
const channel=PosterCordReader.inspectControl(bundle,controls).channels[0];
if(!channel)throw new Error('encrypted control wraps did not reveal the general channel');

const message=await PosterCordReader.createChatWrap(
  bundle,controls,channel.id,'react to me',owner,signEvent,
);
const shortcode='carlJAM',url='https://emoji.example/carlJAM.gif';
const reaction=await PosterCordReader.createChatWrap(
  bundle,controls,channel.id,`:${shortcode}:`,owner,signEvent,
  [['e',message.rumorId],['emoji',shortcode,url]],7,
);
if(reaction.wrap.kind!==1059 || reaction.wrap.content.includes(shortcode) || reaction.wrap.content.includes(url))
  throw new Error('kind-7 reaction was not fully encrypted on the relay wire');

const opened=await PosterCordReader.inspectChat(bundle,controls,channel.id,[message.wrap,reaction.wrap]);
const reactions=new Map(opened.reactions).get(message.rumorId)||[];
const urls=new Map(opened.reactionUrls).get(message.rumorId)||[];
if(JSON.stringify(reactions)!==JSON.stringify([[`:${shortcode}:`,[owner]]]))
  throw new Error('reader did not decrypt the custom kind-7 reaction');
if(JSON.stringify(urls)!==JSON.stringify([[`:${shortcode}:`,url]]))
  throw new Error('reader discarded the decrypted NIP-30 reaction URL');

const noop=()=>{};
globalThis.document={querySelector:()=>({}),createElement:()=>({dataset:{}}),head:{appendChild:noop},documentElement:{appendChild:noop},addEventListener:noop};
globalThis.addEventListener=noop;
load('../../static/js/client/concord.js','PCConcord');
const pc={enc:String,viewer:()=>({pubkey:owner})};
const image=PCConcord.reactionSummary(pc,{id:message.rumorId,reactions:Object.fromEntries(reactions),reactionUrls:Object.fromEntries(urls)});
if(!image.includes('<img') || !image.includes(url) || image.includes(`>${shortcode}<`))
  throw new Error('Concord did not render the decrypted custom reaction image');
const fallback=PCConcord.reactionSummary(pc,{id:message.rumorId,reactions:{':missing:':[owner]},reactionUrls:{}});
if(!fallback.includes(':missing:') || fallback.includes('<img'))
  throw new Error('Concord custom-reaction fallback is not readable');

console.log('encrypted Concord custom-reaction wire fixture ok');
