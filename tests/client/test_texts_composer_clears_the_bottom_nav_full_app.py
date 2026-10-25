"""Texts on a phone: the text box is never under the bottom nav ("text on android: attaching file from blossom ruins
screen, can no longer see text input, have to force close app", 2026-10-25).

On Android, Texts is the NATIVE screen; attaching from Files (Blossom) is the one path that lands on the WEB Texts
conversation (ThreadActivity -> MainActivity "texts-blossom:<address>" -> PCSms.openBlossom). That conversation is
full-height inside #feed, whose `feed-texts` rule drops the feed's bottom padding -- so at phone width the composer
sat UNDER the fixed .mobilenav (measured at 390x844: the topmost element at #sms-in's centre was the nav's compose
button, before anything was even attached). The shipped client at phone width, through the reported path: open the
conversation, attach from Files, take a file -- the text box is the topmost element where it is drawn and ends above
the nav.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_sms_image_paste_full_app import open_texts


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PROBE = r"""(()=>{const i=document.querySelector('#sms-in'),n=document.querySelector('.mobilenav');
if(!i)return {input:false};const r=i.getBoundingClientRect(),nr=n.getBoundingClientRect(),ns=getComputedStyle(n);
const pts=[0.2,0.5,0.8].map(f=>document.elementFromPoint(Math.round(r.left+r.width*f),Math.round(r.top+r.height/2))===i);
return {input:true,onTop:pts.every(Boolean),bottom:Math.round(r.bottom),navTop:ns.display==='none'?null:Math.round(nr.top),
        att:!!document.querySelector('.sms-attachment-draft')};})()"""

PNG = ("(async()=>{const c=document.createElement('canvas');c.width=1200;c.height=1600;c.getContext('2d').fillRect(0,0,1200,1600);"
       "return await new Promise(r=>c.toBlob(b=>r(URL.createObjectURL(b)),'image/png'))})()")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width,height', [(390, 844), (360, 640)])
def test_the_texts_box_is_above_the_nav_before_and_after_a_files_attachment(width, height):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': True})
        await b.call('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 5})
        await open_texts(b)
        got['before'] = await b.js(PROBE)
        url = await b.js(PNG)
        # The real picker opens; a tile tap closes it and hands over the file (upload.js blossomPicker).
        await b.js("window.__real=__PC.blossomPicker;__PC.blossomPicker=(ta,cb,o)=>{window.__cb=cb;return window.__real(ta,cb,o)}")
        await b.js("document.querySelector('#sms-attach').click()")
        await b.until("!!document.querySelector('#sms-src-blossom')")
        await b.js("document.querySelector('#sms-src-blossom').click()")
        await b.until("!!window.__cb&&!!document.querySelector('.bp-picker-bg')")
        await b.js("(()=>{document.querySelector('.bp-picker-bg').remove();const r=document.querySelector('#modal-root');"
                   "if(!r||!r.children.length)document.body.classList.remove('modal-open');})()")
        await b.js("window.__cb({url:%s,type:'image/png',ext:'png',name:'photo.png',sha:'',enc:false})" % json.dumps(url))
        await b.until("!!document.querySelector('.sms-attachment-draft')")
        await asyncio.sleep(.4)
        got['after'] = await b.js(PROBE)

    extra = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')"
             "||'{}'),osMode:false}));")
    asyncio.run(desktop.with_browser('online', '', check, extra))
    for when in ('before', 'after'):
        g = got[when]
        assert g['input'] and g['navTop'] is not None, (when, got)
        assert g['onTop'], ('the Texts box is covered (%s attaching)' % when, got)
        assert g['bottom'] <= g['navTop'] + 1, ('the Texts box runs under the bottom nav (%s attaching)' % when, got)
    assert got['after']['att'], got
