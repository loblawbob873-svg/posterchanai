'use strict';
/* A PAYMENT RAIL THE TIP SHEET CAN USE MUST BE VISIBLE ON THE CARD.
 *
 * Reported about deallen@erybody.com: "i can zap him but no icon that he has monero like the
 * others". Measured on his real events — his kind-0 carries a `lud16` and nothing else, and his
 * NIP-A3 kind-10133 carries exactly one `payto monero`. The tip SHEET resolves 10133, so paying
 * him worked; the card MARK was resolved from the kind-0 alone, so nothing on screen ever said he
 * takes Monero. Two surfaces answering the same question from different facts, which is the
 * failure this repo keeps finding, and the reason it is invisible is that the half that handles
 * money was right.
 *
 * This runs the SHIPPED app.js — the learn step, the render mark and the late patch — against
 * REAL SIGNED events, because an unsigned 10133 must teach the client nothing.
 */
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),crypto=require('crypto'),path=require('path');
const root=path.resolve(__dirname,'../..'),src=fs.readFileSync(root+'/static/js/client/app.js','utf8');
function part(start,end){const a=src.indexOf(start);assert(a>=0,'missing slice start: '+start);
  const b=src.indexOf(end,a+start.length);assert(b>a,'missing slice end: '+end);return src.slice(a,b);}

/* A DOM small enough to read and real enough to branch on: `_tipMarks` asks for one element, reads
 * and writes a dataset, appends children and walks one `closest`. Nothing else. */
function card(){
  const marks=[],el={
    className:'act actz',title:'',
    querySelector(sel){ if(sel==='.'+'xmr-mark'||sel==='.xmr-mark')return marks.find(m=>m.className==='xmr-mark')||null;
                        if(sel==='.bch-mark')return marks.find(m=>m.className==='bch-mark')||null;
                        return null; },
    appendChild(c){ marks.push(c); return c; },
    closest(){ return el.act; }};
  el.act={title:''};
  const node={dataset:{},querySelector:sel=>sel==='.actz .tipbolt'?el:null,marks,bolt:el};
  return node;
}

function setup(){
  const ctx={console,crypto:crypto.webcrypto,TextEncoder,TextDecoder,Uint8Array,Map,Set,Date,
             setTimeout,clearTimeout,JSON,Math,Number,String,Object,Array,RegExp,Promise};
  ctx.window=ctx;ctx.document={createElement:tag=>({tag,className:'',textContent:''}),
                               querySelectorAll:()=>[]};
  ctx.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
  ctx.addEventListener=()=>{};
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(root+'/static/vendor/nostr/nostr.bundle.js','utf8'),ctx);
  vm.runInContext(fs.readFileSync(root+'/static/js/client/store.js','utf8'),ctx);
  vm.runInContext(fs.readFileSync(root+'/static/js/client/payment-targets.js','utf8'),ctx);
  // Everything actsRow needs that is not part of what is under test.
  Object.assign(ctx,{
    enc:s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    REPLY_ICON:'<r>',RT_ICON:'<t>',QUOTE_ICON:'<q>',REACT_ICON:'<k>',ZAP_ICON:'<z>',
    BOOKMARKS:new Set(),fmtSats:n=>String(n),tipCountLabel:()=>'',
    countsFor:()=>({replies:0,reactions:0,reposts:0,zaps:0,zapN:0,iRt:false,sob:0,tips:null,tipN:0}),
    myReaction:()=>null,_repostAction:()=>({label:'repost',pending:false}),
    renderMe(){},decorateProfiles(){},Relay:{ready:async()=>true,query:async()=>[]},
  });
  vm.runInContext(
      part('  function profOf(pk){','  async function _ensurePaymentTargets(){')
    + part('  function isXmrAddr(a){','  // A Monero tip note')
    + part('  function isCashAddr(a){',"  // Open the payer's BCH wallet")
    + part('  function actsRow(ev){','  let _noteCardErrs = 0;')
    + part('  function _tipMarks(n, p){','  function decorateProfiles(){')
    + 'const _profQ=new Set(),_profMiss=new Map();'
    + part('  async function flushProfiles(){','  async function fetchMyProfile(){')
    + '\nthis.__api={actsRow,_tipMarks,flushProfiles,_advertises,_learnRailsFromEvent,xmrOf,bchOf,_profQ};',
    ctx);
  return ctx;
}

