"""DIRECT MESSAGES BETWEEN TWO REAL PEOPLE -- two accounts, two shipped clients, this node's REAL relay.

Every other DM test checks one side against a hand-made imitation of the other: scripts/check_user_journeys.py
only asks whether a wrap REACHED the relay, and the DM UI suites answer from a stubbed socket. Neither can see
the bug where the other person cannot read it. Here Alice and Bob are separate keys, each in their own Chrome
profile running the shipped client, talking through the shipped RelayServer over PosterChanDB (NIP-42 AUTH, the
WoT gate -- tests/client/realnode_harness.py), with the instance's Blossom for attachments. Every step is what a
person does: New -> npub -> Send, open the row, type and press Send, and every assertion is what the OTHER
person's screen shows.

  live        Alice writes to Bob by npub; Bob, already on Messages, sees the conversation and the decrypted
              text with no reload; his reply reaches Alice's open conversation live.
  self copy   Alice's second device (fresh profile, same key) shows both messages, her own included.
  offline     Bob has no browser open when Alice writes; signing in later on a fresh device shows the whole
              history decrypted, in order, each bubble on the right side.
  alert       Bob is on another screen: the Messages badge counts it, an in-app toast names Alice, and the
              page raises an OS notification (window.Notification, observed).
  attachment  Alice attaches a picture with the lock on (encrypted to the conversation, uploaded to Blossom);
              Bob's client fetches and decrypts it to the very bytes Alice picked.
"""
import asyncio
import base64
import hashlib
import json

import pytest

from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
ALICE, BOB = 23, 24            # account keys -- not 1 / 7 / 9 (other suites' accounts)
CFG = {'wot_enabled': True}    # a node as it runs: both are members of its web of trust

# The page's OS-notification path, observed: every `new Notification(...)` the client makes is recorded (and
# still made -- permission is granted to the origin below).
OBSERVE_OS = r'''
window.__osNotes=[];
(()=>{const N=window.Notification;if(!N)return;
  class Seen extends N{constructor(t,o){super(t,o);try{__osNotes.push({title:String(t),body:String((o&&o.body)||''),tag:String((o&&o.tag)||'')})}catch(_){}}}
  window.Notification=Seen;})();
'''

THREAD = r"""[...document.querySelectorAll('#dm-msgs .bubble')].map(x=>[x.classList.contains('out')?'out':'in',
  ((x.querySelector('.b-txt')||{}).innerText||'').trim()])"""


def device(n, fill, **kw):
    return R.Device(n, fill, pre_init=OBSERVE_OS, **kw)


async def npub(d, fill):
    return await d.b.js('NostrTools.nip19.npubEncode(%s)' % json.dumps(R.pubkey(fill)))


async def open_messages(d):
    await d.b.js("__PC.switchView('messages');true")
    await R.wait(d.b, "!!document.querySelector('#dm-list #dm-rows')", 30, 'the Messages screen')


async def new_dm(d, to_npub, text):
    """Messages -> New -> the npub -> the text -> Send, as a person does."""
    b = d.b
    await open_messages(d)
    await b.js("document.querySelector('#dm-new').click();true")
    await R.wait(b, "!!document.querySelector('#dm-to') && !!document.querySelector('#dm-body')", 15, 'the New message dialog')
    await b.js("(()=>{const to=document.querySelector('#dm-to');to.value=%s;to.dispatchEvent(new Event('input',{bubbles:true}));"
               "const t=document.querySelector('#dm-body');t.value=%s;t.dispatchEvent(new Event('input',{bubbles:true}));"
               "document.querySelector('#dm-go').click();return true})()" % (json.dumps(to_npub), json.dumps(text)))
    await R.wait(b, "!document.querySelector('#dm-to') && !!document.querySelector('#dm-in')", 30,
                 'the dialog closed onto the conversation')


async def open_peer(d, fill, seconds=40):
    pk = R.pubkey(fill)
    row = "document.querySelector('.dm-peer[data-peer=\"%s\"]')" % pk
    await R.wait(d.b, '!!' + row, seconds, 'the conversation is listed')
    await d.b.js(row + '.click();true')
    await R.wait(d.b, "!!document.querySelector('#dm-in') && !!document.querySelector('#dm-msgs')", 20, 'the conversation open')


async def reply(d, text):
    await d.b.js("(()=>{const i=document.querySelector('#dm-in');i.focus();i.value=%s;"
                 "i.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#dm-send').click();return true})()"
                 % json.dumps(text))


async def shows(d, want, seconds=40, what=None):
    """The open conversation's bubbles are exactly `want` ([('in'|'out', text), ...]) -- order and sides."""
    want = [list(x) for x in want]
    await R.wait(d.b, 'JSON.stringify(%s)===%s' % (THREAD, json.dumps(json.dumps(want, separators=(',', ':')))),
                 seconds, what or ('the conversation reads %r' % want),
                 state="({thread:%s,toasts:(window.__toastLog||[]).slice(-6),errors:(window.__errors||[]).slice(-6)})" % THREAD)


