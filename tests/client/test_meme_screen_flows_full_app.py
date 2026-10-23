"""The Meme Builder, driven end to end in the shipped client against the node's REAL render route.

  render   app/routers/client.py `POST /client/meme/render` mounted in-process (discover_flows_harness.Routers),
           rendering with the shipped meme_builder_service and the machine's own ffmpeg; the layer media it fetches
           comes off the instance's Blossom over HTTP, through the route's own guarded fetch. Only the fleet load
           balancer (which would ask the peers named in this machine's settings) and the stats counter (a file on
           this machine) are taken out.
  blossom  the instance's Blossom (wallpaper_harness.Instance): a dropped photo and the finished meme are real
           BUD-02 uploads, each signed by the shipped client; a per-device fault makes an upload answer 503.
  relay    the node's own relay: the post, and the reply, are the shipped composer's / builder's own publishes.

Flows: a photo dropped on the stage + a caption -> Still -> Post to Nostr -> the composer -> Send: the note on the
relay carries the Blossom URL of the RENDERED picture, and another account's fresh device shows that picture. Opened
from somebody's post (⋯ -> Meme Builder), the finished meme is posted back as ONE reply threaded to it. A render the
node refuses (503), cannot be reached for (dropped) or answers late; an upload that fails (the dropped photo, the
finished meme): each says what happened, and nothing broken or empty is ever posted.

AI features of the builder (image generation, the talking head, face swap, magic erase) are out of scope.
Harness: tests/client/discover_flows_harness.py. Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/meme.js=...".
"""
import json

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R
from tests.client import wallpaper_harness as H

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('meme',)
FIRST_FILL = 90            # 90 .. 113 (account keys are one byte; each module has its own node)
SCENARIOS = {}
COST = {'a_meme_is_posted_with_its_picture_and_a_fresh_device_sees_it': 30,
        'a_meme_made_from_a_post_is_posted_back_as_one_reply': 30}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


# ------------------------------------------------------------------------------------------------ the node
class Ctx:
    def __init__(self, n):
        from app.routers import client
        from app.services import meme_builder_service, search_service, stats_service
        self.mp = mp = pytest.MonkeyPatch()
        self.renders = []           # (pubkey-less) every edit list the renderer was handed, with its output type
        real = meme_builder_service.render

        def recording(edit, sources, out_file=None):
            out = real(edit, sources, out_file) if out_file else real(edit, sources)
            self.renders.append((edit, out[1]))
            return out
        mp.setattr(meme_builder_service, 'render', recording)

        async def local(*a, **kw):
            return None             # never hand a job to the peers this machine's own settings name
        mp.setattr(client, '_meme_lb_forward', local)
        mp.setattr(stats_service, 'bump', lambda *a, **kw: None)
        # The instance's Blossom is this node's own media host (as blossom_public_url makes it on a node).
        mp.setattr(search_service, 'own_media_hosts', lambda: {'127.0.0.1'})
        self.users = F.members(FIRST_FILL, SCENARIOS)
        self.api = F.Routers(mp, [(client.router, '')], self.users, paths=['/client/meme/'])
        # Blossom uploads: a per-device fault (the device's pcdev cookie), at the instance's real PUT.
        self.upload_fail = set()
        ctx = self
        orig_put = R.Instance.do_PUT

        def do_put(h):
            if h.path.split('?')[0] == '/blossom/upload':
                tag = ''
                for part in (h.headers.get('Cookie') or '').split(';'):
                    k, _, v = part.strip().partition('=')
                    if k == 'pcdev':
                        tag = v
                if tag in ctx.upload_fail:
                    h._body()
                    return h._json({'detail': 'Temporarily unavailable; try again shortly'}, 503)
            return orig_put(h)
        mp.setattr(R.Instance, 'do_PUT', do_put)

    def stop(self):
        self.api.stop()
        self.mp.undo()


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


# ------------------------------------------------------------------------------------------------ helpers
STATUS = "((document.getElementById('mb-status')||{}).textContent||'')"
RESULT = "((document.getElementById('mb-result')||{}).innerText||'')"
LAYERS = "JSON.parse(localStorage.getItem('pc_meme_project')||'{}').layers"


