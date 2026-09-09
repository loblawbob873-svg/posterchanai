"""Actual bundled feed/thread picker with trusted mouse/touch; all relay writes stay in fixture."""
import asyncio,json
import pytest
from tests.client import test_desktop_offline_full_app as desktop
@pytest.fixture(scope='module',autouse=True)
def bundle():yield from desktop.bundle.__wrapped__()
async def click(b,selector,touch=False):
 p=await b.js("(()=>{const el=document.querySelector("+json.dumps(selector)+"),r=el.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;if(!el.contains(document.elementFromPoint(x,y)))throw Error('target clipped');return {x,y}})()")
 if touch:
  await b.call('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[p]});await b.call('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
 else:
  for t in ['mousePressed','mouseReleased']:await b.call('Input.dispatchMouseEvent',{'type':t,'button':'left','clickCount':1,**p})
@pytest.mark.parametrize('context',['home','thread'])
@pytest.mark.parametrize('width',[1280,390])
def test_custom_pack_stays_open(context,width):
 async def check(b):
  await b.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':750,'deviceScaleFactor':1,'mobile':width<600})
  if width<600:await b.call('Emulation.setTouchEmulationEnabled',{'enabled':True})
  await desktop.login(b)
  await b.js("window.note=NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000),content:Array(40).fill('Thread emoji example').join('\\n'),tags:[]},new Uint8Array(32).fill(1));Store.saveEvent(note);window.__events=[note];__PC.timelineTop('home')")
  if context=='thread':
   await b.js("window.reply=NostrTools.finalizeEvent({kind:1,created_at:note.created_at+1,content:'A reply in the thread',tags:[['e',note.id,'','root'],['p',note.pubkey]]},new Uint8Array(32).fill(1));Store.saveEvent(reply);__events.push(reply);void __PC.openThread(note.id)")
  selector='.note[data-id="'+await b.js('reply.id' if context=='thread' else 'note.id')+'"] [data-a=react]'
  await b.until('!!document.querySelector('+json.dumps(selector)+')')
  await asyncio.sleep(.4)
  await b.js('document.querySelector('+json.dumps(selector)+').scrollIntoView({block:"end"})')
  await asyncio.sleep(.15)
  await click(b,selector,width<600)
  await b.until("!!document.querySelector('.ep-tab[data-tab=DRC]')")
  await asyncio.sleep(.3)
  await click(b,'.ep-tab[data-tab=DRC]',width<600)
  await asyncio.sleep(.15)
  assert await b.js("!!document.querySelector('.emoji-pop .ep-tab[data-tab=DRC].on')"),'custom pack vanished after actual tab mouse sequence'
  await b.js("const q=document.querySelector('.ep-q');q.value='drc';q.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('.ep-grid').scrollTop=200;document.querySelector('.ep-grid').dispatchEvent(new Event('scroll'))")
  assert await b.js("!!document.querySelector('.emoji-pop')&&document.querySelector('.ep-grid').scrollTop>0"),'internal search/scroll closed picker'
  await b.js("document.querySelector('.ep-grid').scrollTop=0")
  await click(b,'.ep-grid [data-e=":drc:"]',width<600)
  await b.until("__published.filter(e=>e.kind===7).length===1")
  assert await b.js("__published.find(e=>e.kind===7).content===':drc:'"), 'selected shortcode changed'
  assert not await b.js("!!document.querySelector('.emoji-pop')")
  await b.js('window.picks=0;__PC.openEmojiPopover(document.querySelector('+json.dumps(selector)+'),(e,close)=>{picks++;close()})')
  await asyncio.sleep(.3)
  for key in ['ArrowDown','Enter']:await b.call('Input.dispatchKeyEvent',{'type':'keyDown','key':key})
  assert await b.js('picks===1&&!document.querySelector(".emoji-pop")')
  await b.js('__PC.openEmojiPopover(document.querySelector('+json.dumps(selector)+'),()=>picks++)')
  await asyncio.sleep(.3)
  for event in ['mousePressed','mouseReleased']:await b.call('Input.dispatchMouseEvent',{'type':event,'button':'left','clickCount':1,'x':2,'y':2})
  assert await b.js('picks===1&&!document.querySelector(".emoji-pop")'),'outside click must close without selecting'
  await b.js('__PC.openEmojiPopover(document.querySelector('+json.dumps(selector)+'),()=>picks++);__PC.openThread("")')
  assert await b.js('!!document.querySelector(".emoji-pop")'),'invalid navigation must be a no-op'
  await b.js('__PC.openMenuPopover(document.querySelector('+json.dumps(selector)+'),[["fixture","Fixture"]],()=>picks++)')
  await asyncio.sleep(.2)
  assert await b.js('!document.querySelector(".emoji-pop")&&!!document.querySelector(".menu-pop")&&!__PC.openEmojiPopover.closeActive'),'replacement left stale picker cleanup'
  if width<600:assert await b.js('!!document.querySelector(".pop-backdrop")'),'old picker removed replacement backdrop'
  await b.js('__PC.openEmojiPopover(document.querySelector('+json.dumps(selector)+'),()=>picks++);__PC.switchView("settings")')
  await asyncio.sleep(.2)
  assert not await b.js('!!document.querySelector(".emoji-pop")'),'navigation left stale picker'

 extra="localStorage.setItem('pc_nostr_settings',JSON.stringify({osMode:false}));localStorage.setItem('pc_emoji_index',JSON.stringify({at:Math.floor(Date.now()/1000),base:'/client/emoji',emojis:[{s:'drc',p:'DRC',f:'drc.png'},...Array.from({length:180},(_,i)=>({s:'drc_extra'+i,p:'Other',f:i+'.png'}))]}));"
 asyncio.run(desktop.with_browser('online','',check,extra))
