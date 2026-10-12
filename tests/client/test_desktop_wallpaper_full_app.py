"""Changing the desktop background in the BROWSER's desktop mode: on screen, stored, read back, synced.

Reported by a member on poster.place: "can't change desktop background in the browser in desktop
mode". Their account publishes a 26-relay list from this client ("use my own relays"). Measured
against those real relays from this node: once the pool is up, EVERY read of the account's
`pcai:desktop` comes back `complete:false` after the full 6s -- `filter.nostr.wine` answers
`auth-required` and the AUTH it provokes waits 8s for an OK that never comes, `rss.nos.social` never
answers at all -- while the node's own relay answers in milliseconds. os.js only let the layout be
written once a read was `complete`, i.e. once EVERY relay in the pool had answered, so for that pool
the layout could never be written:

  * before deploy 206 the change was REFUSED ("still loading your desktop layout -- try that again
    in a moment"): the picture flashed on and went straight back to the old one;
  * after 206 it is HELD ("saved on this device -- will sync when your relays answer") and, since
    that never happens, is gone at the next reload and never reaches another device.

The write gate is about not publishing over a layout nobody has read. The relay that holds this
account's private documents is the instance's own (every write goes there); its answer is that
question answered. The fix lets the read count as answered when THAT relay answered, while a pool
where it has not still holds every change.

Everything here runs the SHIPPED web client (/client) in real Chrome against a REAL relay and a REAL
instance (files index + Blossom) -- tests/client/wallpaper_harness.py -- and a second Chrome profile
is a second device with nothing cached. What is asserted is what a person sees: a screenshot pixel of
the empty desktop, plus the document the relay ends up holding.
"""
import asyncio
import os
from pathlib import Path

import pytest

from tests.client import wallpaper_harness as H

CHROME = pytest.mark.skipif(not Path(H.CHROME).exists(), reason='Chrome required')


def run(coro):
    H.start()
    H.OVERRIDES.clear()
    # The fail-first proof: PC_WALLPAPER_OVERRIDE="/static/js/client/os.js=/tmp/old-os.js,…" serves an
    # older copy of a shipped file in place of the working tree's.
    for pair in filter(None, os.environ.get('PC_WALLPAPER_OVERRIDE', '').split(',')):
        path, _, src = pair.partition('=')
        H.OVERRIDES[path] = src
    return asyncio.run(coro)


async def _published_bg(b, sha, seconds=30):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    doc = None
    while loop.time() < deadline:
        doc = await H.decoded_layout(b)
        if doc and doc.get('bg') == sha:
            return doc
        await asyncio.sleep(.3)
    raise AssertionError({'relay holds': doc, 'wanted bg': sha, 'toasts': await b.js('__toasts'),
                          'publishes': len(H.STATE['layout_pubs'])})


@CHROME
def test_a_wallpaper_is_shown_stored_read_back_and_synced_to_another_device():
    """The whole round trip, through the picker each time: an ENCRYPTED picture and a PUBLIC (plain)
    one from the drive's Backgrounds folder, and the Default tile."""
    async def main():
        a = H.Device()
        ba = await a.__aenter__()
        try:
            red = await H.put_picture(ba, 'red.png', encrypted=True, b64=H.PNG_RED)
            blue = await H.put_picture(ba, 'blue.png', encrypted=False, b64=H.PNG_BLUE)
            await H.enter_desktop(ba)
            await H.open_picker(ba)
            await H.pick(ba, red)
            await H.sees(ba, H.is_red)
            await _published_bg(ba, red)

            # A second device, nothing cached: it reads the layout and the drive back and paints it.
            bdev = H.Device()
            bb = await bdev.__aenter__()
            try:
                await H.enter_desktop(bb)
                await H.sees(bb, H.is_red)
                # Changing it THERE reaches the first device live, without a reload.
                await H.open_picker(bb)
                await H.pick(bb, blue)
                await H.sees(bb, H.is_blue)
                await _published_bg(bb, blue)
                await H.sees(ba, H.is_blue)
                # And back to the default emblem, which must be stored as "no picture".
                await H.open_picker(bb)
                await H.pick(bb, '')
                await H.sees(bb, H.is_default)
                await _published_bg(bb, '')
                await H.sees(ba, H.is_default)
                assert not await bb.js('__errors'), await bb.js('__errors')
            finally:
                await bdev.__aexit__()
            await a.reload()
            await H.enter_desktop(ba)
            await asyncio.sleep(3)
            assert H.is_default(await H.colour_at(ba)), await H.colour_at(ba)
            assert not await ba.js('__errors'), await ba.js('__errors')
        finally:
            await a.__aexit__()
    run(main())