async def open_builder(d, fresh=True):
    await d.b.js("__PC.switchView('meme');true")
    await F.wait(d, "!!document.getElementById('mb-stage') && !!window.PCMeme && !!document.querySelector('.mb-wrap')",
                 30, 'the Meme Builder')
    if fresh:
        await d.b.js("PCMeme.reset();true")
    await still(d)


async def still(d):
    """Format -> Still image: one frame, a PNG (the quickest real render)."""
    await F.wait(d, "!!document.getElementById('mb-fmt')", 15, 'the format picker')
    await d.b.js("(()=>{const s=document.getElementById('mb-fmt');s.value='png';"
                 "s.dispatchEvent(new Event('change',{bubbles:true}));return true})()")
    await F.wait(d, "document.getElementById('mb-fmt').value==='png'", 10, 'Still selected')


DROP = r'''(async rgb=>{
  const c=document.createElement('canvas');c.width=320;c.height=240;const g=c.getContext('2d');
  g.fillStyle='rgb('+rgb.join(',')+')';g.fillRect(0,0,320,240);
  const blob=await new Promise(r=>c.toBlob(r,'image/png'));
  const dt=new DataTransfer();dt.items.add(new File([blob],'holiday.png',{type:'image/png'}));
  document.querySelector('.mb-wrap').dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:dt}));
  return true;})(%s)'''


async def drop_photo(d, rgb=(200, 40, 40)):
    await d.b.js(DROP % json.dumps(list(rgb)))


async def n_layers(d):
    return await d.b.js("(window.PCMeme&&document.querySelectorAll('#mb-stage .mb-item').length)||0")


async def add_caption(d, text):
    await d.b.js("document.getElementById('mb-add-text').click();true")
    await F.wait(d, "!!document.getElementById('mb-f-text')", 15, 'the caption inspector')
    await d.b.js("(t=>{const e=document.getElementById('mb-f-text');e.value=t;"
                 "e.dispatchEvent(new Event('input',{bubbles:true}));return true})(%s)" % json.dumps(text))


async def press_render(d):
    await F.wait(d, "!!document.getElementById('mb-render')", 10, 'the Render button')
    await d.b.js("document.getElementById('mb-render').click();true")


async def rendered(d):
    await F.wait(d, "!!document.querySelector('#mb-result img.mb-resvid') && !!document.getElementById('mb-post')",
                 45, 'the finished meme')


def blob_of(url):
    sha = url.rsplit('/', 1)[-1].split('.')[0]
    return H.STATE['blobs'].get(sha)


def kind1(n, pk):
    return n.home.query({'kinds': [1], 'authors': [pk]})


# ------------------------------------------------------------------------------------------------ scenarios
@scenario
async def a_meme_is_posted_with_its_picture_and_a_fresh_device_sees_it(S):
    n, ctx = S.n, S.ctx
    d = await S.device(0)
    await open_builder(d)
    await drop_photo(d)
    await F.wait(d, "%s.length===1 && /\\/blossom\\/[0-9a-f]{64}/.test(%s[0].src)" % (LAYERS, LAYERS), 30,
                 'the dropped photo uploaded and on the stage')
    photo = (await d.b.js(LAYERS))[0]['src']
    assert blob_of(photo), 'the dropped photo is not on Blossom'
    caption = 'when the build is green %d' % S.base
    await add_caption(d, caption)
    before = len(ctx.renders)
    await press_render(d)
    await rendered(d)
    edit, ctype = ctx.renders[before]
    assert ctype == 'image/png', ctype
    kinds = sorted(l['type'] for l in edit['layers'])
    assert kinds == ['image', 'text'] and any(caption == ' '.join((l.get('text') or '').split()) for l in edit['layers']), edit['layers']
    await d.b.js("document.getElementById('mb-post').click();true")
    await F.wait(d, "!!document.querySelector('#cmp') && /\\/blossom\\/[0-9a-f]{64}\\.png/.test(document.querySelector('#cmp').value)",
                 30, 'the composer opened with the uploaded meme')
    url = await d.b.js("document.querySelector('#cmp').value.match(/https?:\\/\\/\\S+\\.png/)[0]")
    data = blob_of(url)
    assert data and data[:8] == b'\x89PNG\r\n\x1a\n', 'the meme on Blossom is not the rendered PNG'
    assert url.split('/')[-1].split('.')[0] != photo.split('/')[-1].split('.')[0], 'the PHOTO was posted, not the meme'
    await d.b.js("document.querySelector('#cmp-send').click();true")
    got = await F.until(lambda: [e for e in kind1(n, S.pk(0)) if url in e['content']], 30, 'the meme note on the relay', d)
    assert len(kind1(n, S.pk(0))) == 1, 'more than one note was published'
    # Another account, on a device that has never seen any of it.
    b = await S.device(1)
    await F.open_note(b, got[0]['id'])
    sel = '.note[data-id="%s"] img' % got[0]['id']
    await F.wait(b, "[...document.querySelectorAll(%s)].some(i=>i.src.includes(%s) && i.complete && i.naturalWidth>0)"
                 % (json.dumps(sel), json.dumps(url.split('/')[-1].split('.')[0])), 30, 'the meme picture on the fresh device')


