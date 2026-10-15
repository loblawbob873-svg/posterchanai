"""Social → Mini app (.xdc): the file picker lets a phone CHOOSE the game.

"social -> choose webxdc on android won't allow me to select a webxdc file to even upload". The picker carried
`accept=".xdc,application/x-webxdc,…"`; the APK's WebView hands that to Android's document picker as a MIME
filter, and Android has no type for `.xdc` -- it reports the file as application/octet-stream -- so every game
on the phone was greyed out. Driven in the shipped bundle with the picker captured: it must carry no filter,
and a valid .xdc that arrives as application/octet-stream (what Android gives) must end up in the post.
"""
import asyncio
import base64
import io
import zipfile
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def _xdc() -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('index.html', '<!doctype html><title>dice</title><p>roll')
        z.writestr('manifest.toml', 'name = "Dice"\n')
    return base64.b64encode(buf.getvalue()).decode()


PICK = r'''(async(b64)=>{
  window.__picker=null;
  const click=HTMLInputElement.prototype.click;
  HTMLInputElement.prototype.click=function(){if(this.type==='file'){window.__picker=this;return;}return click.call(this);};
  __PC.uploadBlob=async f=>{window.__uploaded={name:f.name,size:f.size};return 'https://fixture.invalid/blossom/'+'e'.repeat(64)+'.xdc';};
  const ta=document.createElement('textarea');document.body.appendChild(ta);window.__ta=ta;
  PCWebxdc.attach(ta);
  const accept=__picker&&__picker.accept;
  const bin=Uint8Array.from(atob(b64),c=>c.charCodeAt(0));
  const dt=new DataTransfer();dt.items.add(new File([bin],'dice.xdc',{type:'application/octet-stream'}));
  __picker.files=dt.files;__picker.onchange();
  return {accept};
})(%r)'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_picker_has_no_filter_and_an_octet_stream_xdc_is_posted():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!window.PCWebxdc && !!PCWebxdc.attach && !!window.PCZip")
        got['pick'] = await b.js(PICK % _xdc())
        for _ in range(100):
            if await b.js("!!(window.__ta&&__ta.value.includes('fixture.invalid'))"):
                break
            await asyncio.sleep(.1)
        got['post'] = await b.js("window.__ta?__ta.value:''")
        got['uploaded'] = await b.js("window.__uploaded||null")
        got['errors'] = await b.js('__errors')
    asyncio.run(desktop.with_browser('online', '', check))

    assert got['pick'] and got['pick'].get('accept') == '', ('the picker filters files; Android greys out every .xdc', got)
    assert got['uploaded'], ('an .xdc reported as application/octet-stream was not uploaded', got)
    assert 'fixture.invalid/blossom/' in got['post'], ('the game did not land in the post', got)