async def tick():
    # The bubbles are ordered by the rumor's created_at, in seconds; a person never sends two in one second.
    await asyncio.sleep(1.2)


def run(scenario):
    async def main():
        with R.Node(cfg=CFG, members=[R.pubkey(ALICE), R.pubkey(BOB)]) as n:
            await scenario(n)
    asyncio.run(main())


def wraps_for(n, fill):
    return n.home.query({'kinds': [1059], '#p': [R.pubkey(fill)]})


# ------------------------------------------------------------------------------------------------ live, both ways
@CHROME
def test_a_dm_and_its_reply_arrive_live_and_a_second_device_has_both():
    async def scenario(n):
        async with device(n, BOB) as bob, device(n, ALICE) as alice, device(n, ALICE) as alice_desk:
            await open_messages(bob)
            await open_messages(alice_desk)        # Alice's other device is open too, on Messages
            await new_dm(alice, await npub(alice, BOB), 'hi Bob, lunch at noon?')
            await shows(alice, [('out', 'hi Bob, lunch at noon?')])
            # Bob did nothing: the row and the decrypted line arrive on the screen he is looking at.
            await R.wait(bob.b, "[...document.querySelectorAll('.dm-peer[data-peer=\"%s\"] .dm-prev')]"
                                ".some(x=>x.innerText.includes('lunch at noon'))" % R.pubkey(ALICE), 40,
                         "Bob's open Messages list shows Alice's message")
            await open_peer(bob, ALICE)
            await shows(bob, [('in', 'hi Bob, lunch at noon?')])
            await tick()
            await reply(bob, 'noon works, see you there')
            await shows(bob, [('in', 'hi Bob, lunch at noon?'), ('out', 'noon works, see you there')])
            # Alice's conversation is still open on Bob: his reply lands in it, no reload.
            await shows(alice, [('out', 'hi Bob, lunch at noon?'), ('in', 'noon works, see you there')],
                        what="Bob's reply on Alice's open conversation")
            # Her other device, open the whole time, got her own message (the self copy) and Bob's reply live.
            await open_peer(alice_desk, BOB)
            await shows(alice_desk, [('out', 'hi Bob, lunch at noon?'), ('in', 'noon works, see you there')],
                        what="Alice's other open device shows the exchange")
            assert await alice_desk.b.js("document.querySelector('#dm-badge').classList.contains('hidden')"), \
                'her own message, sent from her other device, badged as unread'
            assert not [e for e in await alice.b.js('__errors') if 'ResizeObserver' not in e]
            assert not [e for e in await bob.b.js('__errors') if 'ResizeObserver' not in e]
            # Alice's OTHER device -- nothing cached, same key: her own message comes from the self-copy wrap.
            async with device(n, ALICE) as alice2:
                await open_messages(alice2)
                await open_peer(alice2, BOB)
                await shows(alice2, [('out', 'hi Bob, lunch at noon?'), ('in', 'noon works, see you there')],
                            what="Alice's second device has both messages, hers on the right")
    run(scenario)


@CHROME
def test_on_two_phones_the_open_conversation_updates_live():
    """The phone layout: the conversation is a full-screen overlay over the list, and it must stay open and
    take the arriving message in place (a list rebuild used to drop the overlay)."""
    async def scenario(n):
        async with device(n, BOB, phone=True) as bob, device(n, ALICE, phone=True) as alice:
            await new_dm(alice, await npub(alice, BOB), 'phone hello')
            await open_messages(bob)
            await open_peer(bob, ALICE)
            await shows(bob, [('in', 'phone hello')])
            await tick()
            await reply(alice, 'are you on the bus?')
            await shows(bob, [('in', 'phone hello'), ('in', 'are you on the bus?')],
                        what="Bob's open conversation takes Alice's next message live, on a phone")
            assert await bob.b.js("document.querySelector('#dm-list').classList.contains('has-active') && "
                                  "document.querySelector('#dm-in').getClientRects().length>0"), 'the conversation closed'
            await tick()
            await reply(bob, 'yes, 5 min')
            await shows(alice, [('out', 'phone hello'), ('out', 'are you on the bus?'), ('in', 'yes, 5 min')])
    run(scenario)


# ------------------------------------------------------------------------------------------------ Bob was offline
@CHROME
def test_bob_offline_reads_the_whole_history_on_a_fresh_device():
    async def scenario(n):
        async with device(n, ALICE) as alice:
            async with device(n, BOB) as bob:
                await new_dm(alice, await npub(alice, BOB), 'first: are you around this week?')
                await open_messages(bob)
                await open_peer(bob, ALICE)
                await shows(bob, [('in', 'first: are you around this week?')])
                await tick()
                await reply(bob, 'second: yes, from Thursday')
                await shows(alice, [('out', 'first: are you around this week?'), ('in', 'second: yes, from Thursday')])
            # Bob's browser is gone. Alice writes twice more.
            await tick()
            await reply(alice, 'third: Thursday it is')
            await tick()
            await reply(alice, 'fourth: bring the charger')
            await shows(alice, [('out', 'first: are you around this week?'), ('in', 'second: yes, from Thursday'),
                                ('out', 'third: Thursday it is'), ('out', 'fourth: bring the charger')])
            assert len(wraps_for(n, BOB)) >= 3, 'the relay does not hold the wraps to Bob'
        async with device(n, BOB) as bob2:
            await open_messages(bob2)
            await open_peer(bob2, ALICE)
            await shows(bob2, [('in', 'first: are you around this week?'), ('out', 'second: yes, from Thursday'),
                               ('in', 'third: Thursday it is'), ('in', 'fourth: bring the charger')],
                        what="Bob's fresh device: the whole history, in order, each on its sender's side")
    run(scenario)


