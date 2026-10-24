"""A forum channel you just created stays in the channel list.

Git issue "Forum Channel Disappears" (Vyram, 2026-10-24, with a video): create a forum channel, hit the menu, and the
new channel is gone from the list; saving the community's appearance brings it back. The video shows "Forum #Tester2
created", Tester2 listed, the menu opened -- and the list without it.

A real node and relay (realnode_harness), the shipped client: create a community, create a FORUM channel through the
new-channel dialog, open the header menu like the video, then watch the channel list through the background re-reads
that follow (the control plane is re-read about once a minute, refreshRoomMetadata) and a full repaint.
"""
import asyncio
import json

from tests.client import realnode_harness as R
from tests.client.test_concord_between_two_accounts_real_relay_full_app import (
    ALICE, CHROME, STATE, create, press_dialog, run)

LISTED = "!!document.querySelector('[data-cc-channel=\"tester2\"],[data-cc-channel=\"Tester2\"]')"
NAMES = "[...document.querySelectorAll('[data-cc-channel]')].map(x=>x.dataset.ccChannel)"


@CHROME
def test_a_new_forum_channel_is_still_listed_after_the_menu_and_the_next_reads():
    async def scenario(n):
        async with R.Device(n, ALICE) as alice:
            b = alice.b
            await create(alice, 'Forums')
            await R.wait(b, "!!document.querySelector('#cc-add-channel')", 60, 'the community open', state=STATE)
            await b.js("document.querySelector('#cc-add-channel').click();true")
            await R.wait(b, "!!document.querySelector('.pc-cord-spaces-dialog .uiprompt-in')", 20, 'the new-channel dialog')
            await b.js("(()=>{const i=document.querySelector('.pc-cord-spaces-dialog .uiprompt-in');i.value='Tester2';"
                       "i.dispatchEvent(new Event('input',{bubbles:true}));"
                       "document.querySelector('.pc-cord-spaces-dialog [data-cc-view=\"forum\"]').click();return true})()")
            await press_dialog(alice, 'Create channel')
            await R.wait(b, "(window.__toastLog||[]).some(t=>/Tester2 created/.test(t))", 40, 'the forum created', state=STATE)
            await R.wait(b, LISTED, 20, 'Tester2 listed right after creating it', state="(%s)" % NAMES)
            # The menu, as in the video.
            await b.js("(()=>{const m=document.querySelector('#cc-head-more');if(m)m.click();return true})()")
            await asyncio.sleep(1)
            await b.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));"
                       "document.querySelectorAll('.menu-pop,.pop-backdrop').forEach(x=>x.remove());true")
            seen = []
            loop = asyncio.get_running_loop()
            end = loop.time() + 95
            while loop.time() < end:
                await b.js("document.activeElement&&document.activeElement.blur();try{PCConcord.backgroundRender()}catch(_){};true")
                names = await b.js(NAMES)
                seen.append(names)
                assert any(x.lower() == 'tester2' for x in names), \
                    ('the new forum channel dropped out of the list after %d s' % (len(seen) * 3), names,
                     await b.js("(()=>{const r=JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)||'[]');"
                                "return r.map(x=>({name:x.name,channels:(x.channels||[]).map(c=>c.name+':'+(c.view||''))}))})()"))
                await asyncio.sleep(3)
    run(scenario)


ROOMS_NAMED = ("JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)||'[]')"
               ".filter(r=>r&&r.name===%s).length")


@CHROME
def test_joining_a_community_you_are_already_in_does_not_add_a_second_copy():
    """Git issue "Posterchan Community Not Loading" (Vyram, 2026-10-24): "Joining again says you already have joined the
    community. But it creates a new community in your list & shows a double." Bob joins by the link, opens the SAME link
    again -- the preview says he is already in -- presses its button, and his list must still hold ONE copy, on screen
    and saved, after a reload too."""
    from tests.client.test_concord_between_two_accounts_real_relay_full_app import BOB, join, concord

    async def scenario(n):
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob:
            url = await create(alice, 'Twice')
            await join(bob, url)
            await R.wait(bob.b, (ROOMS_NAMED % json.dumps('Twice')) + "===1", 30, 'Bob holds the community once')
            # The same link, opened again.
            await bob.b.js("PCConcord.openInvite(%s);true" % json.dumps(url))
            await R.wait(bob.b, "!!document.querySelector('#cc-invite-sheet:not(.hidden) #cc-invite-accept')", 60, 'the preview again')
            kicker = await bob.b.js("(document.querySelector('.cc-invite-kicker')||{}).textContent||''")
            await bob.b.js("document.querySelector('#cc-invite-accept').click();true")
            await asyncio.sleep(6)
            saved = await bob.b.js(ROOMS_NAMED % json.dumps('Twice'))
            shown = await bob.b.js("[...document.querySelectorAll('.cc-server')].filter(s=>/Twice|^T$/.test((s.getAttribute('title')||s.getAttribute('aria-label')||s.textContent||'').trim())).length")
            assert 'already' in kicker.lower(), ('the second preview did not know Bob is in', kicker)
            assert saved == 1, ('joining again added a second copy to the saved list', saved)
            assert shown <= 1, ('joining again shows the community twice in the rail', shown)
            await bob.b.js("location.reload();true")
            await concord(bob)
            await asyncio.sleep(4)
            assert await bob.b.js(ROOMS_NAMED % json.dumps('Twice')) == 1, 'a reload brought the double back'
    run(scenario)
