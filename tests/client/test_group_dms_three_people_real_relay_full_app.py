"""GROUP DMs BETWEEN THREE REAL PEOPLE -- three accounts, three shipped clients, this node's REAL relay.

"yes do the group DMs" (2026-10-27). Before this, a NIP-17 message was filed under ONE peer -- the sender, or the
first `p` of our own copy -- so a group (Armada's, keyed by its whole participant set) arrived here as a 1:1 per
member and a reply went to one of them. Every step is what a person does: Alice writes to Bob AND Carol from New
message; each of them sees ONE conversation holding all three, the sender named on the bubble; Bob's reply reaches
Alice's open conversation and Carol's, and nobody grows a stray 1:1.
"""
import asyncio
import base64
import json
import os

from tests.client import realnode_harness as R
from tests.client.test_dms_between_two_accounts_real_relay_full_app import (
    CHROME, THREAD, device, npub, open_messages, reply, shows, tick)

ALICE, BOB, CAROL = 33, 34, 35        # account keys of their own
CFG = {'wot_enabled': True}
A, B, C = R.pubkey(ALICE), R.pubkey(BOB), R.pubkey(CAROL)


def key(*pks):
    return ','.join(sorted(pks))


async def new_group(d, npubs, text):
    b = d.b
    await open_messages(d)
    await b.js("document.querySelector('#dm-new').click();true")
    await R.wait(b, "!!document.querySelector('#dm-to') && !!document.querySelector('#dm-body')", 15, 'the New message dialog')
    await b.js("(()=>{const to=document.querySelector('#dm-to');to.value=%s;to.dispatchEvent(new Event('input',{bubbles:true}));"
               "const t=document.querySelector('#dm-body');t.value=%s;t.dispatchEvent(new Event('input',{bubbles:true}));"
               "document.querySelector('#dm-go').click();return true})()" % (json.dumps(', '.join(npubs)), json.dumps(text)))
    await R.wait(b, "!document.querySelector('#dm-to') && !!document.querySelector('#dm-in')", 30,
                 'the dialog closed onto the group conversation')


async def open_conv(d, conv, seconds=40):
    row = "document.querySelector('.dm-peer[data-peer=\"%s\"]')" % conv
    await R.wait(d.b, '!!' + row, seconds, 'the group conversation is listed (%s)' % conv[:20],
                 state="[...document.querySelectorAll('.dm-peer')].map(r=>r.dataset.peer)")
    await d.b.js(row + '.click();true')
    await R.wait(d.b, "!!document.querySelector('#dm-in') && !!document.querySelector('#dm-msgs')", 20, 'the conversation open')


WHO = "[...document.querySelectorAll('#dm-msgs .bubble.in .b-who')].map(x=>x.dataset.prof)"
ROWS = "[...document.querySelectorAll('.dm-peer')].map(r=>r.dataset.peer)"


@CHROME
def test_a_group_dm_reaches_everyone_as_one_conversation_and_replies_go_to_all():
    async def main():
        with R.Node(cfg=CFG, members=[A, B, C]) as n:
            # Carol is on a phone: the group row and the labelled bubbles at 390 px.
            async with device(n, BOB) as bob, device(n, CAROL, phone=True) as carol, device(n, ALICE) as alice:
                await open_messages(bob)
                await open_messages(carol)
                await new_group(alice, [await npub(alice, BOB), await npub(alice, CAROL)], 'team lunch friday?')
                await shows(alice, [('out', 'team lunch friday?')])
                assert await alice.b.js("document.querySelector('#dm-msgs')&&document.querySelector('.dm-peer-name').innerText.includes('👥 3')"), \
                    'the open conversation is not labelled as a group of three'
                # One wrap per member (and Alice's own copy): Bob and Carol were each delivered one.
                assert n.home.query({'kinds': [1059], '#p': [B]}) and n.home.query({'kinds': [1059], '#p': [C]})

                await open_conv(bob, key(A, C))
                await shows(bob, [('in', 'team lunch friday?')])
                assert await bob.b.js(WHO) == [A], 'the bubble does not name its sender'
                await tick()
                await reply(bob, 'count me in')
                await shows(bob, [('in', 'team lunch friday?'), ('out', 'count me in')])

                # Bob's reply went to BOTH: Alice's open group conversation, and Carol's.
                await shows(alice, [('out', 'team lunch friday?'), ('in', 'count me in')],
                            what="Bob's reply in Alice's group conversation")
                await open_conv(carol, key(A, B))
                await shows(carol, [('in', 'team lunch friday?'), ('in', 'count me in')],
                            what="Carol's group conversation holds both messages")
                assert await carol.b.js(WHO) == [A, B], "Carol cannot tell who wrote which"
                if os.environ.get('PC_SHOTS'):
                    shot = await carol.b.call('Page.captureScreenshot', {'format': 'png'})
                    with open(os.path.join(os.environ['PC_SHOTS'], 'group-dm-phone.png'), 'wb') as fh:
                        fh.write(base64.b64decode(shot['data']))
                    await carol.b.js("document.querySelector('#dm-back').click();true")
                    await asyncio.sleep(0.5)
                    shot = await carol.b.call('Page.captureScreenshot', {'format': 'png'})
                    with open(os.path.join(os.environ['PC_SHOTS'], 'group-dm-list-phone.png'), 'wb') as fh:
                        fh.write(base64.b64decode(shot['data']))

                # Nobody grew a stray 1:1: the only conversation each has is the group.
                assert await bob.b.js(ROWS) == [key(A, C)], await bob.b.js(ROWS)
                assert await carol.b.js(ROWS) == [key(A, B)], await carol.b.js(ROWS)
                assert await alice.b.js(ROWS) == [key(B, C)], await alice.b.js(ROWS)
                for d in (alice, bob, carol):
                    assert not [e for e in await d.b.js('__errors') if 'ResizeObserver' not in e]
    asyncio.run(main())
