"""⋯ → Invitation links → Create invite link MAKES a link, says so, and the link lets somebody in.

Reported 2026-10-24: "Trying to create invite link with Community invite links, nothing happens when I click invite
link, for an armada room". The only test of that dialog (test_concord_dialog_buttons_full_app.py) checks that it
OPENS; nothing ever pressed Create. A real node and relay, the shipped client: Alice creates a community, opens
Invitation links, presses Create invite link; it must answer (a toast, or the error line) within a bounded time,
the new link must be on the list when the dialog is opened again, and Bob must be able to join with it.
"""
import asyncio
import json
import time

from tests.client import realnode_harness as R
from tests.client.test_concord_between_two_accounts_real_relay_full_app import (
    ALICE, BOB, CHROME, STATE, concord, create, join, run)

ANSWER = ("(()=>{const t=(window.__toastLog||[]).slice(-6).join(' | ');const e=document.querySelector('#cc-mint-error');"
          "return {toast:t,error:e&&!e.hidden?e.textContent:'',busy:!!(document.querySelector('#cc-mint-link')||{}).disabled,"
          "open:!!document.querySelector('#cc-mint-link')}})()")


async def open_links(d):
    b = d.b
    await b.js("document.activeElement&&document.activeElement.blur();document.getElementById('cc-head-more').click();true")
    await R.wait(b, "!!document.querySelector('.menu-pop [data-m=\"cc-manage-links\"]')", 20, 'the ⋯ menu')
    await b.js("document.querySelector('.menu-pop [data-m=\"cc-manage-links\"]').click();true")
    await R.wait(b, "!!document.querySelector('#cc-mint-link') && !!document.querySelector('#cc-owned-links')", 60,
                 'the invitation links dialog', state=STATE)


@CHROME
def test_create_invite_link_makes_a_link_that_lets_somebody_in():
    async def scenario(n):
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob:
            await create(alice, 'Linked')
            await R.wait(alice.b, "!!document.querySelector('#cc-head-more')", 60, 'the community open', state=STATE)
            await open_links(alice)
            # Everything the button and its status line SAY from the click on, however fast the relays are.
            await alice.b.js("""(()=>{window.__said=[];const m=document.querySelector('#cc-mint-link'),e=document.querySelector('#cc-mint-error');
              const note=()=>__said.push((m.textContent||'')+' | '+(e&&!e.hidden?e.textContent:''));
              const o=new MutationObserver(note);o.observe(m,{childList:true,characterData:true,subtree:true});
              if(e)o.observe(e,{childList:true,characterData:true,subtree:true,attributes:true});return true})()""")
            await alice.b.js("window.__copies.length=0;(window.__toastLog||[]).length=0;document.querySelector('#cc-mint-link').click();true")
            t0 = time.monotonic()
            got = None
            while time.monotonic() - t0 < 90:
                got = await alice.b.js(ANSWER)
                if 'Invitation created' in got['toast'] or got['error'].startswith('The link was not created'):
                    break
                await asyncio.sleep(.5)
            took = time.monotonic() - t0
            said = await alice.b.js("window.__said||[]")
            # It SAYS it is working the moment it is pressed -- a quietly greyed button read as "nothing happens".
            assert any('Creating' in x and 'relays' in x for x in said), ('Create gave no sign it was working', said)
            assert got and 'Invitation created' in got['toast'], ('Create invite link answered nothing / failed', took, got)
            assert took < 45, ('creating a link took %.0fs with nothing on screen' % took, got)
            link = await alice.b.js("__copies.filter(x=>x.includes('/invite/')).pop()||''")
            assert '/invite/' in link, link
            # The new link is listed when the dialog is opened again.
            await alice.b.js("document.querySelectorAll('.modal .btn-close,.modal [data-close]').forEach(x=>x.click());try{__PC.closeModal()}catch(_){};true")
            await open_links(alice)
            rows = await alice.b.js("[...document.querySelectorAll('#cc-owned-links > div')].length")
            assert rows >= 1, 'the created link is not on the list'
            # And it lets somebody in.
            await join(bob, link)
            await R.wait(bob.b, "JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)||'[]').some(r=>r.name==='Linked')",
                         30, 'Bob joined through the created link')
    run(scenario)


@CHROME
def test_a_member_who_may_not_invite_is_told_so_and_still_has_the_communitys_link():
    """The report came from a MEMBER of an Armada community (Developer's Quarters), not its owner. A member the
    community does not let invite must be TOLD, the moment the dialog opens -- and can still copy the community's
    own link, which is what "invite somebody" means for them."""
    async def scenario(n):
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob:
            url = await create(alice, 'Members only')
            await join(bob, url)
            await R.open_room_channel(bob)
            await open_links(bob)
            await asyncio.sleep(1)
            seen = await bob.b.js("(()=>{const m=document.querySelector('#cc-mint-link'),e=document.querySelector('#cc-mint-error');"
                                  "return {disabled:!!(m&&m.disabled),said:e&&!e.hidden?e.textContent:'',"
                                  "copy:[...document.querySelectorAll('#cc-owned-links button')].map(b=>b.textContent)}})()")
            await bob.b.js("(window.__toastLog||[]).length=0;document.querySelector('#cc-mint-link').click();true")
            await asyncio.sleep(4)
            after = await bob.b.js(ANSWER)
            print("MEMBER", json.dumps({'seen': seen, 'after': after}))
            assert seen['disabled'] and seen['said'], ('a member who may not invite is not told so', seen)
            assert any('Copy' in c for c in seen['copy']), ('the member has no way to share the community link', seen)
    run(scenario)
