"""A ban HOLDS: the banned person cannot come back through the invite link, and what they post stays hidden.

Reported 2026-10-24: "there is an imposter vyram that I banned a few times, not sure our banning code works". The
existing ban test (test_concord_between_two_accounts_real_relay_full_app.py) proves the banned member stops READING
new messages and leaves the member lists. It never asked the two things an imposter does next:

  * open the community's invite link again and accept it, and
  * post.

A real node and relay (realnode_harness), three accounts, the shipped client. Alice owns the community, Bob and Carol
join by its link, Alice bans Carol. Then Carol opens the SAME link again and accepts, and posts; Alice and Bob must
not show anything Carol posts after the ban, Carol must not read what is said after it, and Carol must stay off both
member lists -- after a reload too.
"""
import asyncio
import json

from tests.client import realnode_harness as R
from tests.client.test_concord_between_two_accounts_real_relay_full_app import (
    ALICE, BOB, CAROL, CHROME, LINES, MEMBERS, STATE, community, concord, join, member_menu, press_dialog, send, shows,
    run)


async def _ban(alice, pk):
    offered = await member_menu(alice, pk)
    assert 'Ban from community' in offered, offered
    await alice.b.js("document.querySelector('[data-cc-member-ban]').click();true")
    await press_dialog(alice, 'Ban')
    await R.wait(alice.b, "(window.__toastLog||[]).some(t=>/Member banned/.test(t))", 90, 'the ban', state=STATE)


async def _try_rejoin(carol, url):
    """Carol opens the invite again and presses whatever the preview offers. Returns what the preview said."""
    await concord(carol)
    await carol.b.js("PCConcord.openInvite(%s);true" % json.dumps(url))
    await R.wait(carol.b, "!!document.querySelector('#cc-invite-sheet:not(.hidden)')", 60, 'the invite preview again')
    await asyncio.sleep(2)
    said = await carol.b.js("(document.querySelector('#cc-invite-sheet')||{}).innerText||''")
    await carol.b.js("(()=>{const b=document.querySelector('#cc-invite-accept');if(b&&!b.disabled)b.click();return true})()")
    await asyncio.sleep(8)
    return said


async def _try_post(carol, text):
    """Post the way a person would; if the room refuses (it should, once Carol is removed), say so."""
    await carol.b.js("document.activeElement&&document.activeElement.blur();true")
    try:
        await R.open_room_channel(carol)
    except Exception:
        pass
    usable = await carol.b.js("(()=>{const i=document.querySelector('#cc-input');return !!i&&!i.disabled})()")
    if usable:
        await R.concord_send(carol, text)
        await asyncio.sleep(6)
    return usable


@CHROME
def test_a_banned_member_cannot_come_back_through_the_invite_link_or_be_read():
    async def scenario(n):
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob, R.Device(n, CAROL) as carol:
            url = await community(n, alice, bob, 'Held')
            await join(carol, url)
            await R.open_room_channel(carol)
            await send(carol, 'carol before the ban')
            for d in (alice, bob):
                await shows(d, 'carol before the ban')
            for d in (alice, bob, carol):
                await d.b.js("document.activeElement&&document.activeElement.blur();true")

            await _ban(alice, R.pubkey(CAROL))

            # The imposter's next moves come from a FRESH device (a new browser, cleared storage): the device that was
            # banned still holds the removed room and opens it read-only, which proves nothing about the ban.
            async with R.Device(n, CAROL) as carol2:
                preview = await _try_rejoin(carol2, url)
                posted = await _try_post(carol2, 'carol after the ban')
                await send(alice, 'alice after the ban')
                await shows(bob, 'alice after the ban', 60, 'Bob still reads the room')
                await asyncio.sleep(10)
                carol_reads = any('alice after the ban' in x for x in await carol2.b.js(LINES))
                carol2_state = await carol2.b.js(STATE)

            got = {
                'preview': preview[:300],
                'carol_could_post': posted,
                'alice_shows_carol': any('carol after the ban' in x for x in await alice.b.js(LINES)),
                'bob_shows_carol': any('carol after the ban' in x for x in await bob.b.js(LINES)),
                'carol_reads_alice': carol_reads,
                'carol2': carol2_state,
            }
            for d, who in ((alice, 'alice'), (bob, 'bob')):
                await d.b.js("document.activeElement&&document.activeElement.blur();"
                             "(()=>{const m=document.querySelector('#cc-members');if(!document.querySelector('.cc-members-pane .cc-member')&&m)m.click();})();true")
                await asyncio.sleep(2)
                got[who + '_members'] = await d.b.js(MEMBERS)
            # And after a reload, on the owner's own device.
            await alice.b.js("location.reload();true")
            await asyncio.sleep(2)
            await R.wait(alice.b, "!!window.__PC && !!__PC.me() && document.readyState==='complete'", 60, 'the page after its reload')
            await concord(alice)
            await R.open_room_channel(alice)
            await asyncio.sleep(8)
            got['alice_shows_carol_after_reload'] = any('carol after the ban' in x for x in await alice.b.js(LINES))

            assert not got["carol_reads_alice"], ('the banned member rejoined through the link and reads the room', got)
            assert not got['alice_shows_carol'] and not got['bob_shows_carol'], ('what the banned member posted is shown', got)
            assert not got['alice_shows_carol_after_reload'], ('after a reload the banned member\'s post is shown', got)
            assert R.pubkey(CAROL) not in got['alice_members'] and R.pubkey(CAROL) not in got['bob_members'], \
                ('the banned member is back on a member list', got)
    run(scenario)
