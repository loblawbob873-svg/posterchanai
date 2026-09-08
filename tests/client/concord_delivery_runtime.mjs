// Run inside concord_runtime's real composer/room setup (see Python launcher).
const assert=(yes,msg)=>{if(!yes)throw new Error(msg);};
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
const owner='a'.repeat(64),other='c'.repeat(64),originalViewer=window.__PC.viewer;
let signCount=0,releaseSign,signMode='ok',ackMode='unknown';
const sentPackets=[],decoded=new Map();
const sandbox={window:{},self:{},Worker:class{postMessage(){}},console,setTimeout,clearTimeout,
 WebSocket:class{
  constructor(url){this.url=url;this.readyState=0;queueMicrotask(()=>{this.readyState=1;this.onopen?.();});}
  send(raw){const packet=JSON.parse(raw);sentPackets.push({url:this.url,packet});if(ackMode!=='unknown')queueMicrotask(()=>this.onmessage?.({data:JSON.stringify(['OK',packet[1].id,ackMode==='ok','fixture'])}));}
  close(){this.readyState=3;}
 }};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync('static/js/client/relay.js','utf8'),sandbox);
const actualRelay=sandbox.window.Relay;
actualRelay._conns=new Map([...roomRelaySet,'wss://unrelated.example'].map(url=>[url,{ws:{readyState:1},_send(){throw Error('managed pool was mutated');}}]));
window.__PC.relayPublishRoom=(urls,event)=>actualRelay.publishTo(urls,event,{includeManaged:true,detailed:true,timeout:10});
window.__PC.signTemplate=async template=>{signCount++;if(signMode==='reject')throw Error('signer declined');if(signMode==='defer')await new Promise(resolve=>releaseSign=resolve);return {...template,sig:'fixture-signature'};};
PosterCordReader.createChatWrap=async(_bundle,_wraps,_channel,text,author,sign,tags,kind)=>{
 await sign({kind:20013,content:'ciphertext',tags:[]});
 const id='delivery-'+signCount,wrap={id:'wrap-'+id,kind:1059,content:'encrypted-'+id,created_at:42,pubkey:'f'.repeat(64),sig:'fixture-signature',tags:[]};
 decoded.set(wrap.id,{id,pubkey:author,text,at:Date.now(),kind,tags});return {rumorId:id,wrap,ms:decoded.get(wrap.id).at};
};
PosterCordReader.inspectChat=async(_bundle,_controls,_channel,wraps)=>({messages:wraps.map(w=>decoded.get(w.id)).filter(Boolean),reactions:[],reactionIds:[]});
const submit=text=>{const input=control('cc-input');input.value=text;input.dispatchEvent({type:'input'});return control('cc-send').click();};
const rowFor=text=>JSON.parse(messageData.get(raceKey)).find(m=>m.text===text);
const retryFor=id=>dollars('[data-cc-retry-delivery]').find(b=>b.dataset.ccRetryDelivery===id);

signMode='reject';await submit('declined draft');
assert(control('cc-input').value==='declined draft','declined signature lost draft');
assert(sentPackets.length===0&&rowFor('declined draft').delivery==='failed','declined signature was published or looked sent');
assert(feed.innerHTML.includes('Not sent'),'signer failure not visible');

signMode='defer';const pending=submit('retry ciphertext exactly');await tick();
assert(signCount===2&&sentPackets.length===0,'did not wait on real sign callback');
assert(feed.innerHTML.includes('Waiting for signer'),'signing not visible');
releaseSign();await pending;
const unknown=rowFor('retry ciphertext exactly');
assert(unknown.delivery==='unknown'&&!unknown.remote,'missing ACK looked sent');
assert(control('cc-input').value==='','unknown delivery restored blind resend draft');
assert(feed.innerHTML.includes('Delivery unknown')&&retryFor(unknown.id),'unknown state has no explicit recovery');
assert(sentPackets.length===roomRelaySet.slice(0,4).length,'managed room endpoints were skipped');
assert(sentPackets.every(x=>roomRelaySet.includes(x.url)),'envelope escaped room relays');
assert(![...data.values()].some(v=>String(v).includes('retry ciphertext exactly')),'private message persisted plaintext');
const encryptedSnapshot=JSON.stringify([...pendingDeliveryCache]);
assert(!encryptedSnapshot.includes('retry ciphertext exactly'),'pending journal stored decrypted text');
const originalWrap=JSON.stringify(sentPackets[0].packet[1]);

// Re-evaluate actual Concord module with only durable encrypted cache retained. No implicit send.
vm.runInThisContext(fs.readFileSync(concordSource,'utf8').replace('window.PCConcord={','window.__deliveryTest={recover:recoverDeliveries,controls:roomControls};window.__concordStore={get:testMessages,set:saveTestMessages};window.PCConcord={'));
const reloadedRoom=JSON.parse(data.get('pc.concord.invites'))[0],channel=reloadedRoom.channels.find(c=>c.name==='general');
PosterCordReader.inspectControl=()=>({name:reloadedRoom.name,controlPubkeys:['8'.repeat(64)],channels:reloadedRoom.channels.map(c=>({...c,streamPubkeys:['6'.repeat(64)]}))});
window.__deliveryTest.controls.set(reloadedRoom.communityId||reloadedRoom.naddr,[{id:'control'}]);
window.__PC.viewer=()=>({...originalViewer(),pubkey:other});
await window.__deliveryTest.recover(window.__PC,reloadedRoom,channel);
assert(!window.__concordStore.get(channelStoreKey()).some(m=>m.text==='retry ciphertext exactly'),'other account recovered private pending send');
function channelStoreKey(){return reloadedRoom.naddr;}
window.__PC.viewer=originalViewer;
await window.__deliveryTest.recover(window.__PC,reloadedRoom,channel);PCConcord.render();await tick();
assert(rowFor('retry ciphertext exactly')?.delivery==='unknown','reload lost pending ciphertext');
assert(signCount===2&&sentPackets.length===4,'reload automatically signed or published');
ackMode='ok';await retryFor(unknown.id).onclick();await tick();
assert(signCount===2,'retry requested an extra signature');
assert(sentPackets.slice(4).every(x=>JSON.stringify(x.packet[1])===originalWrap),'retry changed signed wrap');
assert(rowFor('retry ciphertext exactly').delivery==='sent','positive ACK did not mark sent: '+JSON.stringify({row:rowFor('retry ciphertext exactly'),toasts:calls.toasts.slice(-2)}));
assert([...pendingDeliveryCache.values()].every(m=>m.size===0),'ACK left resurrectable pending record');
PCConcord.render();
assert(/role="status" style="[^"]*clip:rect\(0,0,0,0\)[^"]*">Sent<\/span>/.test(feed.innerHTML),'successful delivery still visibly labels every message');

// An account change while signature is pending stores only owner-bound ciphertext, sends nothing.
ackMode='ok';signMode='defer';const switched=submit('owner changed while signing');await tick();
const beforeSwitchPackets=sentPackets.length;
window.__PC.viewer=()=>({...originalViewer(),pubkey:other});releaseSign();await switched;
assert(sentPackets.length===beforeSwitchPackets,'old account send escaped after account change');
PCConcord.render();assert(!feed.innerHTML.includes('owner changed while signing'),'other account sees pending owner text');
window.__PC.viewer=originalViewer;PCConcord.render();
assert(rowFor('owner changed while signing')?.delivery==='unknown','sending owner cannot recover interrupted send');
console.log('Concord actual composer/sign callback/Relay delivery scenarios passed');
process.exit(0);