@CHROME
def test_a_relay_in_your_own_list_that_never_answers_does_not_stop_the_wallpaper_saving():
    """THE REPORT. "Use my own relays" on, and one relay in that list never answers a private read
    (the shape filter.nostr.wine / rss.nos.social have for the reporter). The node's own relay
    answers at once. The wallpaper must be stored, and be there on a fresh device."""
    async def main():
        a = H.Device(own_relays=True)
        ba = await a.__aenter__()
        try:
            red = await H.put_picture(ba, 'red.png')
            await H.enter_desktop(ba)
            await H.open_picker(ba)
            await H.pick(ba, red)
            await H.sees(ba, H.is_red)
            await _published_bg(ba, red, seconds=45)
            await asyncio.sleep(2)
            await H.sees(ba, H.is_red, seconds=5)        # still the picture: nothing put it back
            toasts = await ba.js('__toasts')
            assert not any('try that again' in t or 'will sync when' in t for t in toasts), toasts
        finally:
            await a.__aexit__()
        b2 = H.Device(own_relays=True)
        bb = await b2.__aenter__()
        try:
            await H.enter_desktop(bb)
            await H.sees(bb, H.is_red)
        finally:
            await b2.__aexit__()
    run(main())


@CHROME
def test_a_read_the_home_relay_answers_late_holds_the_change_then_sends_it():
    """A slow read: the picture shows at once, NOTHING is published while the layout has not been
    read, and once the relay answers the change is rebased onto what it holds and sent ONCE --
    keeping a newer layout another device stored meanwhile (its folder survives)."""
    async def main():
        a = H.Device()
        ba = await a.__aenter__()
        try:
            red = await H.put_picture(ba, 'red.png')
            H.STATE['hold_layout'] = 'late'
            await H.enter_desktop(ba)
            await H.wait(ba, 'true')
            for _ in range(50):
                if H.STATE['held']:
                    break
                await asyncio.sleep(.2)
            assert H.STATE['held'], 'the layout read never went out'
            # Another device stored a layout meanwhile.
            await H.seed_layout(ba, {'v': 1, 'folders': [{'key': 'other', 'label': 'From the tablet',
                                     'icon': '#i-folder', 'views': ['notes', 'calendar']}],
                                     'order': ['folder:other'], 'hidden': [], 'pos': {}, 'bg': ''})
            await H.open_picker(ba)
            await H.pick(ba, red)
            await H.sees(ba, H.is_red)
            await asyncio.sleep(1.5)
            assert not H.STATE['layout_pubs'], 'published over a layout nobody had read'
            assert H.RELAY.release() > 0
            doc = await _published_bg(ba, red)
            assert [f['key'] for f in doc['folders']] == ['other'], doc
            await asyncio.sleep(1.5)
            assert len(H.STATE['layout_pubs']) == 1, len(H.STATE['layout_pubs'])
            await H.sees(ba, H.is_red, seconds=5)
        finally:
            await a.__aexit__()
    run(main())


@CHROME
def test_a_read_nobody_answers_keeps_the_change_on_screen_and_publishes_nothing():
    """The home relay itself never answers: the picture stays on screen, the desktop says where the
    change is, and nothing is published over a layout nobody has read. When it answers, it is sent."""
    async def main():
        a = H.Device()
        ba = await a.__aenter__()
        try:
            red = await H.put_picture(ba, 'red.png')
            await H.seed_layout(ba, {'v': 1, 'folders': [], 'order': [], 'hidden': ['notes'], 'pos': {}, 'bg': ''})
            H.STATE['hold_layout'] = 'late'
            await H.enter_desktop(ba)
            await H.open_picker(ba)
            await H.pick(ba, red)
            await H.sees(ba, H.is_red)
            await H.wait(ba, "__toasts.some(t=>t.includes('will sync when your relays answer'))", 60)
            assert not H.STATE['layout_pubs']
            await H.sees(ba, H.is_red, seconds=5)
            H.RELAY.release()
            doc = await _published_bg(ba, red, seconds=200)
            assert doc['hidden'] == ['notes'], doc
        finally:
            await a.__aexit__()
    run(main())


