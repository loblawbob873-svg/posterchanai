/* THE COMPOSER SURVIVES A LIVE MESSAGE, AND THE LIVE MESSAGE IS DRAWN.
 *
 * Runs the SHIPPED backgroundRender()/patchMessageList() pair against a stub document. A
 * source-literal test cannot see the thing that was actually broken: the paint was deferred, which
 * is invisible in the source and looks like "new messages only appear after I send or switch
 * rooms" on screen. */
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';

const code=fs.readFileSync(process.env.PC_CONCORD_SOURCE||new URL('../../static/js/client/concord.js',import.meta.url),'utf8');
const slice=code.slice(code.indexOf('  function backgroundRender(){'),code.indexOf('  function handoffState('));

/* One builder, two painters: render() and patchMessageList() must call the same one, or the two
 * paints can disagree about what a message looks like. */
assert.equal((code.match(/function messagesPaneHtml\(/g)||[]).length,1,
  'one builder, so the two paints cannot disagree about what a message looks like');
for(const painter of ['  function render(){','  function patchMessageList(){']){
  const from=code.indexOf(painter),body=code.slice(from,code.indexOf('\n  }',from));
  assert(body.includes('messagesPaneHtml(p,'),painter.trim()+' must build its rows with the shared builder');
}

const el=(tag,attrs={})=>{
  const node={tag,children:[],parent:null,value:'',isConnected:true,dataset:{},listeners:{},...attrs,
    innerHTML:'',
    appendChild(c){c.parent=node;node.children.push(c);return c;},
    contains(other){for(let n=other;n;n=n.parent)if(n===node)return true;return false;},
    closest(){return null;},
    addEventListener(name,fn){(node.listeners[name]=node.listeners[name]||[]).push(fn);}};
  return node;
};

const composer=el('textarea',{id:'cc-input'});
composer.value='half a sentence';
const pane=el('div',{cls:'cc-messages'});
const paneButton=el('button');pane.appendChild(paneButton);
const feed=el('div',{id:'feed'});feed.appendChild(pane);feed.appendChild(composer);

let renders=0,binds=0,hydrated=0,restores=0;
const state={community:0,channel:'general'};
const room={communityId:'room-1',cord:{bundle:{}},channels:[{id:'c1',name:'general'}]};

let reads=0;
const context={console,Map,Set,Promise,JSON,String,Number,Math,setTimeout,
  state,mobileChatOpen:false,mobileDrawerOpen:false,
  matchMedia:()=>({matches:false}),
  conversationIsVisible:(n,c,d)=>!n||(!!c&&!d),
  markRead:()=>{reads++;},
  saved:()=>[room],
  paintedMessages:()=>[{id:'m1',by:'someone',text:'live message',at:1,kind:9}],
  messagesPaneHtml:()=>'<div class="cc-message-list">PAINTED</div>',
  render:()=>{renders++;},
  bindMessages:()=>{binds++;},
  restoreChatScroll:()=>{restores++;},
  wireRoomMedia:()=>{},
  hydrateEncryptedAttachments:()=>{hydrated++;},
  hydrateWebxdcCards:()=>{},
  PC:()=>({$:sel=>sel==='#feed'?feed:null,isView:v=>v==='concord',viewer:()=>({pubkey:'me',npub:'npub1meme',profile:{name:'Me'}}),niceNip05:x=>x}),
  document:{body:{},documentElement:{},activeElement:composer,
    querySelector:sel=>sel==='#cc-input'?composer:(sel==='.cc-messages'?pane:null)}};
context.window=context;
vm.createContext(context);
vm.runInContext('let backgroundRenderPending=false,backgroundFocusHost=null;\n'+slice,context);

/* 1. Typing in the composer: the messages ARE painted, the full render is still deferred, and the
 *    textarea is the same element carrying the same text. */
assert.equal(context.backgroundRender(),false,'a focused composer still defers the full render');
assert.equal(renders,0,'render() would replace the textarea and close a soft keyboard');
assert.equal(pane.innerHTML,'<div class="cc-message-list">PAINTED</div>','the arriving message is drawn');
assert.equal(binds,1,'the replaced rows are re-bound, or every reply/react/delete button is dead');
assert.equal(hydrated,1);assert.equal(restores,1);
assert.equal(reads,1,'a channel you are reading and typing into must not grow an unread mark');
assert.equal(context.document.querySelector('#cc-input'),composer,'the composer element is untouched');
assert.equal(composer.value,'half a sentence','the half-typed message survives the repaint');
assert.equal((composer.listeners.focusout||[]).length,1,'the deferred full render is still armed');

/* 2. Focus INSIDE the pane — an open picker, a poll button — is the same loss one level down. */
pane.innerHTML='';context.document.activeElement=paneButton;
assert.equal(context.patchMessageList(),false,'a focused control inside the pane is not ripped out');
assert.equal(pane.innerHTML,'','the pane is left for the full render');

/* 3. Nothing focused: the ordinary full render, unchanged. */
context.document.activeElement=context.document.body;
assert.equal(context.backgroundRender(),true);
assert.equal(renders,1,'with nothing to protect the whole workspace repaints as before');

/* 4. On the windowed desktop the feed belongs to the focused window. */
context.document.activeElement=composer;
context.PCOS={isOn:()=>true,ownsFeedView:()=>false};
pane.innerHTML='';
assert.equal(context.patchMessageList(),false,'never paint into a window somebody else is using');
assert.equal(pane.innerHTML,'');

/* 5. TWO LIVE PATHS, ONE SCREEN — a fix to one of them is half a fix. NIP-29 rooms flush through
 *    flushChatLive's own branch and Cord rooms through absorbChatWraps; both must reach the paint,
 *    which they do by both going through backgroundRender(). */
for(const [name,from,to] of [
    ['the NIP-29 live path','  async function flushChatLive(','  function mergeCordTimeline('],
    ['the Cord live path','  async function absorbChatWraps(','  async function refreshActiveChannel(']]){
  const body=code.slice(code.indexOf(from),code.indexOf(to));
  assert(body.includes('backgroundRender()'),name+' never reaches the paint');
}

console.log('concord live repaint keeps the composer and still draws the message');
