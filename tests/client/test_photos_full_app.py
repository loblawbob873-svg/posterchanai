"""PHOTOS: the Nostr photo app, paired with the profile's albums -- against this node's REAL relay.

Asked 2026-10-28 ("Nostr Photos"; "should probably pair with that photo album NIP we added to user profiles").
The shipped client signed in with an nsec, the shipped RelayServer over PosterChanDB (realnode_harness), real
clicks. What a person SEES and what lands on the relay:

  * Photos opens from the sidebar; Following shows the pictures of people you follow (and not a stranger's),
    Global shows everybody's -- and every tile's picture actually LOADS (naturalWidth), not just a box;
  * a friend's picture can be saved into a NEW album from its viewer: the relay then holds a NIP-51 kind-30006
    set titled as typed, naming that post -- the same album the profile's Albums tab shows;
  * the Albums tab here is that album, with its cover drawn from the saved picture;
  * "Post a photo" with a caption and an album publishes ONE NIP-68 kind-20 post (imeta url + m + the caption's
    hashtag as a `t` tag) and adds it to the album -- so the set names both pictures;
  * on a phone the grid is three columns with nothing off the side of the screen.
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
ME, FRIEND, STRANGER = 1, 2, 3

TILES = "[...document.querySelectorAll('.ph-tile')].map(t=>t.dataset.id)"
LOADED = "[...document.querySelectorAll('.ph-tile img')].every(i=>i.complete&&i.naturalWidth>0)"


async def click(b, sel):
    await R.wait(b, "!!document.querySelector(%s)" % json.dumps(sel), 20, sel)
    x, y = await b.js("(()=>{const e=document.querySelector(%s);e.scrollIntoView({block:'center'});"
                      "const r=e.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()" % json.dumps(sel))
    for kind in ('mousePressed', 'mouseReleased'):
        await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})


def scene(n):
    me, friend, stranger = R.pubkey(ME), R.pubkey(FRIEND), R.pubkey(STRANGER)
    red = n.foreign.put(R.png((220, 30, 60), 32), owned=False)
    blue = n.foreign.put(R.png((30, 60, 220), 32), owned=False)
    pic = lambda sk, sha, text: R.sign(sk, 20, text, [['imeta', 'url %s/%s' % (n.foreign.url, sha), 'm image/png'],
                                                      ['m', 'image/png']])
    fpost, spost = pic(FRIEND, red, 'the harbour at dawn'), pic(STRANGER, blue, 'my cat')
    n.home.add([R.sign(ME, 3, '', [['p', friend]]), R.sign(FRIEND, 0, '{"name":"Friendly"}'),
                R.sign(STRANGER, 0, '{"name":"Stranger"}'), fpost, spost])
    return me, fpost, spost


async def open_photos(d):
    b = d.b
    if d.phone:
        await b.js("__PC.switchView('photos');true")
    else:
        await click(b, ".nav-item[data-view=photos]")
    await R.wait(b, "!!document.querySelector('.ph-wrap')", 30, 'the Photos screen')


@CHROME
def test_following_global_save_to_a_new_album_and_post_into_it():
    got = {}

    async def main():
        with R.Node(members=[R.pubkey(ME), R.pubkey(FRIEND), R.pubkey(STRANGER)]) as n:
            me, fpost, spost = scene(n)
            async with R.Device(n, ME) as d:
                b = d.b
                await open_photos(d)
                await R.wait(b, "%s.includes(%s)" % (TILES, json.dumps(fpost['id'])), 40, "the friend's photo on Following")
                got['following'] = await b.js(TILES)
                await R.wait(b, LOADED, 20, 'the pictures load')
                await click(b, ".ph-tab[data-tab=global]")
                await R.wait(b, "%s.includes(%s)" % (TILES, json.dumps(spost['id'])), 30, "the stranger's photo on Global")
                got['global'] = await b.js(TILES)
                await R.wait(b, LOADED, 20, 'the Global pictures load')
                if os.environ.get('PC_SHOTS'):
                    import base64
                    Path(os.environ['PC_SHOTS']).mkdir(parents=True, exist_ok=True)
                    Path(os.environ['PC_SHOTS'], 'photos-1440.png').write_bytes(
                        base64.b64decode((await b.call('Page.captureScreenshot', {'format': 'png'}))['data']))

                # Save the friend's picture into a new album, from its viewer.
                await click(b, ".ph-tile[data-id='%s']" % fpost['id'])
                await R.wait(b, "!!document.querySelector('.ph-view .ph-big')", 15, 'the photo viewer')
                got['caption'] = await b.js("(document.querySelector('.ph-caption')||{}).textContent||''")
                got['author'] = await b.js("document.querySelector('.ph-author').textContent")
                await click(b, ".ph-save")
                await click(b, ".ph-alb-new")
                await R.wait(b, "!!document.querySelector('.alb-f-title')", 15, 'the new-album form')
                await b.js("document.querySelector('.alb-f-title').focus();true")
                await b.call('Input.insertText', {'text': 'Harbour trip'})
                await click(b, ".alb-form [type=submit]")
                await R.wait(b, "(window.__toastLog||[]).some(t=>/saved to “Harbour trip”/.test(t))", 30, 'saved to the album')
                sets = n.home.query({'kinds': [30006], 'authors': [me]})
                got['set1'] = max(sets, key=lambda e: e['created_at']) if sets else None

                # The Albums tab is that album, cover drawn from the saved picture.
                await click(b, ".ph-tab[data-tab=albums]")
                await R.wait(b, "[...document.querySelectorAll('.alb-card .alb-title')].some(t=>t.textContent==='Harbour trip')", 30, 'the album card')
                await R.wait(b, "[...document.querySelectorAll('.alb-card .alb-cover img')].some(i=>i.complete&&i.naturalWidth>0)", 20, 'the album cover loads')
                got['count'] = await b.js("document.querySelector('.alb-card .alb-count').textContent")

                # Post a photo with a caption, straight into the album.
                tmp = tempfile.mkdtemp(prefix='pc-photos-')
                f = Path(tmp, 'sunset.png')
                f.write_bytes(R.png((250, 160, 20), 48))
                doc = await b.call('DOM.getDocument', {'depth': -1})
                node = await b.call('DOM.querySelector', {'nodeId': doc['root']['nodeId'], 'selector': '.ph-file'})
                await b.call('DOM.setFileInputFiles', {'nodeId': node['nodeId'], 'files': [str(f)]})
                await R.wait(b, "!!document.querySelector('.ph-compose .ph-cap')", 15, 'the post form')
                await R.wait(b, "[...document.querySelectorAll('.ph-to option')].some(o=>o.textContent==='Harbour trip')", 20, 'the album choice')
                await b.js("(()=>{const s=document.querySelector('.ph-to');s.value=[...s.options].find(o=>o.textContent==='Harbour trip').value;return true})()")
                await b.js("document.querySelector('.ph-cap').focus();true")
                await b.call('Input.insertText', {'text': 'sunset over the water #Harbour'})
                await click(b, ".ph-go")
                await R.wait(b, "(window.__toastLog||[]).some(t=>/posted and added to “Harbour trip”/.test(t))", 40,
                             'posted into the album')
                mine = n.home.query({'kinds': [20], 'authors': [me]})
                got['post'] = mine[0] if mine else None
                sets = n.home.query({'kinds': [30006], 'authors': [me]})
                got['set2'] = max(sets, key=lambda e: e['created_at']) if sets else None

    asyncio.run(main())
    fid = [t[1] for t in got['set1']['tags'] if t[0] == 'e'] if got['set1'] else []
    assert len(got['following']) == 1, ("Following showed someone you do not follow", got['following'])
    assert len(got['global']) == 2, got['global']
    assert got['caption'] == 'the harbour at dawn' and 'Friendly' in got['author'], got
    assert got['set1'], 'no album reached the relay'
    assert ['title', 'Harbour trip'] in got['set1']['tags'] and fid == got['following'], got['set1']
    assert got['count'].startswith('1 photo'), got['count']
    p = got['post']
    assert p and p['content'] == 'sunset over the water #Harbour', p
    im = [t for t in p['tags'] if t[0] == 'imeta']
    assert len(im) == 1 and any(x.startswith('url http') for x in im[0]) and 'm image/png' in im[0], im
    assert ['t', 'harbour'] in p['tags'] and ['m', 'image/png'] in p['tags'], p['tags']
    ids = [t[1] for t in got['set2']['tags'] if t[0] == 'e']
    assert ids == fid + [p['id']], ("the new post was not added to the album after the saved one", ids)


@CHROME
def test_on_a_phone_the_grid_is_three_columns_and_fits():
    got = {}

    async def main():
        with R.Node(members=[R.pubkey(ME), R.pubkey(FRIEND), R.pubkey(STRANGER)]) as n:
            scene(n)
            async with R.Device(n, ME, phone=True) as d:
                b = d.b
                await open_photos(d)
                await click(b, ".ph-tab[data-tab=global]")
                await R.wait(b, "document.querySelectorAll('.ph-tile').length===2", 30, 'two photos on Global')
                await R.wait(b, LOADED, 20, 'the pictures load')
                got['fit'] = await b.js("""(()=>{const g=document.querySelector('.ph-grid');
                  const cols=getComputedStyle(g).gridTemplateColumns.split(' ').length;
                  const tiles=[...document.querySelectorAll('.ph-tile')].map(t=>t.getBoundingClientRect());
                  const tabs=[...document.querySelectorAll('.ph-tab')].map(t=>t.getBoundingClientRect());
                  return {cols, wide: document.scrollingElement.scrollWidth<=innerWidth+1,
                          inside: tiles.every(r=>r.left>=-1&&r.right<=innerWidth+1),
                          tabTap: Math.min(...tabs.map(r=>r.height)), tabsIn: tabs.every(r=>r.right<=innerWidth+1)}})()""")
                if os.environ.get('PC_SHOTS'):
                    import base64
                    Path(os.environ['PC_SHOTS']).mkdir(parents=True, exist_ok=True)
                    Path(os.environ['PC_SHOTS'], 'photos-390.png').write_bytes(
                        base64.b64decode((await b.call('Page.captureScreenshot', {'format': 'png'}))['data']))

    asyncio.run(main())
    f = got['fit']
    assert f['cols'] == 3, f
    assert f['wide'] and f['inside'] and f['tabsIn'], ("something runs off a phone screen", f)
    assert f['tabTap'] >= 34, ("tabs too small to tap", f)
