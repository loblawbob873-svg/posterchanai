"""CONCORD BETWEEN REAL PEOPLE -- two (and three) accounts, each in its own shipped client, on this node's REAL relay.

Every other Concord member-to-member test runs inside ONE client (moderation, pins, threads) or between two pages
joined by an in-page fake relay (concord_fixture_relay.py). Neither can see the bugs that only exist BETWEEN two
clients -- the ones reported 2026-10-22: a mirror serving half a history as all of it, a leave that did not stick,
read marks republished every few seconds. Here each person is a separate key in a separate Chrome profile running
the shipped client, signed in through the login screen; the community is made with the real Create button and
joined with the real invite link; every byte goes through the shipped RelayServer over PosterChanDB with the WoT
gate on (tests/client/realnode_harness.py), and the public CORD relays every Concord client also uses (invites,
membership vaults, the backups a community made here lists) are reached through this node's real tunnel, each
played by a local relay (`Node(internet=R.CORD_PUBLIC)`). Every assertion is what the OTHER person's screen shows.

  live        Alice creates, Bob joins by the invite link: both see #general; Alice's message appears on Bob's open
              client decrypted and live, Bob's reply on Alice's.
  threads     Bob replies in a thread: Alice's channel collapses it under the message it answers and her thread view
              shows it; a channel Alice creates appears on Bob's client.
  moderation  Alice makes Bob moderator: Bob's client offers him Kick on another member. Alice bans Carol: Bob still
              reads the room after the key rotation, Carol's client can no longer read what is posted after and says
              she was removed, and Carol leaves Bob's member list. (Carol's earlier messages stay: nothing in the
              client hides a banned member's history -- only the member lists filter `banned`.)
  pins        Alice pins a message: Bob's pinned list shows it.
  leave       Bob leaves: Alice's member list drops him, and Bob's fresh second device does not bring it back.

Bugs these found (2026-10-22), each failing here before its fix:
  * an invite opened in the first seconds after Concord starts asked NO relay ("invite bundle was not found on its
    relays"): the start-up membership sweep had every CORD relay in flight and queryFrom skips a busy relay --
    concord.js `hydrateInvite(..., {urgent})`;
  * the creator could not post in the room just made ("channel key is unavailable"), 1 run in 3: the creator's own
    Invite List event added a placeholder row whose invite read was then written back over the real room, channel
    ids and all -- concord.js `activateJoinedRoom` keeps a row that holds a bundle by then;
  * a member banned by the owner stayed on another moderator's member list for good: right-clicking a member left
    that row's button focused, and backgroundRender() deferred every full repaint for ANY focused element in
    Concord -- now only for something being typed in;
  * the banned member's room looked untouched (composer open, nothing said) while nothing new could arrive -- a
    `removed` bundle is now read-only and says so, like a dissolved one.

Measured, by design, and allowed for here: roles, pins and bans travel on the control plane, which a member's
client re-reads about once a minute (refreshRoomMetadata, minInterval 60 s); and while the cursor sits in the
message box only the messages repaint (a full render would close a phone's keyboard), so the person here moves
the cursor out before looking at the channel list or the member list.
"""
import asyncio
import json

import pytest

from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
ALICE, BOB, CAROL = 23, 24, 25        # Alice = 23 / Bob = 24 as in the DM suite; Carol is the third member
CFG = {'wot_enabled': True}           # a node as it runs: all three are members of its web of trust

# The clipboard is the one thing captured (the invite link the Create button copies); the real copy still runs.
CLIP = ("(()=>{window.__copies=window.__copies||[];const p=__PC;if(p.__clipWrapped)return true;p.__clipWrapped=true;"
        "const c=p.copyValue;p.copyValue=(v,...a)=>{__copies.push(String(v));try{return c&&c.call(p,v,...a);}catch(_){return true;}};"
        "return true})()")
LINES = ("[...document.querySelectorAll('.cc-message[data-message-id]')]"
         ".map(r=>((r.querySelector('.cc-message-body')||r).innerText||'').trim())")
STATE = ("({lines:%s,toasts:(window.__toastLog||[]).slice(-8),errors:(window.__errors||[]).slice(-6),"
         "head:((document.querySelector('.cc-conversation header')||{}).innerText||'').slice(0,200)})" % LINES)
ROOMS = "JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)||'[]')"
MEMBERS = "[...document.querySelectorAll('.cc-members-pane .cc-member[data-cc-member]')].map(r=>r.dataset.ccMember)"


async def concord(d):
    await d.b.js(CLIP)
    await d.b.js("__PC.switchMessagesTab('concord');true")
    await R.wait(d.b, '!!window.PCConcord && !!window.PosterCord && !!window.PosterCordReader', 40, 'Concord loaded')