const XMR='48TAuDzhzyRCLBAP7v86GGBLrTB2PNyYn1y7DL86H7sj2rRt7mipXRN2vigRChENnZWFDHRzuPoU9WvUxSy3AecgGDaj8Vb';
const BCH='qr95sy3j9xwd2ap32xkykttr4cvcu7as4y0qverfuy';

function signed(ctx,sk,ev){ ctx.__tmp=JSON.stringify(ev);
  return ctx.NostrTools.finalizeEvent(vm.runInContext('JSON.parse(__tmp)',ctx),sk); }

async function run(){
  let checks=0;
  const sk=new Uint8Array(32).fill(7);

  /* THE REPORT, END TO END. An author whose ONLY Monero declaration is a payto: the batched
     profile REQ learns it, and the card drawn afterwards carries the mark. */
  {
    const ctx=setup(),api=ctx.__api,pk=ctx.NostrTools.getPublicKey(sk);
    const kind0=signed(ctx,sk,{kind:0,tags:[],created_at:1700000000,
      content:JSON.stringify({name:'DeAllen',lud16:'hotsleet@cake.cash'})});
    const rails=signed(ctx,sk,{kind:10133,tags:[['payto','monero',XMR],['alt','Payment targets']],
      created_at:1700000001,content:''});
    const note={id:'n1',pubkey:pk,kind:1,created_at:1700000002,tags:[],content:'hi'};

    // Before the batch lands there is nothing to know, and nothing is claimed.
    assert.equal(api._advertises(pk,'monero'),false);
    assert(!api.actsRow(note).includes('xmr-mark'),'no mark before anything is known');

    // The SAME REQ that fetches profiles must carry the 10133 filter — a second query per author
    // is what the resolver was deliberately written to avoid, so this must cost no round trip.
    const asked=[];
    ctx.Relay={ready:async()=>true,query:async f=>{asked.push(f);return [kind0,rails];}};
    api._profQ.add(pk);
    await api.flushProfiles();
    assert.equal(asked.length,1,'the rails must ride the profile REQ, not a second query');
    const kinds=asked[0].flatMap(f=>f.kinds);
    assert(kinds.includes(0)&&kinds.includes(10133),'one REQ, both kinds: '+JSON.stringify(kinds));

    assert.equal(api._advertises(pk,'monero'),true);
    const html=api.actsRow(note);
    assert(html.includes('xmr-mark'),'the card must mark an author who advertises Monero');
    assert(html.includes('Monero'),'and the tip button must say so out loud: '+html);
    // The kind-0 still has no address, so nothing may pretend to know one.
    assert.equal(api.xmrOf(JSON.parse(kind0.content)),'');
    checks++;
  }

  /* THE LATE PATCH. A card drawn before the batch landed is patched in place — and the MARK is
     raised without inventing a `data-xmr`, because the address a tip is paid to must keep coming
     from the verified resolver. */
  {
    const ctx=setup(),api=ctx.__api,pk=ctx.NostrTools.getPublicKey(sk);
    const rails=signed(ctx,sk,{kind:10133,tags:[['payto','monero',XMR]],created_at:1700000001,content:''});
    const node=card();node.dataset.pk=pk;
    api._tipMarks(node,{});
    assert.equal(node.marks.length,0,'nothing known yet, nothing marked');
    assert(api._learnRailsFromEvent(rails));
    api._tipMarks(node,{});
    assert.deepEqual(node.marks.map(m=>m.className),['xmr-mark']);
    assert.equal(node.dataset.xmr,undefined,
      'a rail carries no address, so it must never fill data-xmr — that is what a tip is paid to');
    assert(node.bolt.act.title.includes('Monero'));
    api._tipMarks(node,{});
    assert.equal(node.marks.length,1,'the pass runs on every profile batch and must be idempotent');
    checks++;
  }

  /* A PROFILE ADDRESS STILL WINS FOR THE ADDRESS ITSELF. The rail must only ever ADD a mark. */
  {
    const ctx=setup(),api=ctx.__api,pk=ctx.NostrTools.getPublicKey(sk);
    const rails=signed(ctx,sk,{kind:10133,tags:[['payto','monero',XMR]],created_at:1,content:''});
    api._learnRailsFromEvent(rails);
    const node=card();node.dataset.pk=pk;
    api._tipMarks(node,{monero_address:XMR});
    assert.equal(node.dataset.xmr,XMR,'a kind-0 address is still the address');
    assert.deepEqual(node.marks.map(m=>m.className),['xmr-mark']);
    checks++;
  }

  /* BITCOIN CASH RIDES THE SAME RAIL, and had the same hole. */
  {
    const ctx=setup(),api=ctx.__api,pk=ctx.NostrTools.getPublicKey(sk);
    const rails=signed(ctx,sk,{kind:10133,tags:[['payto','bitcoincash',BCH]],created_at:1,content:''});
    api._learnRailsFromEvent(rails);
    const note={id:'n',pubkey:pk,kind:1,created_at:1,tags:[],content:''};
    assert(api.actsRow(note).includes('bch-mark'));
    assert(api.actsRow(note).includes('Bitcoin Cash'));
    assert(!api.actsRow(note).includes('xmr-mark'),'a BCH rail must not claim Monero');
    checks++;
  }

  /* AN UNSIGNED OR FORGED 10133 TEACHES NOTHING. Anyone can write an event claiming anyone's
     pubkey, and a relay here is untrusted; the mark is only cosmetic, but a mark that can be
     raised on a stranger's post by a stranger is a lie on somebody else's card. */
  {
    const ctx=setup(),api=ctx.__api,pk=ctx.NostrTools.getPublicKey(sk);
    const real=signed(ctx,sk,{kind:10133,tags:[['payto','monero',XMR]],created_at:1,content:''});
    assert.equal(api._learnRailsFromEvent({...real,sig:'0'.repeat(128)}),false);
    assert.equal(api._advertises(pk,'monero'),false);
    assert.equal(api._learnRailsFromEvent({...real,kind:30078}),false);
    checks++;
  }

  /* IT IS REPLACEABLE, so dropping a rail must drop the mark. A stale mark sends somebody into a
     sheet that no longer offers what the icon promised. */
  {
    const ctx=setup(),api=ctx.__api,pk=ctx.NostrTools.getPublicKey(sk);
    api._learnRailsFromEvent(signed(ctx,sk,{kind:10133,
      tags:[['payto','monero',XMR],['payto','bitcoincash',BCH]],created_at:1,content:''}));
    assert(api._advertises(pk,'monero')&&api._advertises(pk,'bitcoincash'));
    api._learnRailsFromEvent(signed(ctx,sk,{kind:10133,tags:[['payto','monero',XMR]],
      created_at:2,content:''}));
    assert.equal(api._advertises(pk,'bitcoincash'),false,'a withdrawn rail must stop being marked');
    checks++;
  }

  /* NEWEST WINS INSIDE ONE BATCH. Relays hand back more than one copy of a replaceable event, and
     in a Map iteration order is arrival order, not time order. */
  {
    const ctx=setup(),api=ctx.__api,pk=ctx.NostrTools.getPublicKey(sk);
    const older=signed(ctx,sk,{kind:10133,tags:[['payto','monero',XMR],['payto','bitcoincash',BCH]],
      created_at:100,content:''});
    const newer=signed(ctx,sk,{kind:10133,tags:[['payto','monero',XMR]],created_at:200,content:''});
    ctx.Relay={ready:async()=>true,query:async()=>[newer,older]};   // newest first, as a relay may
    api._profQ.add(pk);
    await api.flushProfiles();
    assert.equal(api._advertises(pk,'bitcoincash'),false,'an older copy must not win');
    checks++;
  }

  console.log('ok '+checks+' payment-rail mark scenarios');
}
run().catch(e=>{console.error(e);process.exit(1);});
