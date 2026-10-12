"""A DRIVE FILE IS READ FROM WHEREVER IT IS, NOT ONLY FROM TODAY'S MEDIA SERVER.

cryptowolf, 2026-10-12: "couldn't use that picture -- blob HTTP 404" on every desktop wallpaper pick, before and
after he switched his media server back to the default. Measured: his picture's blob was on nostr.build (200);
poster.place and blossom.nostr.build answered 404. Uploads go to the server set at the time (his was nostr.build),
but reads asked only the CURRENT media server -- files.js `_encFileUrl` fetched mediaServer()+'/'+sha and nothing
else -- so a file uploaded under one setting was unreadable under another.

Driven through the real client against the wallpaper harness (real relay + instance + Blossom): the picture is
put in the drive, then the page sees the instance answer 404 for it and nostr.build serve it -- his exact state.
The wallpaper must apply, and the bytes must have come from nostr.build. And when NO server has it, the picker
says so ("on every server") and the layout is not touched.
"""
import asyncio
import time

import pytest

from app.services.nostr.event import build_event

from tests.client import wallpaper_harness as H
from tests.client.test_desktop_wallpaper_full_app import run, _published_bg  # noqa: F401

# The instance "loses" the blob and nostr.build has it: a fetch shim installed after the upload, so the
# upload itself took the ordinary path.
MOVE_TO_NOSTR_BUILD = r"""((sha, everywhere)=>{const real=window.fetch.bind(window);window.__nbHits=0;
  window.fetch=(input,init)=>{const u=String(input&&input.url||input);
    if(u.includes('/blossom/'+sha)) return Promise.resolve(new Response('',{status:404}));
    if(u.startsWith('https://nostr.build/'+sha)){ if(everywhere) return Promise.resolve(new Response('',{status:404}));
      window.__nbHits++; return real(location.origin+'/blossom/'+sha+'?moved=1'); }
    return real(input,init);};
  return true;})"""


@pytest.mark.parametrize("found", [True, False], ids=["on-nostr-build", "nowhere"])
def test_a_wallpaper_whose_file_lives_on_another_server_still_applies(found):
    async def main():
        a = H.Device()
        b = await a.__aenter__()
        try:
            red = await H.put_picture(b, 'red.png', encrypted=True, b64=H.PNG_RED)
            await b.js("(%s)(%r,%s)" % (MOVE_TO_NOSTR_BUILD, red, 'false' if found else 'true'))
            # The instance's own copy must be what the 404 shim hides -- prove it would otherwise be served.
            await H.enter_desktop(b)
            await H.open_picker(b)
            await H.pick(b, red)
            if found:
                await H.sees(b, H.is_red)
                await _published_bg(b, red)
                assert await b.js("window.__nbHits") >= 1, "the picture was not read from nostr.build"
            else:
                await H.wait(b, "__toasts.some(t=>/couldn.t use that picture/.test(t)&&/every server/.test(t))",
                             state="({toasts:__toasts})")
                assert not H.is_red(await H.colour_at(b))
            assert not await b.js('__errors'), await b.js('__errors')
        finally:
            await a.__aexit__()
    run(main())


# ---- what decides where uploads GO ------------------------------------------------------------------
# "that was a bug that changed it, he wants to use default": a device on Default (stored as blank) adopted
# the newest kind-10063/10096 of ANY app as its upload server. Only a list this app published may.
SK = bytes([1]) * 32


@pytest.mark.parametrize("client,adopted", [("Amethyst", False), (None, False), ("PosterChan AI", True)],
                         ids=["another-apps-list", "untagged-list", "posterchans-own-list"])
def test_default_is_not_overridden_by_another_apps_media_server_list(client, adopted):
    async def main():
        tags = [["server", "https://blossom.nostr.build/"]] + ([["client", client]] if client else [])
        ev = build_event(SK, 10063, "", tags=tags)
        ev["created_at"] = int(time.time()) - 60
        H.STATE["events"].append(ev)
        a = H.Device(sk_fill=1)
        b = await a.__aenter__()
        try:
            await asyncio.sleep(4)          # sign-in runs restoreMediaServer (app.js) on its own
            got = await b.js("ClientSettings.get('mediaServer','')")
            if adopted:
                assert got == "https://blossom.nostr.build/", got
            else:
                assert got == "", f"Default was switched to {got!r} by a list another app published"
                assert not await b.js("ClientSettings.get('blossomEnabled', false)")
        finally:
            await a.__aexit__()
    run(main())