async def create(d, name):
    """Concord -> Create community -> a name -> Create on relays; returns the invite link it copied."""
    b = d.b
    await concord(d)
    await R.wait(b, "!!document.querySelector('#cc-create-go')", 30, 'the Create dialog')
    await b.js("__copies.length=0;document.querySelector('#cc-community-name').value=%s;"
               "document.querySelector('#cc-create-go').click();true" % json.dumps(name))
    await R.wait(b, "__copies.some(x=>x.includes('/invite/'))", 90, 'the community created and its invite copied')
    # A headless page has no clipboard, so the app shows the link in its "Copy this:" box; the person closes it.
    for _ in range(30):
        if await b.js("(()=>{const m=[...document.querySelectorAll('.uiconfirm-msg')].find(x=>/Copy this/.test(x.textContent));"
                      "if(!m)return false;const c=[...m.closest('.uiconfirm').querySelectorAll('button')].find(x=>x.textContent.trim()==='Close');"
                      "if(c)c.click();return true})()"):
            break
        await asyncio.sleep(.1)
    return await b.js("__copies.filter(x=>x.includes('/invite/')).pop()")


async def join(d, url):
    """The link opened in Concord (a tap or a paste): its preview, then Accept and join."""
    b = d.b
    await concord(d)
    await b.js("PCConcord.openInvite(%s);true" % json.dumps(url))
    await R.wait(b, "!!document.querySelector('#cc-invite-sheet:not(.hidden) #cc-invite-accept')", 60, 'the invite preview')
    await b.js("document.querySelector('#cc-invite-accept').click();true")
    await R.wait(b, "(window.__toastLog||[]).some(t=>/community joined/.test(t))", 60, 'joined')


async def shows(d, text, seconds=40, what=None):
    await R.wait(d.b, '%s.some(x=>x.includes(%s))' % (LINES, json.dumps(text)), seconds,
                 what or ('the channel shows %r' % text), state=STATE)


async def send(d, text):
    await R.concord_send(d, text)
    await shows(d, text, 30, 'the sender sees %r' % text)
    # Delivered, not just painted: the optimistic row is replaced by the signed one once a relay took it.
    await R.wait(d.b, "[...document.querySelectorAll('.cc-message[data-message-id]')].some(r=>r.innerText.includes(%s)"
                      "&&!r.dataset.messageId.startsWith('pending-'))" % json.dumps(text), 30,
                 '%r delivered' % text, state=STATE)


async def message_action(d, text, attr):
    """The ⋯ on the message reading `text`, then its `attr` action (data-cc-reply, data-cc-pin, ...)."""
    row = ("[...document.querySelectorAll('.cc-message[data-message-id]')].find(r=>r.innerText.includes(%s))"
           % json.dumps(text))
    await R.wait(d.b, "!!(%s||{querySelector:()=>null}).querySelector('[%s]')" % (row, attr), 20,
                 'the %s action on %r' % (attr, text), state=STATE)
    await d.b.js("(()=>{const r=%s,t=r.querySelector('[data-cc-actions]');if(t)t.click();"
                 "r.querySelector('[%s]').click();return true})()" % (row, attr))


async def member_menu(d, pk):
    """Right-click the member's row, as a person does on a desktop."""
    b = d.b
    await R.wait(b, "(()=>{if(!document.querySelector('.cc-members-pane .cc-member')){const m=document.querySelector('#cc-members');"
                    "m&&m.click();}return !!document.querySelector('.cc-members-pane .cc-member[data-cc-member=\"%s\"]')})()" % pk,
                 40, 'the member list shows %s' % pk[:8], state="({members:%s,state:%s})" % (MEMBERS, STATE))
    x, y = await b.js("(()=>{const r=document.querySelector('.cc-members-pane .cc-member[data-cc-member=\"%s\"]')"
                      ".getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()" % pk)
    await b.call('Input.dispatchMouseEvent', dict(type='mousePressed', x=x, y=y, button='right', buttons=2, clickCount=1))
    await b.call('Input.dispatchMouseEvent', dict(type='mouseReleased', x=x, y=y, button='right', buttons=0, clickCount=1))
    await R.wait(b, "!!document.querySelector('.cc-member-menu')", 15, 'the member menu')
    return await b.js("[...document.querySelectorAll('.cc-member-menu button')].map(x=>x.textContent.trim())")


