"""PBS's setup follows the ACCOUNT -- and a read that failed never overwrites it.

Run: venv-unified/bin/python -m pytest tests/client/test_pbs_persist_full_app.py

Three browsers, three fresh profiles, one key:

  1. build a studio (a text overlay dragged to the lower right, an image, a camera, 1080p, the High preset, a
     title) -> it is saved as ONE kind-30078 `d=pcai:pbs`, NIP-44-encrypted (no layer text readable in the event),
     the image referenced (`idb:` here, the upload having been refused), never inlined
  2. a FRESH profile whose relay holds that event opens PBS -> the same layers in the same order, the text drawn
     where it was left, 1080p and High picked, the title back; the camera listed as "not capturing" (a capture
     belongs to a session, never to a document)
  3. a FRESH profile whose relay NEVER ANSWERS a 30078 read opens PBS -> it says it couldn't read the saved studio,
     and building something there publishes NOTHING: the account's setup is not replaced by this device's guess
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import pbs_fixture as F
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


DOCS = "__published.filter(e=>e.kind===30078&&e.tags.some(t=>t[0]==='d'&&t[1]==='pcai:pbs'))"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_studio_is_saved_to_the_account_and_a_failed_read_never_replaces_it(tmp_path):
    one, two, three = {}, {}, {}

    async def build(b):
        await F.open_pbs(b, 1280)
        await b.until("/account|New studio/.test(document.querySelector('#pbs-saved').textContent)")
        await F.click(b, '#pbs-add-text')
        await b.js("(()=>{const e=document.querySelector('#pbs-p-text');e.value='KEEP ME';e.dispatchEvent(new Event('input'));"
                   "const c=document.querySelector('#pbs-p-color');c.value='#ff0000';c.dispatchEvent(new Event('input'));return true})()")
        await asyncio.sleep(.3)
        t = await F.count(b, (255, 0, 0), None, 30)
        x0, y0 = await F.canvas_to_page(b, t['cx'], t['cy'])
        x1, y1 = await F.canvas_to_page(b, .7, .8)
        await F.drag(b, x0, y0, x1, y1)
        await asyncio.sleep(.3)
        one['text_at'] = await F.count(b, (255, 0, 0), None, 30)
        p = tmp_path / 'logo.png'
        p.write_bytes(F.png((255, 255, 0)))
        await F.set_file(b, '#pbs-img-file', p)
        await b.until("document.querySelectorAll('.pbs-layer').length===2")
        await F.click(b, '#pbs-add-camera')
        await b.until("document.querySelectorAll('.pbs-layer').length===3")
        await F.click(b, '#pbs-cap')                                  # the 720p cap off, so 1080p/High can be picked
        await F.click(b, '.pbs-seg[data-seg="res"] .pbs-chip[data-v="1080"]')
        await F.click(b, '.pbs-seg[data-seg="kbps"] .pbs-chip[data-v="3000"]')
        await b.js("(()=>{const e=document.querySelector('#pbs-title');e.value='Saved show';e.dispatchEvent(new Event('input'));return true})()")
        await asyncio.sleep(2.5)
        await b.until("document.querySelector('#pbs-saved').textContent.includes('Saved to your account')")
        one['layers'] = await b.js("[...document.querySelectorAll('.pbs-layer-n')].map(n=>n.textContent)")
        one['docs'] = await b.js(DOCS)

    asyncio.run(desktop.with_browser('online', '', build, F.init(__uploadFails=True)))
    assert one['docs'], 'nothing was saved to the account'
    ev = one['docs'][-1]
    assert 'KEEP ME' not in ev['content'] and 'Saved show' not in ev['content'], 'the studio document must be encrypted'
    assert len(ev['content']) < 20000, 'an image was inlined into the document'

    async def reopen(b):
        await F.open_pbs(b, 1280)
        await b.until("document.querySelectorAll('.pbs-layer').length===3")
        await asyncio.sleep(1)
        two['layers'] = await b.js("[...document.querySelectorAll('.pbs-layer-n')].map(n=>n.textContent)")
        two['tags'] = await b.js("[...document.querySelectorAll('.pbs-layer .pbs-tag')].map(n=>n.closest('.pbs-layer').querySelector('.pbs-layer-n').textContent)")
        two['text_at'] = await F.count(b, (255, 0, 0), None, 30)
        two['res'] = await b.js("document.querySelector('#pbs-canvas').width")
        two['segs'] = await b.js("[...document.querySelectorAll('.pbs-seg')].map(s=>s.dataset.seg+'='+s.querySelector('.pbs-chip.on').dataset.v)")
        two['title'] = await b.js("document.querySelector('#pbs-title').value")
        two['saved'] = await b.js("document.querySelector('#pbs-saved').textContent")
        two['pub'] = await b.js(DOCS + '.length')
    asyncio.run(desktop.with_browser('online', '', reopen, F.init() + 'window.__events=[%s];' % json.dumps(ev)))
    assert two['layers'] == one['layers'], (one['layers'], two['layers'])
    assert two['tags'] == ['Camera'], ('a saved camera must say it is not capturing yet', two['tags'])
    assert abs(two['text_at']['cx'] - one['text_at']['cx']) < .02 and abs(two['text_at']['cy'] - one['text_at']['cy']) < .02, \
        ('the text came back somewhere else', one['text_at'], two['text_at'])
    assert two['res'] == 1920 and 'res=1080' in two['segs'] and 'kbps=3000' in two['segs'], two
    assert two['title'] == 'Saved show' and 'account' in two['saved'], two
    assert two['pub'] == 0, 'opening the studio must not rewrite the document'

    async def silent(b):
        await F.open_pbs(b, 1280)
        await b.until("document.querySelector('#pbs-saved').textContent.includes('Couldn’t read')")
        three['saved'] = await b.js("document.querySelector('#pbs-saved').textContent")
        await F.click(b, '#pbs-add-text')
        await asyncio.sleep(3)
        three['pub'] = await b.js(DOCS + '.length')
        three['note'] = await b.js("document.querySelector('#pbs-saved').textContent")
    asyncio.run(desktop.with_browser('online', '', silent, F.init(__silent30078=True) + 'window.__events=[%s];' % json.dumps(ev)))
    assert 'nothing is written over' in three['saved'], three
    assert three['pub'] == 0, ('a studio that could not be read was written over the account copy', three)
    assert 'this device' in three['note'], three