@CHROME
def test_a_layout_that_will_not_decrypt_is_never_published_over():
    """The relay holds this account's layout but this device cannot decrypt it (a signer that did not
    answer, a damaged copy). The picture still shows, the desktop says the signer is the problem, and
    NOTHING replaces the stored document."""
    async def main():
        a = H.Device()
        ba = await a.__aenter__()
        try:
            red = await H.put_picture(ba, 'red.png')
            bad = await H.seed_layout(ba, content='AnotNIP44atall' * 8)
            await H.enter_desktop(ba)
            await H.open_picker(ba)
            await H.pick(ba, red)
            await H.sees(ba, H.is_red)
            await H.wait(ba, "__toasts.some(t=>/signer/.test(t))", 60)
            await asyncio.sleep(1.5)
            assert not H.STATE['layout_pubs'], 'published over a layout that could not be read'
            assert H.layout_docs() == [bad]
        finally:
            await a.__aexit__()
    run(main())


@CHROME
def test_a_refused_save_is_put_back_and_said_and_the_next_one_lands():
    """The relay REFUSES the write: the old wallpaper comes back on screen and the desktop says the
    change is not stored (CLAUDE.md, desktop gotcha 3) -- then, with the relay accepting again, the
    same choice is stored and survives a reload."""
    async def main():
        a = H.Device()
        ba = await a.__aenter__()
        try:
            red = await H.put_picture(ba, 'red.png')
            await H.enter_desktop(ba)
            await asyncio.sleep(2)              # the layout read has answered (empty account)
            H.STATE['refuse_layout'] = True
            await H.open_picker(ba)
            await H.pick(ba, red)
            await H.wait(ba, "__toasts.some(t=>t.includes('not stored'))", 30)
            await H.sees(ba, H.is_default)
            assert not H.layout_docs()
            H.STATE['refuse_layout'] = False
            await H.open_picker(ba)
            await H.pick(ba, red)
            await _published_bg(ba, red)
            await a.reload()
            await H.enter_desktop(ba)
            await H.sees(ba, H.is_red)
        finally:
            await a.__aexit__()
    run(main())


@CHROME
def test_a_big_existing_layout_keeps_everything_when_the_wallpaper_changes():
    """An account with a large arrangement -- many folders, hidden icons, placed icons and widgets.
    Changing the wallpaper keeps every one of them, and a fresh device draws the picture."""
    async def main():
        a = H.Device()
        ba = await a.__aenter__()
        try:
            red = await H.put_picture(ba, 'red.png')
            views = await ba.js("[...document.querySelectorAll('.sidebar .nav .nav-item[data-view]')]"
                                ".map(x=>x.dataset.view)")
            folders = [{'key': f'f{i}', 'label': f'Folder {i}', 'icon': '#i-folder',
                        'views': [f'x{i}a', f'x{i}b']} for i in range(40)]
            widgets = [{'id': f'w{i}', 'type': 'note', 'x': (i % 6) / 6, 'y': (i // 6) / 6, 'size': 's',
                        'cfg': {'text': 'note %d ' % i + 'lorem ipsum ' * 20}} for i in range(12)]
            big = {'v': 1, 'folders': folders, 'order': ['folder:' + f['key'] for f in folders],
                   'hidden': views[:5], 'pos': {}, 'bg': '', 'widgets': widgets}
            await H.seed_layout(ba, big)
            await H.enter_desktop(ba)
            await H.open_picker(ba)
            await H.pick(ba, red)
            await H.sees(ba, H.is_red)
            doc = await _published_bg(ba, red)
            assert len(doc['folders']) == 40 and len(doc.get('widgets') or []) == 12, \
                (len(doc['folders']), len(doc.get('widgets') or []))
            assert doc['hidden'] == views[:5]
        finally:
            await a.__aexit__()
        b2 = H.Device()
        bb = await b2.__aenter__()
        try:
            await H.enter_desktop(bb)
            await H.sees(bb, H.is_red)
        finally:
            await b2.__aexit__()
    run(main())