@CHROME
def test_a_meme_is_posted_with_its_picture_and_a_fresh_device_sees_it(ran):
    ran('a_meme_is_posted_with_its_picture_and_a_fresh_device_sees_it')


@scenario
async def a_meme_made_from_a_post_is_posted_back_as_one_reply(S):
    n = S.n
    # Somebody's post with a picture in it, from their own client.
    a = await S.device(1)
    await open_builder(a)
    await drop_photo(a, (30, 90, 220))
    await F.wait(a, "%s.length===1" % LAYERS, 30, "the author's photo uploaded")
    src = (await a.b.js(LAYERS))[0]['src']
    if not src.endswith('.png'):
        src += '.png'
    post = await F.post_note(a, 'my holiday %d %s' % (S.base, src))
    d = await S.device(0)
    await F.open_note(d, post['id'])
    card = F.CARD % post['id']
    await d.b.js("%s.querySelector('.act[data-a=menu]').click();true" % card)
    await F.wait(d, "!!document.querySelector('[data-m=memebuild]')", 15, 'the ⋯ menu')
    await d.b.js("document.querySelector('[data-m=memebuild]').click();true")
    await F.wait(d, "!!window.PCMeme && %s && %s.length===1" % (LAYERS, LAYERS), 30, "the post's picture in the builder")
    await still(d)
    await add_caption(d, 'nice one %d' % S.base)
    await press_render(d)
    await rendered(d)
    await F.wait(d, "!!document.getElementById('mb-reply')", 10, 'the Reply button')
    # Two quick taps: ONE reply. Counted at the client's publish too -- the relay alone would hide a second sign
    # landing in the same second (an identical event, one id).
    await d.b.js("(()=>{window.__replies=0;const P=__PC,real=P.publish;"
                 "try{P.publish=function(k){if(k===1)window.__replies++;return real.apply(this,arguments)};}catch(_){}return true})()")
    await d.b.js("document.getElementById('mb-reply').click();document.getElementById('mb-reply').click();true")
    await F.wait(d, "/replied/.test(document.getElementById('mb-reply').textContent)", 30, 'the reply posted')
    replies = await F.until(lambda: [e for e in kind1(n, S.pk(0))], 20, 'the reply on the relay', d)
    assert await d.b.js("window.__replies") == 1, ('the reply was published %s times' % await d.b.js("window.__replies"))
    assert len(kind1(n, S.pk(0))) == 1, ('two replies', replies)
    r = replies[0]
    assert any(t[0] == 'e' and t[1] == post['id'] for t in r['tags']), r['tags']
    assert any(t[0] == 'p' and t[1] == S.pk(1) for t in r['tags']), 'the author is not tagged -- never notified'
    url = r['content'].strip()
    assert (blob_of(url) or b'')[:4] == b'\x89PNG', 'the reply does not carry the rendered meme'


@CHROME
def test_a_meme_made_from_a_post_is_posted_back_as_one_reply(ran):
    ran('a_meme_made_from_a_post_is_posted_back_as_one_reply')