async def press_dialog(d, label):
    """The confirmation dialog's button labelled `label` (app.js uiConfirm / cord-spaces dialogs)."""
    find = ("[...document.querySelectorAll('.uiconfirm button,.modal button,[role=dialog] button')]"
            ".find(x=>x.textContent.trim()===%s&&x.getBoundingClientRect().width>0)" % json.dumps(label))
    await R.wait(d.b, '!!' + find, 20, 'the %r button' % label)
    await d.b.js(find + '.click();true')


async def community(n, alice, bob, name='Two people'):
    """Alice creates, Bob joins by the link, both on #general."""
    url = await create(alice, name)
    await join(bob, url)
    await R.open_room_channel(bob)
    await R.open_room_channel(alice)
    return url


def run(scenario):
    async def main():
        with R.Node(cfg=CFG, members=[R.pubkey(ALICE), R.pubkey(BOB), R.pubkey(CAROL)], internet=R.CORD_PUBLIC) as n:
            await scenario(n)
    asyncio.run(main())


def no_errors(*ds):
    async def check():
        for d in ds:
            errs = [e for e in await d.b.js('window.__errors||[]') if 'ResizeObserver' not in e]
            assert not errs, errs
    return check()


# ----------------------------------------------------------------------------------------------- 1. live
@CHROME
def test_a_message_and_its_reply_arrive_live_on_the_other_client():
    async def scenario(n):
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob:
            await community(n, alice, bob)
            assert [r['name'] for r in await bob.b.js(ROOMS)] == ['Two people']
            await send(alice, 'hello Bob from Alice')
            await shows(bob, 'hello Bob from Alice', what="Alice's message, decrypted, on Bob's open client")
            await send(bob, 'hi Alice, Bob here')
            await shows(alice, 'hi Alice, Bob here', what="Bob's reply on Alice's open client")
            assert not [t for t in await alice.toasts() + await bob.toasts()
                        if 'not sent' in t or 'needs attention' in t or 'could not' in t]
            await no_errors(alice, bob)
    run(scenario)


# ----------------------------------------------------------------------------------------------- 2. threads, channels
@CHROME
def test_a_thread_reply_and_a_new_channel_reach_the_other_client():
    async def scenario(n):
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob:
            await community(n, alice, bob, 'Threads')
            await send(alice, 'which day works for the meetup?')
            await shows(bob, 'which day works for the meetup?')
            # Bob answers IN A THREAD: Reply on Alice's message, then Send.
            await message_action(bob, 'which day works for the meetup?', 'data-cc-reply')
            await send(bob, 'thursday suits me')
            # Alice's channel: the reply is collapsed under the message it answers, not in the channel's flow.
            await R.wait(alice.b, "!!document.querySelector('.cc-thread-summary') && "
                                  "document.querySelector('.cc-thread-summary').innerText.includes('thursday suits me')", 40,
                         "Alice's channel summarises Bob's thread reply under her message", state=STATE)
            flow = await alice.b.js(LINES)
            assert not any('thursday suits me' in x for x in flow), ('the thread reply is in the channel flow', flow)
            # ...and her thread view shows it.
            await alice.b.js("document.querySelector('.cc-thread-summary .cc-ts-open').click();true")
            await R.wait(alice.b, "!!document.querySelector('.cc-thread-bar')", 20, 'the thread view', state=STATE)
            await shows(alice, 'thursday suits me', what="Bob's reply in Alice's thread view")
            await shows(alice, 'which day works for the meetup?', what='the root message in the thread view')
            await alice.b.js("document.querySelector('#cc-thread-back').click();true")
            await R.wait(alice.b, "!document.querySelector('.cc-thread-bar')", 20, 'back in the channel')
            # A channel Alice creates appears on Bob's client.
            await alice.b.js("document.querySelector('#cc-add-channel').click();true")
            await R.wait(alice.b, "!!document.querySelector('.pc-cord-spaces-dialog .uiprompt-in')", 20, 'the new-channel dialog')
            await alice.b.js("(()=>{const i=document.querySelector('.pc-cord-spaces-dialog .uiprompt-in');i.value='announcements';"
                             "i.dispatchEvent(new Event('input',{bubbles:true}));return true})()")
            await press_dialog(alice, 'Create channel')
            await R.wait(alice.b, "(window.__toastLog||[]).some(t=>/#announcements created/.test(t))", 40, 'the channel created',
                         state=STATE)
            # Bob's cursor leaves the message box. While it is there, backgroundRender() deliberately repaints only the
            # messages (a full render replaces the textarea and closes a phone's keyboard), so the channel list -- and
            # a new channel in it -- waits for the box to lose focus: measured, the channel was in Bob's saved room
            # within seconds and still not on screen 100 s later with the cursor left in the box.
            await bob.b.js("document.activeElement&&document.activeElement.blur();true")
            await R.wait(bob.b, "!!document.querySelector('[data-cc-channel=\"announcements\"]')", 60,
                         "#announcements on Bob's client", state=STATE)
            await R.open_room_channel(alice, 'announcements')
            await send(alice, 'the meetup is on thursday')
            await R.open_room_channel(bob, 'announcements')
            await shows(bob, 'the meetup is on thursday', what="Alice's post in the new channel on Bob's client")
            await no_errors(alice, bob)
    run(scenario)


