"""EVERY PICTURE IN FILES SHOWS AS A PICTURE -- on any Blossom server, encrypted or not.

Josephus, 2026-10-13: "files with no thumbnails, no idea what picture is what". Two causes, both measured:
  * a tile asked only `<server>/thumb/<sha>`, a route only THIS app's Blossom server has. His server
    (blossom.ditto.pub) answers that 404 while serving the blob itself, so every image tile fell back to a
    generic file icon;
  * an ENCRYPTED drive picture had no thumbnail at all -- always a lock -- because the server cannot preview
    bytes it cannot read.

Driven through the real client against the wallpaper harness (real relay + a real instance whose Blossom
serves `/blossom/<sha>` and, like ditto.pub, has NO `/thumb/` route). What a person SEES is asserted:
  (a) a plain picture's tile holds a decoded picture, not the file icon;
  (b) an encrypted picture's tile carries its decrypted picture as the tile background (`has-thumb`);
  (c) a picture that cannot be loaded anywhere becomes the file icon -- never the broken-image glyph.
  Plus: the decrypted picture is fitted to the tile (the enc tile's `background:none` shorthand reset
  background-size), and a redraw reuses the decrypted copy rather than downloading it again.

Fail-first: PC_WALLPAPER_OVERRIDE="/static/js/client/files.js=<old>,/static/js/client/upload.js=<old>"
serves the pre-fix copies; (a) and (b) then fail. An upload.js whose fallback never swaps fails (c).
"""
from pathlib import Path

import httpx
import pytest

from tests.client import wallpaper_harness as H
from tests.client.test_desktop_wallpaper_full_app import run

MISSING = 'cd' * 32          # listed by the server, held by nobody: neither a preview nor the blob loads

TILE = r"""(sha=>{const c=document.querySelector('#bl-grid .file-card[data-sha="'+sha+'"]');if(!c)return null;
  const im=c.querySelector('img'), ic=c.querySelector('.file-icon');
  return {img:!!im, src:im?im.getAttribute('src'):'', loaded:!!(im&&im.complete&&im.naturalWidth>0),
          broken:!!(im&&im.complete&&im.naturalWidth===0), icon:!!ic,
          thumb:!!(ic&&ic.classList.contains('has-thumb')), bg:ic?(ic.style.backgroundImage||''):'',
          bgSize:ic?getComputedStyle(ic).backgroundSize:'',
          bgRepeat:ic?getComputedStyle(ic).backgroundRepeat:''};})"""


async def _tile(b, sha):
    return await b.js('(%s)(%r)' % (TILE, sha))


@pytest.mark.skipif(not Path(H.CHROME).exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1440, 390], ids=['desktop', 'phone'])
def test_pictures_show_in_files_on_a_server_without_previews(width):
    async def main():
        a = H.Device(width=width)
        b = await a.__aenter__()
        try:
            plain = await H.put_picture(b, 'red.png', folder='Pics', encrypted=False, b64=H.PNG_RED)
            secret = await H.put_picture(b, 'blue.png', folder='Pics', encrypted=True, b64=H.PNG_BLUE)
            pk = await b.js('__PC.me().pubkey')
            await b.js("(async()=>{__PC.filesIdx().setFile(%r,{name:'gone.png',folder:'Pics',mime:'image/png',"
                       "size:68,ts:1760000000});await __PC.filesIdx()._save();return true})()" % MISSING)
            # The server lists all three; like blossom.ditto.pub it has no preview route at all.
            port = H.HTTP.server_address[1]
            origin = f'http://127.0.0.1:{port}'
            async with httpx.AsyncClient(trust_env=False) as h:
                assert (await h.get(f'{origin}/blossom/thumb/{plain}')).status_code == 404
                assert (await h.get(f'{origin}/blossom/{plain}')).status_code == 200
            H.STATE['api'][f'/blossom/list/{pk}'] = [
                {'sha256': plain, 'size': len(H.STATE['blobs'][plain]), 'type': 'image/png',
                 'url': f'{origin}/blossom/{plain}', 'uploaded': 1760000001},
                {'sha256': secret, 'size': len(H.STATE['blobs'][secret]), 'type': 'application/octet-stream',
                 'url': f'{origin}/blossom/{secret}', 'uploaded': 1760000002},
                {'sha256': MISSING, 'size': 68, 'type': 'image/png',
                 'url': f'{origin}/blossom/{MISSING}', 'uploaded': 1760000003},
            ]
            await b.js("ClientSettings.set('filesView','tiles'); __PC.switchView('blossom')")   # thumbnails are a TILES thing
            vis = ("[...document.querySelectorAll('[data-folder=\"Pics\"]')]"
                   ".find(e=>e.getClientRects().length&&e.offsetParent)")
            await H.wait(b, '!!' + vis)
            await b.js(vis + '.click()')
            await H.wait(b, "!!document.querySelector('#bl-grid .file-card[data-sha=\"%s\"]')" % MISSING,
                         state="document.querySelector('#bl-grid')&&document.querySelector('#bl-grid').innerText")

            async def settled(sha, test, what):
                try:
                    await H.wait(b, '(()=>{const t=(%s)(%r);return !!t&&(%s)})()' % (TILE, sha, test), seconds=20,
                                 state='(%s)(%r)' % (TILE, sha))
                except AssertionError as e:
                    raise AssertionError(f'{what}: {e}') from None

            # (a) the plain picture: the 404'd preview is replaced by the original, which decodes.
            await settled(plain, 't.img && t.loaded', 'a picture on a server with no /thumb/ route is not shown')
            t = await _tile(b, plain)
            assert t['src'].endswith('/blossom/' + plain), t

            # (b) the encrypted picture: decrypted HERE and drawn as the tile, the lock hidden.
            await settled(secret, "t.thumb && t.bg.startsWith('url(\"blob:')",
                          'an encrypted drive picture still shows only a lock')
            t = await _tile(b, secret)
            assert t['bgSize'] == 'cover' and t['bgRepeat'] == 'no-repeat', ('the picture is not fitted to its tile', t)
            # and the bytes behind it really are the picture (decrypted, typed as an image)
            ok = await b.js("""(async()=>{const c=document.querySelector('#bl-grid .file-card[data-sha="%s"] .file-icon');
              const u=/url\\("?([^")]+)"?\\)/.exec(c.style.backgroundImage)[1];
              const im=new Image();im.src=u;await im.decode();return im.naturalWidth>0})()""" % secret)
            assert ok

            # Leaving and coming back redraws the grid; the picture is drawn again from the decrypted copy
            # already held, not downloaded and decrypted a second time (and its first object URL leaked).
            fetched = ("performance.getEntriesByType('resource')"
                       ".filter(e=>e.name.includes('/blossom/%s')).length" % secret)
            before = await b.js(fetched)
            await b.js("__PC.switchView('notifications')")
            await b.js("__PC.switchView('blossom')")
            card = "!!document.querySelector('#bl-grid .file-card[data-sha=\"%s\"]')" % secret
            await H.wait(b, card + ' || !!' + vis)
            if not await b.js(card):           # a phone reopens the folder it was in; a desktop its home
                await b.js(vis + '.click()')
            await settled(secret, "t.thumb && t.bg.startsWith('url(\"blob:')", 'the picture was lost on a redraw')
            assert await b.js(fetched) == before, 'a redraw downloaded the encrypted picture again'

            # (c) nothing anywhere: the file icon, never a broken-image glyph.
            await settled(MISSING, 't.icon && !t.img', 'an unloadable picture did not fall back to the icon')
            assert not await b.js('__errors'), await b.js('__errors')
        finally:
            await a.__aexit__()
    run(main())