@scenario
async def a_render_the_node_refuses_or_drops_says_so_and_nothing_is_posted(S):
    n, api, tag = S.n, S.ctx.api, 'mr%d' % S.base
    d = await S.device(0, tag=tag)
    await open_builder(d)
    await drop_photo(d)
    await F.wait(d, "%s.length===1" % LAYERS, 30, 'the photo on the stage')
    api.fail('/client/meme/render', 503, tag)
    await press_render(d)
    await F.wait(d, "!!document.querySelector('#mb-result .mb-err')", 30, 'the refused render said')
    err = await d.b.js(RESULT)
    assert 'Temporarily unavailable' in err and 'object Object' not in err and '{' not in err, err
    assert not await d.b.js("!!document.getElementById('mb-post')"), 'a Post button for a meme that never rendered'
    api.fail('/client/meme/render', 'drop', tag)
    await press_render(d)
    await F.wait(d, "!!document.querySelector('#mb-result .mb-err') && /Render|Still/.test(document.getElementById('mb-render').textContent)",
                 30, 'the dropped render said')
    err = await d.b.js(RESULT)
    assert 'Failed to fetch' not in err and 'reach' in err, err
    # The button is a Render button again, not stuck on Cancel, and the next try (node back) works.
    api.heal('/client/meme/render', tag)
    await press_render(d)
    await rendered(d)
    assert kind1(n, S.pk(0)) == [], 'something was published'


@CHROME
def test_a_render_the_node_refuses_or_drops_says_so_and_nothing_is_posted(ran):
    ran('a_render_the_node_refuses_or_drops_says_so_and_nothing_is_posted')


@scenario
async def a_slow_render_shows_progress_can_be_cancelled_and_then_finishes(S):
    api, tag = S.ctx.api, 'ms%d' % S.base
    d = await S.device(0, tag=tag)
    await open_builder(d)
    await drop_photo(d)
    await F.wait(d, "%s.length===1" % LAYERS, 30, 'the photo on the stage')
    api.fail('/client/meme/render', 4, tag)
    await press_render(d)
    await F.wait(d, "/rendering… [1-9]/.test(%s) && /Cancel/.test(document.getElementById('mb-render').textContent)" % STATUS,
                 10, 'the render clock and a Cancel button')
    await d.b.js("document.getElementById('mb-render').click();true")       # Cancel
    await F.wait(d, "%s.some(t=>/render cancelled/.test(t))" % "(window.__toastLog||[])", 10, 'the cancel said')
    assert await d.b.js(STATUS) == '' and not await d.b.js("!!document.getElementById('mb-post')")
    await press_render(d)                                                    # slow again, and waited for this time
    await F.wait(d, "/rendering…/.test(%s)" % STATUS, 10, 'rendering again')
    await rendered(d)
    api.heal('/client/meme/render', tag)
    assert await d.b.js(STATUS) == '', 'the clock kept running after the meme came back'


@CHROME
def test_a_slow_render_shows_progress_can_be_cancelled_and_then_finishes(ran):
    ran('a_slow_render_shows_progress_can_be_cancelled_and_then_finishes')


@scenario
async def a_meme_whose_upload_fails_is_not_posted_and_posts_once_on_retry(S):
    n, ctx, tag = S.n, S.ctx, 'mu%d' % S.base
    d = await S.device(0, tag=tag)
    await open_builder(d)
    await drop_photo(d)
    await F.wait(d, "%s.length===1" % LAYERS, 30, 'the photo on the stage')
    await press_render(d)
    await rendered(d)
    ctx.upload_fail.add(tag)
    await d.b.js("document.getElementById('mb-post').click();true")
    await F.wait(d, "/upload failed/.test((document.getElementById('mb-reslink')||{}).textContent||'')", 30,
                 'the failed upload said')
    msg = await d.b.js("document.getElementById('mb-reslink').textContent")
    assert '{' not in msg and 'Temporarily unavailable' in msg, msg
    assert not await d.b.js("!!document.querySelector('#cmp')"), 'the composer opened with no meme in it'
    ctx.upload_fail.discard(tag)
    await d.b.js("document.getElementById('mb-post').click();true")
    await F.wait(d, "!!document.querySelector('#cmp') && /\\/blossom\\/[0-9a-f]{64}\\.png/.test(document.querySelector('#cmp').value)",
                 30, 'the composer with the meme, on retry')
    await d.b.js("document.querySelector('#cmp-send').click();true")
    await F.until(lambda: kind1(n, S.pk(0)), 30, 'the post', d)
    assert len(kind1(n, S.pk(0))) == 1