# ----------------------------------------------------------------------------------------------- 3. roles, moderation
async def menu_offers(d, pk, label, seconds=60):
    """Re-open `pk`'s member menu until it offers `label` (the other client's control read decides what it may do)."""
    loop = asyncio.get_running_loop()
    end, seen = loop.time() + seconds, []
    while loop.time() < end:
        seen = await member_menu(d, pk)
        await d.b.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));"
                     "document.querySelectorAll('.cc-member-menu').forEach(m=>m.remove());true")
        if label in seen:
            return seen
        await asyncio.sleep(2)
    raise R.StepFailed({'expected': '%s offered on %s' % (label, pk[:8]), 'seen': seen})


@CHROME
def test_a_moderator_and_a_ban_take_effect_on_the_other_clients():
    async def scenario(n):
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob, R.Device(n, CAROL) as carol:
            url = await community(n, alice, bob, 'Moderated')
            await join(carol, url)
            await R.open_room_channel(carol)
            await send(carol, 'carol says hi')
            await send(bob, 'bob says hi')
            for d in (alice, bob):
                await shows(d, 'carol says hi')
            await shows(alice, 'bob says hi')
            for d in (alice, bob, carol):        # the cursor leaves the box, so the member list repaints (see threads)
                await d.b.js("document.activeElement&&document.activeElement.blur();true")
            # Before: Bob, a plain member, is offered no moderation on Carol.
            before = await member_menu(bob, R.pubkey(CAROL))
            await bob.b.js("document.querySelectorAll('.cc-member-menu').forEach(m=>m.remove());true")
            assert 'Kick from community' not in before and 'Ban from community' not in before, before
            # Alice makes Bob moderator, from her member list.
            offered = await member_menu(alice, R.pubkey(BOB))
            assert 'Make moderator' in offered, offered
            await alice.b.js("document.querySelector('[data-cc-member-role=\"moderator\"]').click();true")
            await R.wait(alice.b, "(window.__toastLog||[]).some(t=>/Role granted/.test(t))", 40, 'the role granted', state=STATE)
            # Bob's client, which read nothing from Alice's: it now offers him the moderator's Kick. Roles live on the
            # control plane, which a member's client re-reads about once a minute (refreshRoomMetadata, minInterval
            # 60 s; measured 64 s), so allow for that.
            await menu_offers(bob, R.pubkey(CAROL), 'Kick from community', 120)
            # Alice bans Carol (the owner's ban also rotates the community keys).
            offered = await member_menu(alice, R.pubkey(CAROL))
            assert 'Ban from community' in offered, offered
            await alice.b.js("document.querySelector('[data-cc-member-ban]').click();true")
            await press_dialog(alice, 'Ban')
            await R.wait(alice.b, "(window.__toastLog||[]).some(t=>/Member banned/.test(t))", 90, 'the ban', state=STATE)
            said = [t for t in await alice.toasts() if 'Member banned' in t]
            assert said and 'not rotated' not in said[-1], said
            # What is said after the ban reaches Bob, who kept his keys, and never Carol.
            await send(alice, 'posted after the ban')
            await shows(bob, 'posted after the ban', 60, 'Bob still reads the room after the rotation')
            await asyncio.sleep(10)
            assert not any('posted after the ban' in x for x in await carol.b.js(LINES)), 'the banned member reads new messages'
            # Carol leaves Bob's member list.
            await R.wait(bob.b, "(()=>{const m=document.querySelector('#cc-members');if(!document.querySelector('.cc-members-pane .cc-member')&&m)m.click();"
                                "return !!document.querySelector('.cc-members-pane .cc-member') && !%s.includes(%s)})()"
                         % (MEMBERS, json.dumps(R.pubkey(CAROL))), 100, "Carol gone from Bob's member list",
                         state="({members:%s,active:(document.activeElement||{}).outerHTML.slice(0,200),state:%s})" % (MEMBERS, STATE))
            # Carol's room SAYS she is out, and does not take a message nobody can read any more.
            await R.wait(carol.b, "(()=>{const i=document.querySelector('#cc-input');return !!i&&i.disabled&&"
                                  "/removed/i.test((document.querySelector('.cc-conversation header')||{}).innerText||'')})()", 60,
                         "Carol's room says she was removed and offers no composer",
                         state="({input:(document.querySelector('#cc-input')||{}).outerHTML,state:%s,rooms:JSON.stringify(%s.map(r=>r.cord.bundle.removed))})" % (STATE, ROOMS))
            await no_errors(alice, bob)
    run(scenario)