# ------------------------------------------------------------------------------------------------ the alert
@CHROME
def test_a_dm_while_bob_is_elsewhere_badges_toasts_and_notifies():
    async def scenario(n):
        async with device(n, BOB) as bob, device(n, ALICE) as alice:
            await bob.b.call('Browser.grantPermissions', {'permissions': ['notifications'], 'origin': n.origin})
            assert await bob.b.js("Notification.permission") == 'granted'
            # Bob signed in and never opened Messages: he is on his timeline. The inbox is watched from login.
            await R.wait(bob.b, "!document.querySelector('#dm-list')", 15, 'Bob is on another screen')
            await asyncio.sleep(4)
            assert await bob.b.js("document.querySelector('#dm-badge').classList.contains('hidden')"), 'a badge before any DM'
            await new_dm(alice, await npub(alice, BOB), 'ping from Alice')
            await R.wait(bob.b, "(()=>{const b=document.querySelector('#dm-badge');"
                                "return !!b && !b.classList.contains('hidden') && b.textContent.trim()==='1'})()", 40,
                         "Bob's Messages badge counts the new DM")
            await R.wait(bob.b, "(window.__toastLog||[]).some(t=>/sent you a message/.test(t))", 20,
                         'an in-app toast says a message arrived')
            await R.wait(bob.b, "__osNotes.some(x=>x.tag==='pc-dm' && /sent you a DM/.test(x.body))", 20,
                         'the page raised an OS notification for the DM')
            # The alert never leaks the message itself.
            assert not await bob.b.js("__osNotes.some(x=>/ping from Alice/.test(x.body+x.title))")
            # And reading it clears the badge.
            await open_messages(bob)
            await open_peer(bob, ALICE)
            await shows(bob, [('in', 'ping from Alice')])
            await R.wait(bob.b, "document.querySelector('#dm-badge').classList.contains('hidden')", 15,
                         'the badge clears once read')
    run(scenario)


# ------------------------------------------------------------------------------------------------ an attachment
ATTACH = r"""(async b64=>{const bin=Uint8Array.from(atob(b64),c=>c.charCodeAt(0));
  const dt=new DataTransfer();dt.items.add(new File([bin],'beach.png',{type:'image/png'}));
  const inp=document.querySelector('#dm-file');inp.files=dt.files;inp.dispatchEvent(new Event('change',{bubbles:true}));return true})"""
SHOWN_SHA = r"""(async()=>{const im=[...document.querySelectorAll('#dm-msgs .bubble.in img')].find(i=>/^blob:/.test(i.src)&&i.naturalWidth>0);
  if(!im)return '';const buf=await (await fetch(im.src)).arrayBuffer();
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256',buf))].map(x=>x.toString(16).padStart(2,'0')).join('')})()"""


@CHROME
def test_an_encrypted_attachment_reaches_bob_and_opens():
    picture = R.png((40, 160, 90), 16)
    async def scenario(n):
        async with device(n, ALICE) as alice:
            await new_dm(alice, await npub(alice, BOB), 'photo coming')
            # The lock: attachments encrypted to the conversation (the thread's own 🔒 button).
            await alice.b.js("ClientSettings.set('dmEncryptAtts', true);true")
            await alice.b.js(ATTACH + '(%s)' % json.dumps(base64.b64encode(picture).decode()))
            await R.wait(alice.b, "/https?:\\/\\/\\S+#/.test(document.querySelector('#dm-in').value)", 40,
                         'the encrypted upload is attached to the message')
            await alice.b.js("document.querySelector('#dm-send').click();true")
            await R.wait(alice.b, "document.querySelectorAll('#dm-msgs .bubble.out').length===2", 30, 'sent')
            stored = list(R.H.STATE['blobs'].values())
            in_clear = picture in stored
            assert stored and not in_clear, 'the picture reached Blossom in the clear'
        async with device(n, BOB) as bob:
            await open_messages(bob)
            await open_peer(bob, ALICE)
            await R.wait(bob.b, "!!document.querySelector('#dm-msgs .bubble.in img[src^=\"blob:\"]')", 40,
                         'Bob sees the decrypted picture',
                         state="({thread:%s,html:(document.querySelector('#dm-msgs')||{}).innerHTML})" % THREAD)
            got = ''
            for _ in range(50):
                got = await bob.b.js(SHOWN_SHA)
                if got:
                    break
                await asyncio.sleep(.2)
            assert got == hashlib.sha256(picture).hexdigest(), 'Bob decrypted something other than what Alice sent'
    run(scenario)
