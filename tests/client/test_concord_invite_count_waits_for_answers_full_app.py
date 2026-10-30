"""COMMUNITIES DOES NOT COUNT AN INVITATION AS PENDING BEFORE IT KNOWS WHICH ONES WERE ANSWERED -- in the real app.

Reported 2026-10-30 on poster.place: "I clicked on communities and I see that I have 26 invitations and no communities
were loaded" ... "communities eventually loaded without reloading the page, just took a while". Every invitation the
account had ever answered read as new: the card counted the parked invitations before the synced document that
records the answers (d=pcai:concord-read) had come back, and before the membership vault said which communities are
already joined.

Real node, real relay, real gift wraps from another account: two invitations, one of them already answered (recorded
in the account's read document, as another device would), and that document's read held back four seconds. Sampled
the whole time: the card never says 2; once the answers are in it says 1 -- the one that really is new. Fail-first:
the shipped code before this change drew "You have 2 invitations" for the whole wait.
"""
import asyncio
import json

import pytest

from tests.client import realnode_harness as R
from tests.client.test_concord_invite_without_rumor_id_full_app import WIRE

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
ME, SENDER = 1, 7

# The account's own read document saying `answered` was answered (what noteInviteAnswered writes on another device).
READ_DOC = r'''(async(answered)=>{const me=__PC.me().pubkey;
  const content=await __PC.nip44enc(me,JSON.stringify({v:1,marks:{},invites:[answered],stars:{}}));
  const ev=await __PC.signTemplate({kind:30078,created_at:Math.floor(Date.now()/1000),content,tags:[['d','pcai:concord-read']],pubkey:me});
  const r=await Relay.publish(ev);return {id:ev.id,ok:!!(r&&(r.ok||r===true))};})'''

# Hold back any read of that document, as a slow start does.
SLOW_READ_DOC = r'''(()=>{const q=__PC.relayQuery;window.__heldReads=0;
  __PC.relayQuery=async(f,...r)=>{if(JSON.stringify(f).includes('pcai:concord-read')){__heldReads++;await new Promise(z=>setTimeout(z,4000));}return q(f,...r);};
  window.__cards=[];const t0=performance.now();
  window.__sampler=setInterval(()=>{const c=document.querySelector('.cc-invite-card b');__cards.push([Math.round(performance.now()-t0),c?c.textContent:'']);},150);
  return true;})()'''


@CHROME
def test_answered_invitations_are_not_counted_while_the_answers_load():
    async def main():
        with R.Node() as n:
            async with R.Device(n, ME, phone=True) as d:
                b = d.b
                old = await b.js(WIRE + '(%d,%d,%s)' % (SENDER, 8, json.dumps('Answered before')))
                new = await b.js(WIRE + '(%d,%d,%s)' % (SENDER + 2, 9, json.dumps('Really new')))
                assert n.home.publish(old)[2] is True and n.home.publish(new)[2] is True
                await R.wait(b, 'Relay.status==="ok"', 30, 'signed in to the relay')
                doc = await b.js(READ_DOC + '(%s)' % json.dumps(old['id']))
                assert doc['ok'], ('the read document was not stored', doc)
                await b.call('Page.reload')
                await R.wait(b, '!!window.__PC && !!__PC.me && !!__PC.me() && Relay.status==="ok"', 60, 'signed in again')
                await R.wait(b, "(JSON.parse(localStorage.getItem('pc.concord.direct.v1.'+__PC.me().pubkey)||'{}').wraps||[]).length>=2",
                             30, 'both invitations kept in the Concord inbox')
                await b.js(SLOW_READ_DOC)
                await b.js("__PC.switchMessagesTab('concord')")
                await R.wait(b, "window.__heldReads>0", 20, 'Concord asked for the answers')
                await R.wait(b, "!!document.querySelector('.cc-invite-card b')", 30, 'the invitation card')
                await asyncio.sleep(1.5)
                cards = await b.js("clearInterval(__sampler), __cards")
                said = sorted({c for _, c in cards if c})
                assert 'You have 2 invitations' not in said, ('an answered invitation was counted as new while the answers loaded', cards)
                assert cards[-1][1] == 'You have an invitation', ('the really new one must still be shown', cards[-1], said)
    asyncio.run(main())