# ----------------------------------------------------------------------------------------------- 4. pins
PINNED = ("[...document.querySelectorAll('.cc-pin-list .cc-pin-item')].map(x=>x.innerText)")


@CHROME
def test_a_pin_shows_in_the_other_members_pinned_list():
    async def scenario(n):
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob:
            await community(n, alice, bob, 'Pins')
            await send(alice, 'house rules: be kind')
            await send(alice, 'just chatting')
            await shows(bob, 'house rules: be kind')
            await message_action(alice, 'house rules: be kind', 'data-cc-pin')
            await R.wait(alice.b, "(window.__toastLog||[]).some(t=>/Message pinned/.test(t))", 40, 'pinned', state=STATE)
            # Bob opens 📌 Pinned: the pin is a control-plane write, which his client re-reads about once a minute.
            loop = asyncio.get_running_loop()
            end, seen = loop.time() + 100, []
            while loop.time() < end:
                await bob.b.js("document.activeElement&&document.activeElement.blur();document.querySelector('#cc-pins').click();true")
                await R.wait(bob.b, "!!document.querySelector('.cc-pin-list,.modal')", 10, 'the pinned list')
                seen = await bob.b.js(PINNED)
                await bob.b.js("__PC.closeModal&&__PC.closeModal();true")
                if any('house rules: be kind' in x for x in seen):
                    break
                await asyncio.sleep(3)
            assert any('house rules: be kind' in x for x in seen), ("Bob's pinned list", seen)
            assert not any('just chatting' in x for x in seen), seen
            await no_errors(alice, bob)
    run(scenario)


# ----------------------------------------------------------------------------------------------- 5. leave
@CHROME
def test_a_member_who_leaves_is_gone_for_the_owner_and_on_his_other_device():
    async def scenario(n):
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob:
            await community(n, alice, bob, 'Leaving')
            await send(bob, 'bob was here')
            await shows(alice, 'bob was here')
            await alice.b.js("document.activeElement&&document.activeElement.blur();true")
            await R.wait(alice.b, "(()=>{const m=document.querySelector('#cc-members');if(!document.querySelector('.cc-members-pane .cc-member')&&m)m.click();"
                                  "return %s.includes(%s)})()" % (MEMBERS, json.dumps(R.pubkey(BOB))), 40, "Bob on Alice's member list",
                         state="({members:%s,state:%s})" % (MEMBERS, STATE))
            # Positive control: Bob's other device, signed in fresh, restores the community from his membership vault.
            loop = asyncio.get_running_loop()
            async with R.Device(n, BOB) as bob2:
                await concord(bob2)
                t0 = loop.time()
                await R.wait(bob2.b, "%s.some(r=>r.name==='Leaving')" % ROOMS, 60, "Bob's second device restores the community")
                restore = loop.time() - t0
            # Bob leaves, from the room's header.
            await bob.b.js("document.querySelector('#cc-leave-shortcut').click();true")
            await press_dialog(bob, 'Leave')
            await R.wait(bob.b, "(window.__toastLog||[]).some(t=>/community left/.test(t))", 60, 'left', state=STATE)
            assert not [r for r in await bob.b.js(ROOMS) if r['name'] == 'Leaving']
            # Alice's member list drops him (his guestbook Leave).
            await R.wait(alice.b, "(()=>{const m=document.querySelector('#cc-members');if(!document.querySelector('.cc-members-pane .cc-member')&&m)m.click();"
                                  "return !!document.querySelector('.cc-members-pane .cc-member') && !%s.includes(%s)})()"
                         % (MEMBERS, json.dumps(R.pubkey(BOB))), 100, "Bob gone from Alice's member list",
                         state="({members:%s,state:%s})" % (MEMBERS, STATE))
            # A fresh device of Bob's does not bring it back.
            async with R.Device(n, BOB) as bob3:
                await concord(bob3)
                await asyncio.sleep(max(20, 3 * restore))     # well past the time the same sweep restored it above
                rooms = await bob3.b.js(ROOMS)
                assert not [r for r in rooms if r['name'] == 'Leaving'], ('the left community came back', rooms)
            await no_errors(alice, bob)
    run(scenario)