@CHROME
def test_a_meme_whose_upload_fails_is_not_posted_and_posts_once_on_retry(ran):
    ran('a_meme_whose_upload_fails_is_not_posted_and_posts_once_on_retry')


@scenario
async def a_photo_whose_upload_fails_adds_no_broken_layer(S):
    ctx, tag = S.ctx, 'mp%d' % S.base
    d = await S.device(0, tag=tag)
    await open_builder(d)
    ctx.upload_fail.add(tag)
    await drop_photo(d)
    await F.wait(d, "/upload failed/.test(%s)" % STATUS, 30, 'the failed upload said')
    msg = await d.b.js(STATUS)
    assert '{' not in msg and 'Temporarily unavailable' in msg, msg
    assert not (await d.b.js(LAYERS) or []), 'a layer was added for a photo that never uploaded'
    # Render with nothing on it says so instead of sending an empty edit to the node.
    seen = len(ctx.api.seen)
    await press_render(d)
    await F.wait(d, "(window.__toastLog||[]).concat([%s]).some(t=>/add (a|at least one) layer/.test(t))" % RESULT, 10,
                 'nothing to render said')
    assert not [s for s in ctx.api.seen[seen:] if s[1] == '/client/meme/render'], 'an empty meme was sent to render'
    ctx.upload_fail.discard(tag)
    await drop_photo(d)
    await F.wait(d, "%s.length===1" % LAYERS, 30, 'the photo, once the upload works')


@CHROME
def test_a_photo_whose_upload_fails_adds_no_broken_layer(ran):
    ran('a_photo_whose_upload_fails_adds_no_broken_layer')


# ------------------------------------------------------------------------------------------------ two accounts
async def add_account(d, fill):
    """⋯ Accounts -> Add an account (the page reloads to the login screen) -> sign in with another nsec."""
    await d.b.js("window.__beforeAdd=1;__PC.accountAct('add');true")
    await F.wait(d, "!window.__beforeAdd && !!window.__PC && document.body.classList.contains('guest')", 60, 'the login screen')
    await d.b.js("(()=>{document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(new Uint8Array(32).fill(%d));"
                 "document.querySelector('#btn-nsec-login').click();return true})()" % fill)
    await F.wait(d, "!!__PC.me() && __PC.me().pubkey===%s && Relay.status==='ok'" % json.dumps(R.pubkey(fill)), 60,
                 'signed in as the added account')


async def switch_to(d, pk):
    await d.b.js("window.__beforeSwitch=1;__PC.accountAct(%s);true" % json.dumps(pk))
    await F.wait(d, "!window.__beforeSwitch && !!window.__PC && !!__PC.me() && __PC.me().pubkey===%s" % json.dumps(pk),
                 60, 'switched account')


STAGE_TEXT = "((document.getElementById('mb-stage')||{}).innerText||'')"


@scenario
async def another_account_on_this_device_never_gets_the_meme_in_progress(S):
    n = S.n
    d = await S.device(0)
    secret = 'only account A may see this %d' % S.base
    await open_builder(d)
    await drop_photo(d)
    await F.wait(d, "%s.length===1" % LAYERS, 30, 'the photo on the stage')
    await add_caption(d, secret)
    await F.wait(d, "%s.includes(%s)" % (STAGE_TEXT, json.dumps(secret.split()[0])), 10, "A's caption on the stage")
    await add_account(d, S.fill(2))
    await open_builder(d, fresh=False)
    stage = ' '.join((await d.b.js(STAGE_TEXT)).split())
    layers = await d.b.js("(window.PCMeme&&document.querySelectorAll('#mb-stage .mb-item').length)||0")
    assert secret not in stage and layers == 0, ("account B opened account A's meme", layers, stage[:200])
    # And A's work is still A's: switching back finds it where it was left.
    await switch_to(d, S.pk(0))
    await open_builder(d, fresh=False)
    await F.wait(d, "document.querySelectorAll('#mb-stage .mb-item').length===2", 20, "A's meme back under A")
    assert kind1(n, S.pk(2)) == [], 'account B published something'


@CHROME
def test_another_account_on_this_device_never_gets_the_meme_in_progress(ran):
    ran('another_account_on_this_device_never_gets_the_meme_in_progress')
