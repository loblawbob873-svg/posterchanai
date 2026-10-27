"""👥 Share to Communities: a post's picture or video goes to the Communities channel you choose, in the SHIPPED client.

Asked 2026-10-26: "Add 'Share to Communities' button to social posts with a video/image, it shares the image to the
community/channel of your choosing. make sure UI good on mobile/OS/Web". Measured on a real post card, every press a
real mouse event at the control's centre after proving nothing covers it (music_art_fixture.press):

  * a post with an image or a video carries the button in its action row (and a ⋯ menu item); a text post has neither;
  * the action row -- now seven buttons on a media post -- still fits the narrowest phone, every button inside the card;
  * pressing it opens the channel picker OVER the post (no jump to Communities first); choosing a channel opens it,
    and its message box holds the post's image AND video as attachments (URL + imeta with the right type), the Blossom
    hash URL typed by asking the server -- nothing is uploaded again and nothing is sent until Send.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client import music_art_fixture as A
from tests.client.test_concord_react_full_app import ROOM
from tests.client.test_concord_share_into_channel_full_app import CLASSIC

CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
IMG = "https://media.example.com/" + "ab" * 32           # a Blossom address: no extension, typed by the server
VID = "https://media.example.com/clip.mp4"


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SEED = r"""(()=>{
  const k=new Uint8Array(32).fill(9), now=Math.floor(Date.now()/1000);
  const mk=(content,tags)=>NostrTools.finalizeEvent({kind:1,created_at:now-30,content,tags},k);
  const media=mk('look at these %s %s', [['imeta','url %s','m image/png']]);
  const text=mk('just words, nothing to share', []);
  Store.saveEvent(media); Store.saveEvent(text);
  window.__media=media.id; window.__text=text.id;
  const f=window.fetch; window.__heads=[];
  window.fetch=(u,o)=>{ if(String(u).startsWith('https://media.example.com/')){ __heads.push([String(u),(o&&o.method)||'GET']);
    return Promise.resolve(new Response('', {status:200, headers:{'content-type': String(u).endsWith('.mp4')?'video/mp4':'image/png'}})); }
    return f(u,o); };
  return true; })()""" % (IMG, VID, IMG)


async def _ready(b, w, h, os_mode=False):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2 if w <= 900 else 1,
                                                        "mobile": w <= 900})
    await desktop.login(b)
    await b.js("(()=>{window.__toasts=[];const t=__PC.toast;__PC.toast=(m)=>{__toasts.push(String(m));try{t(m)}catch(_){}};window.__publishOK=true;})()")
    await b.js(ROOM)
    if not os_mode:            # on the windowed desktop Communities is a window, opened when it is needed
        await b.until("!!document.querySelector('.cc-app')")
    await b.js(SEED)


async def _open(b, which):
    await b.js("__PC.openThread(window.%s); 1" % which)
    await b.until("!!document.querySelector('article.note[data-id=\"'+window.%s+'\"] .acts')" % which)
    await asyncio.sleep(.3)


ROW = r"""(()=>{const art=document.querySelector('article.note[data-id="'+window.__media+'"]'), acts=art.querySelector('.acts');
  const ar=art.getBoundingClientRect(), r=acts.getBoundingClientRect();
  const btns=[...acts.querySelectorAll(':scope > button')].filter(b=>b.checkVisibility());
  const tops=btns.map(b=>b.getBoundingClientRect().top), ws=btns.map(b=>b.getBoundingClientRect().width);
  const mine=acts.querySelector('[data-a="sharecomm"]'), mw=mine?mine.getBoundingClientRect().width:0;
  return {n:btns.length, inside:btns.every(b=>{const x=b.getBoundingClientRect();return x.left>=ar.left-0.5&&x.right<=ar.right+0.5}),
    // one line: icons of different glyphs sit a few px apart in their boxes; a WRAPPED row is a whole row lower
    oneLine:Math.max(...tops)-Math.min(...tops)<=6, sideways:document.documentElement.scrollWidth>innerWidth+1,
    asWide:mw>=Math.min(...ws.filter((_,i)=>btns[i]!==mine))-1, widths:ws.map(Math.round)}})()"""


@CHROME
@pytest.mark.parametrize("w,h", [(320, 640), (390, 844), (1280, 860)])
def test_a_posts_image_and_video_go_to_the_chosen_channel(w, h):
    got = {}

    async def check(b):
        await _ready(b, w, h)
        await b.js("__PC.switchView('home'); 1")
        await _open(b, "__text")
        got["text_btn"] = await b.js("!!document.querySelector('article.note[data-id=\"'+window.__text+'\"] [data-a=\"sharecomm\"]')")
        await _open(b, "__media")
        got["row"] = await b.js(ROW)
        await A.press(b, "article.note[data-id='%s'] [data-a='sharecomm']" % await b.js("window.__media"))
        await b.until("!!document.querySelector('[data-cc-share-channel]')")
        got["view_at_picker"] = await b.js("__PC.isView('concord')")
        got["fits"] = await b.js("[...document.querySelectorAll('.cc-share-channel')].every(e=>{const r=e.getBoundingClientRect();return r.height>=%d&&r.right<=innerWidth+1&&r.left>=0})" % (44 if w <= 820 else 24))
        await A.press(b, "[data-cc-share-channel='general']")
        await b.until("!!document.querySelector('#cc-input') && document.querySelector('#cc-input').value.includes('clip.mp4')")
        got["composer"] = await b.js("document.querySelector('#cc-input').value")
        got["view"] = await b.js("__PC.isView('concord')")
        got["sent"] = await b.js("(window.__wraps||[]).length")
        got["heads"] = await b.js("__heads.slice()")
        got["errors"] = await b.js("(window.__errors||[]).slice(-5)")

    asyncio.run(desktop.with_browser("online", "", check, CLASSIC))
    shape = (w, h)
    assert got["text_btn"] is True, (shape, "a text post must offer Share to Communities too (it shares its link)")
    assert got["row"]["n"] == 7 and got["row"]["inside"] and got["row"]["oneLine"] and not got["row"]["sideways"], \
        (shape, "the action row does not fit", got["row"])
    assert got["row"]["asWide"], (shape, "the Share to Communities button is smaller than its neighbours", got["row"]["widths"])
    assert got["view_at_picker"] is False, (shape, "the picker should open over the post, not after a jump to Communities")
    assert got["fits"], (shape, "the channel choices are not finger-sized and on screen")
    assert got["composer"].split() == [IMG, VID], (shape, "the composer does not hold the post's image and video", got["composer"])
    assert got["view"] and got["sent"] == 0, (shape, "sent without the person pressing Send", got)
    # The image's imeta states its type and the video's name does: neither is asked about.
    assert got["heads"] == [], (shape, "a typed address was asked about", got["heads"])
    assert not got["errors"], (shape, got["errors"])


@CHROME
def test_the_posts_more_menu_offers_it_too():
    got = {}

    async def check(b):
        await _ready(b, 390, 844)
        await b.js("__PC.switchView('home'); 1")
        await _open(b, "__media")
        await A.press(b, "article.note[data-id='%s'] .acts [data-a='menu']" % await b.js("window.__media"))
        await b.until("!!document.querySelector('.menu-pop')")
        got["items"] = await b.js("[...document.querySelectorAll('.menu-pop button')].map(e=>e.textContent.trim())")

    asyncio.run(desktop.with_browser("online", "", check, CLASSIC))
    assert "👥 Share to Communities" in got["items"], got["items"]


UNTYPED = r"""(()=>{
  const k=new Uint8Array(32).fill(8), now=Math.floor(Date.now()/1000);
  const ev=NostrTools.finalizeEvent({kind:1,created_at:now-20,content:'a clip %s',tags:[]},k);
  Store.saveEvent(ev); window.__untyped=ev.id; return true; })()""" % ("https://media.example.com/" + "cd" * 32)


@CHROME
def test_an_untyped_blossom_address_is_asked_once_and_attached_as_what_it_is():
    """A Blossom address has no extension and this post no imeta: the server is asked (HEAD) once, and the video is
    attached as a video, not guessed to be an image."""
    got = {}

    async def check(b):
        await _ready(b, 390, 844)
        await b.js(UNTYPED)
        await b.js("__PC.switchView('home'); 1")
        await _open(b, "__untyped")
        await A.press(b, "article.note[data-id='%s'] [data-a='sharecomm']" % await b.js("window.__untyped"))
        await b.until("!!document.querySelector('[data-cc-share-channel]')")
        await A.press(b, "[data-cc-share-channel='general']")
        await b.until("!!document.querySelector('#cc-input') && document.querySelector('#cc-input').value.includes('cdcd')")
        got["heads"] = await b.js("__heads.filter(h=>h[1]==='HEAD')")   # the card's own <img> GET is not a question

    asyncio.run(desktop.with_browser("online", "", check, CLASSIC))
    url = "https://media.example.com/" + "cd" * 32
    assert got["heads"] == [[url, "HEAD"]], ("the untyped address was not asked about exactly once", got["heads"])


@CHROME
def test_on_the_posterchanos_desktop_the_picker_opens_and_the_channel_window_gets_it():
    """The windowed desktop (OS mode): Communities is a window of its own, and the shared media reaches its composer."""
    got = {}

    async def check(b):
        await _ready(b, 1280, 860, os_mode=True)
        await b.js("__PC.switchView('home'); 1")
        await _open(b, "__media")
        await A.press(b, "article.note[data-id='%s'] [data-a='sharecomm']" % await b.js("window.__media"))
        await b.until("!!document.querySelector('[data-cc-share-channel]')")
        await A.press(b, "[data-cc-share-channel='general']")
        await b.until("[...document.querySelectorAll('#cc-input')].some(i=>i.value.includes('clip.mp4'))")
        got["os"] = await b.js("!!(window.PCOS && PCOS.isOn && PCOS.isOn())")
        got["errors"] = await b.js("(window.__errors||[]).slice(-5)")

    asyncio.run(desktop.with_browser("online", "", check, ""))
    assert got["os"], "this run was meant to be on the windowed desktop"
    assert not got["errors"], got["errors"]


# The real shape of the report (2026-10-27, "sharing to communities does not always work. communities opens but no
# link", then "that worked a second time"): Communities NOT opened yet this session, no room active, a community with
# more than one channel, and a fediverse GIF (the example post). The tests above open Communities first and only ever
# choose #general -- the one channel the first paint of a newly opened room draws.
GIF = "https://ak.kyaruc.moe/media/3cfc6648/73456155b3c8.gif"
COLD = r"""(()=>{
  const room={name:'Two rooms',communityId:'d'.repeat(64),naddr:'fixture-two',
    channels:[{id:'fx-general',name:'general'},{id:'fx-memes',name:'memes'}],cord:{bundle:{relays:['wss://fixture.invalid']},hydrated:true}};
  localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));
  localStorage.removeItem('pc.concord.active.v1.'+__PC.me().pubkey);
  window.__wraps=[];
  window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[{id:'fx-general',name:'general',streamPubkeys:[]},{id:'fx-memes',name:'memes',streamPubkeys:[]}]}),
    inspectChat:async()=>({messages:[],reactions:[],reactionIds:[]}),
    createChatWrap:async()=>{throw new Error('nothing is sent in this test')}};
  const k=new Uint8Array(32).fill(7), now=Math.floor(Date.now()/1000);
  const ev=NostrTools.finalizeEvent({kind:1,created_at:now-30,content:%r,tags:[['imeta','url '+%r,'m image/gif']]},k);
  Store.saveEvent(ev); window.__gif=ev.id; return true; })()""" % (GIF, GIF)


@CHROME
@pytest.mark.parametrize("channel", ["memes", "general"])
def test_the_first_share_of_a_session_lands_in_the_chosen_channel(channel):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
        await desktop.login(b)
        await b.js("window.__publishOK=true; 1")
        await b.js(COLD)
        await b.js("__PC.switchView('home'); 1")
        await _open(b, "__gif")
        await A.press(b, "article.note[data-id='%s'] [data-a='sharecomm']" % await b.js("window.__gif"))
        await b.until("!!document.querySelector('[data-cc-share-channel]')")
        await A.press(b, "[data-cc-share-channel='%s']" % channel)
        await b.until("!!document.querySelector('#cc-input')")
        await asyncio.sleep(2.5)                 # every repaint the room's opening does
        got["shown"] = await b.js("(document.querySelector('.cc-conversation header b')||{}).textContent||''")
        got["composer"] = await b.js("document.querySelector('#cc-input').value")
        got["errors"] = await b.js("(window.__errors||[]).slice(-5)")

    asyncio.run(desktop.with_browser("online", "", check, CLASSIC))
    assert got["shown"] == channel, ("the chosen channel is not the one showing", got)
    assert GIF in got["composer"], ("Communities opened with no link in the message box", got)
    assert not got["errors"], got["errors"]



@CHROME
@pytest.mark.parametrize("w,h", [(390, 844), (1280, 860)])
def test_a_post_with_no_media_shares_its_link(w, h):
    """Asked 2026-10-27: "Share to communities: if no media, share link to post in communities". A text post's 👥 puts
    the post's own link -- the address Copy link gives -- in the chosen channel's message box; nothing is sent."""
    got = {}

    async def check(b):
        await _ready(b, w, h)
        await b.js("__PC.switchView('home'); 1")
        await _open(b, "__text")
        await A.press(b, "article.note[data-id='%s'] [data-a='sharecomm']" % await b.js("window.__text"))
        await b.until("!!document.querySelector('[data-cc-share-channel]')")
        got["prompt"] = await b.js("document.querySelector('.modal p').textContent")
        await A.press(b, "[data-cc-share-channel='general']")
        await b.until("!!document.querySelector('#cc-input') && /nevent1/.test(document.querySelector('#cc-input').value)")
        got["composer"] = await b.js("document.querySelector('#cc-input').value")
        got["id"] = await b.js("window.__text")
        got["decoded"] = await b.js("(()=>{const m=document.querySelector('#cc-input').value.match(/nevent1[a-z0-9]+/);"
                                    "return m?NostrTools.nip19.decode(m[0]).data.id:''})()")
        got["sent"] = await b.js("(window.__wraps||[]).length")

    asyncio.run(desktop.with_browser("online", "", check, CLASSIC))
    shape = (w, h)
    assert "a link to this post" in got["prompt"], (shape, got["prompt"])
    assert got["decoded"] == got["id"], (shape, "the message box does not hold a link to THIS post", got)
    assert got["sent"] == 0, (shape, "sent without pressing Send")


# A message carrying a shared post's LINK, as Armada draws it: the post as a card, not a URL (2026-10-27, "about
# sharing a nostr event, we need to make sure we embed it right so it displays cleanly. What does armada do?").
EMBED_ROOM = r"""(()=>{
  const k=new Uint8Array(32).fill(6), now=Math.floor(Date.now()/1000);
  const post=NostrTools.finalizeEvent({kind:1,created_at:now-60,content:'the shared post itself',tags:[]},k);
  Store.saveEvent(post); window.__shared=post.id;
  const link=location.origin+'/'+NostrTools.nip19.neventEncode({id:post.id,author:post.pubkey,kind:1,relays:['wss://relay.poster.place']});
  const gone='nostr:'+NostrTools.nip19.neventEncode({id:'e'.repeat(64)});
  const room={name:'Embeds',communityId:'f'.repeat(64),naddr:'fixture-embeds',
    channels:[{id:'fx-general',name:'general'}],cord:{bundle:{relays:['wss://fixture.invalid']},hydrated:true}};
  localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));
  localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
  window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[{id:'fx-general',name:'general',streamPubkeys:[]}]}),
    inspectChat:async()=>({messages:[{id:'m-link',pubkey:'b'.repeat(64),text:'look at this '+link,at:Date.now()-60000,kind:9,tags:[]},
                                     {id:'m-gone',pubkey:'b'.repeat(64),text:gone,at:Date.now()-50000,kind:9,tags:[]}],reactions:[],reactionIds:[]}),
    createChatWrap:async()=>{throw new Error('nothing is sent')}};
  __PC.fetchEvent=async()=>null;   // the missing post: nobody has it
  __PC.switchMessagesTab('concord'); return link; })()"""


@CHROME
@pytest.mark.parametrize("w,h", [(390, 844), (1280, 860)])
def test_a_shared_post_link_is_drawn_as_the_post(w, h):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": w <= 900})
        await desktop.login(b)
        got["link"] = await b.js(EMBED_ROOM)
        await b.js("(()=>{const app=document.querySelector('.cc-app');if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}})()")
        await b.until("[...document.querySelectorAll('.cc-message')].length>=2")
        await b.until("![...document.querySelectorAll('.cc-note-load')].some(e=>e.dataset.ccNote)", )
        got["msgs"] = await b.js("""[...document.querySelectorAll('.cc-message')].map(m=>({text:(m.querySelector('p')||{}).textContent||'',
          card:!!m.querySelector('.quoted[data-open]'), cardText:(m.querySelector('.quoted')||{}).textContent||'',
          wide:(()=>{const q=m.querySelector('.quoted');return q?q.getBoundingClientRect().right<=innerWidth+1:true})()}))""")
        got["errors"] = await b.js("(window.__errors||[]).slice(-5)")

    asyncio.run(desktop.with_browser("online", "", check, CLASSIC))
    link_msg = next(m for m in got["msgs"] if "look at this" in m["text"])
    assert link_msg["card"] and "the shared post itself" in link_msg["cardText"], ("the shared post is not drawn as a card", got)
    assert "nevent1" not in link_msg["text"], ("the raw link is still in the message", link_msg)
    gone = [m for m in got["msgs"] if "not found" in m["cardText"]]
    assert gone, ("a post nobody has must say so, not spin", got["msgs"])
    assert all(m["wide"] for m in got["msgs"]), ("a card runs off the screen", got["msgs"])
    assert not got["errors"], got["errors"]
