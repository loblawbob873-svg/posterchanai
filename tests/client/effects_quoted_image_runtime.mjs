import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(process.argv[2],'utf8');
const helper=source.slice(source.indexOf('  function postImageUrl('),source.indexOf('  // Shared launcher for the Effects studio:'));
const action=source.slice(source.indexOf('  async function effectPost('),source.indexOf('  // Consumed by aiMount once'));
const id=n=>n.toString(16).padStart(64,'0');
const image='https://image.nostr.build/3d7eafb2958028c0c3d46fcb31af6401f556223edbee09834187699093ae0eae.png';
const outer='0000cd75c4d2107b8cb789dc9d8b5408ff0896572233b0a728ddd936ba3ce214';
const quoted='ad1c9afe92bbd19446edf4e1785de24dd29ee9d91fdb2de2f60a1d04d6a53dea';
const make=(i,content='',tags=[])=>({id:i,pubkey:id(99),kind:1,content,tags});
async function run(name,{events,remote=[],expected,fetches=[],fail=false}){
 const cache=new Map(events.map(e=>[e.id,e])),network=new Map(remote.map(e=>[e.id,e]));
 const seen=[],launched=[],toasts=[];
 const context=vm.createContext({Store:{get:i=>cache.get(i),saveEvent:e=>cache.set(e.id,e)},
  fetchEvent:async i=>{seen.push(i);if(fail)throw Error('relay unavailable');return network.get(i);},
  toast:m=>toasts.push(m),launchEffectStudio:(url,reply)=>launched.push({url,reply})});
 vm.runInContext(helper+'\n'+action,context);
 await context.effectPost(events[0].id,events[0].pubkey);
 assert.deepEqual(seen,fetches,name+' fetches');
 if(expected){assert.equal(launched.length,1,name);assert.equal(launched[0].url,expected,name);
  assert.equal(launched[0].reply.id,events[0].id,name+' original reply ID');
  assert.equal(launched[0].reply.pk,events[0].pubkey,name+' original reply author');assert.equal(toasts.length,0,name);
 }else{assert.equal(launched.length,0,name);assert.equal(toasts.length,1,name);}
 console.log('PASS '+name);
}
const root=make(outer,'Comment about the quoted image',[['q',quoted,'']]);
const photo=make(quoted,image,[['imeta','url '+image,'m image/png']]);
await run('reported cached quote',{events:[root,photo],expected:image});
await run('quoted image fetched and cached',{events:[root],remote:[photo],expected:image,fetches:[quoted]});
await run('direct image wins over quote',{events:[make(outer,image,[['q',quoted]])],expected:image});
await run('direct imeta-only image',{events:[make(outer,'',[['imeta','url '+image]])],expected:image});
await run('extensionless Blossom image',{events:[make(outer,'https://media.poster.place/'+id(1))],expected:'https://media.poster.place/'+id(1)});
await run('nested quote image',{events:[root,make(quoted,'',[['q',id(3)]]),make(id(3),image)],expected:image});
await run('quote cycle terminates',{events:[root,make(quoted,'',[['q',outer]])]});
await run('self quote terminates',{events:[make(outer,'',[['q',outer]])]});
await run('missing quote',{events:[root],fetches:[quoted]});
await run('failed relay is handled',{events:[root],fail:true,fetches:[quoted]});
await run('reply parent is not a quote',{events:[make(outer,'',[['e',quoted,'','reply']]),photo]});
await run('invalid quote ID is ignored',{events:[make(outer,'',[['q','not-an-event']])]});
await run('text-only post',{events:[make(outer,'No image')]});
await run('video-only post',{events:[make(outer,'https://media.example/video.mp4')]});
const chain=Array.from({length:12},(_,i)=>make(id(i+1),'',[['q',id(i+2)]]));
await run('quote depth is bounded',{events:[chain[0]],remote:chain.slice(1),fetches:chain.slice(1,8).map(e=>e.id)});
