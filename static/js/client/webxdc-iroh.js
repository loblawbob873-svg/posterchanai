/* Webxdc realtime transport compatible with Armada/Vector/Delta Chat.
 *
 * The Webxdc API does not prescribe a carrier, but the interoperable ecosystem uses Iroh Gossip.
 * Concord only carries encrypted CORD-04 peer advertisements; realtime frames never ride Nostr.
 */
(function(){
  'use strict';
  const B32='ABCDEFGHIJKLMNOPQRSTUVWXYZ234567',TRAILER=36,MAX_ADDR=2048;
  let nodePromise=null;
  function diag(stage,detail){try{if(window.PCWebxdc&&PCWebxdc.rtDiagnostic)PCWebxdc.rtDiagnostic(stage,detail||'');else console.info('[webxdc realtime]',stage,detail||'');}catch(_){}}
  function decode32(s){
    if(!/^[A-Z2-7]{52}$/.test(String(s||'')))return null;
    let out=[],buf=0,bits=0;for(const c of s){buf=(buf<<5)|B32.indexOf(c);bits+=5;if(bits>=8){bits-=8;out.push((buf>>>bits)&255);}}
    return new Uint8Array(out);
  }
  function encode32(bytes){let out='',buf=0,bits=0;for(const b of bytes){buf=(buf<<8)|b;bits+=8;while(bits>=5){bits-=5;out+=B32[(buf>>>bits)&31];}}if(bits)out+=B32[(buf<<(5-bits))&31];return out;}
  function encodeAddr(json){return encode32(new TextEncoder().encode(json));}
  function decodeAddr(s){if(typeof s!=='string'||!s||s.length>MAX_ADDR)return null;try{const b=decodeLoose(s),v=new TextDecoder('utf-8',{fatal:true}).decode(b);JSON.parse(v);return v;}catch(_){return null;}}
  function decodeLoose(s){let out=[],buf=0,bits=0;for(const c of String(s).toUpperCase()){const v=B32.indexOf(c);if(v<0)throw new Error('bad base32');buf=(buf<<5)|v;bits+=5;if(bits>=8){bits-=8;out.push((buf>>>bits)&255);}}return new Uint8Array(out);}
  function parseSignal(row,topic){try{const v=JSON.parse(row.content);if(v.topic!==topic||(v.op!=='ad'&&v.op!=='left'))return null;if(v.op==='ad'&&(typeof v.addr!=='string'||!v.addr||v.addr.length>MAX_ADDR))return null;return v;}catch(_){return null;}}
  function hexBytes(hex){return new Uint8Array(String(hex).match(/../g).map(x=>parseInt(x,16)));}
  function hex(bytes){return Array.from(bytes,x=>x.toString(16).padStart(2,'0')).join('');}
  function frame(payload,seq,key){const out=new Uint8Array(payload.length+TRAILER);out.set(payload);new DataView(out.buffer).setUint32(payload.length,seq>>>0,true);out.set(key,payload.length+4);return out;}
  function unframe(bytes){bytes=new Uint8Array(bytes);if(bytes.length<TRAILER)return null;const cut=bytes.length-TRAILER;return{payload:bytes.slice(0,cut),sender:hex(bytes.slice(cut+4))};}
  async function transport(){
    if(!nodePromise)nodePromise=(async()=>{const mod=await import('/static/vendor/webxdc-rt/webxdc_rt.js');await mod.default();return await new mod.RealtimeNode();})().catch(e=>{nodePromise=null;throw e;});
    return nodePromise;
  }
  async function join(topic,ctx,onMessage){
    diag('iroh-start',topic);
    const topicBytes=decode32(topic);if(!topicBytes||topicBytes.length!==32)throw new Error('attachment has no interoperable Webxdc topic');
    if(!ctx||ctx.protocol!=='concord2'||!window.PCConcord)throw new Error('Iroh multiplayer is available in Concord rooms');
    const node=await transport(),self=node.publicKeyHex(),key=hexBytes(self),latest=new Map();let dead=false,seq=0,off=null;diag('iroh-node',self.slice(0,16));
    /* Armada supplies known peers in the initial gossip join. Our old path joined an empty mesh and
     * learned every address later; addPeer could mark the id joined before its relay QUIC connection
     * completed, leaving two advertised players with no gossip neighbor. Fold durable CORD-04 ads
     * first and bootstrap the topic with their complete endpoint addresses. */
    let initial=[];
    try{const rows=PCConcord.webxdcPeerQuery?await PCConcord.webxdcPeerQuery(ctx):[];for(const row of rows){const sig=parseSignal(row,topic);if(!sig)continue;const at=Number(row.at)||Number(row.created_at)*1000||0,prior=latest.get(row.pubkey);if(prior&&(prior.at>at||(prior.at===at&&prior.op==='left')))continue;latest.set(row.pubkey,{at,op:sig.op,addr:sig.addr});}for(const value of latest.values())if(value.op==='ad'){const addr=decodeAddr(value.addr);if(addr)initial.push(addr);}diag('peer-bootstrap',String(initial.length));}catch(e){diag('peer-bootstrap-failed',e&&e.message||e);}
    await node.join(topicBytes,initial,bytes=>{if(dead)return;const got=unframe(bytes);if(got&&got.sender!==self)onMessage(got.payload);},msg=>{try{console.debug('[webxdc] iroh:',msg);}catch(_){};diag('iroh-event',msg);});
    diag('iroh-joined',topic);
    try{
      off=await PCConcord.webxdcPeerSubscribe(ctx,row=>{if(dead)return;const sig=parseSignal(row,topic);if(!sig)return;const at=Number(row.at)||Number(row.created_at)*1000||0,prior=latest.get(row.pubkey);if(prior&&(prior.at>at||(prior.at===at&&prior.op==='left')))return;latest.set(row.pubkey,{at,op:sig.op});if(sig.op!=='ad')return;const addr=decodeAddr(sig.addr);if(!addr){diag('peer-address-invalid',row.pubkey||'');return;}try{if(JSON.parse(addr).id===self)return;}catch(_){return;}diag('peer-dial',row.pubkey||'');void node.addPeer(topicBytes,addr).then(()=>diag('peer-added',row.pubkey||'')).catch(e=>diag('peer-add-failed',e&&e.message||e));});
      diag('peer-subscribed',topic);
      await PCConcord.webxdcPeerPublish(ctx,JSON.stringify({op:'ad',topic,addr:encodeAddr(node.nodeAddrJson())}),off);
      diag('peer-advertised',topic);
    }catch(e){diag('peer-setup-failed',e&&e.message||e);try{node.leave(topicBytes);}catch(_){}throw e;}
    return{
      send(data){if(dead)return;seq++;return node.send(topicBytes,frame(new Uint8Array(data),seq,key));},
      async leave(){if(dead)return;dead=true;try{await PCConcord.webxdcPeerPublish(ctx,JSON.stringify({op:'left',topic}),off);}catch(_){}try{off&&off();}catch(_){}try{node.leave(topicBytes);}catch(_){}}
    };
  }
  window.PCWebxdcIroh={join,decodeTopic:decode32,encodeNodeAddr:encodeAddr,decodeNodeAddr:decodeAddr,frame,unframe,parseSignal};
})();
