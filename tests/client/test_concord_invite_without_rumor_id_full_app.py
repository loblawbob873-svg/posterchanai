"""A CONCORD INVITATION FROM ANOTHER NIP-59 CLIENT (no rumor id) ARRIVES, SHOWS AND OPENS -- in the real app.

Reported 2026-10-13 from the desktop: "The invitation has an invalid signed rumor: rumor has no id", as toasts at
startup and on every open. Other NIP-59 producers leave the unsigned rumor's `id` out; the seal's signature is what
authenticates it. Fixed by e27dbeb78 (cord-direct-invites.js computes a missing id; a present one must still match).

The regression case that shipped with the fix (tests/client/test_cord_direct_invites.mjs) calls the module's
`open()` directly under node. This is the user's path end to end: the invitation is a real
gift wrap (kind 1059, `k` 3313, NIP-44, a sealed rumor WITHOUT `id`) published to this node's real relay by
another account; the member's phone starts, its inbox picks the wrap up, Concord → Invites lists it, and Review
opens the community it names. Nothing may toast "invalid signed rumor".

Fail-first: PC_JOURNEY_OVERRIDE=/static/js/client/cord-direct-invites.js=<e27dbeb78^ copy> -> "No pending
invitations" (the inbox refused the wrap) and the open throws "rumor has no id".
"""
import asyncio
import json

import pytest

from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
ME, SENDER, EPHEMERAL = 1, 7, 8

# An independent producer: plain NIP-59 with NostrTools, never the app's own invitation builder.
WIRE = r'''(async(senderFill,ephFill,name)=>{
  const NT=NostrTools,sk=n=>new Uint8Array(32).fill(n),sender=sk(senderFill),eph=sk(ephFill);
  const senderPk=NT.getPublicKey(sender),me=__PC.me().pubkey;
  const hex=b=>[...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join('');
  const unhex=h=>Uint8Array.from(h.match(/../g).map(x=>parseInt(x,16)));
  const salt='44'.repeat(32);
  const cid=hex(await crypto.subtle.digest('SHA-256',new Uint8Array([...new TextEncoder().encode('concord/community'),...unhex(senderPk),...unhex(salt)])));
  const bundle={community_id:cid,owner:senderPk,owner_salt:salt,community_root:'55'.repeat(32),root_epoch:1,channels:[],
    relays:['wss://example.invalid'],name,expires_at:Date.now()+3600000};
  const enc=(text,skx,pk)=>NT.nip44.encrypt(text,NT.nip44.getConversationKey(skx,pk));
  const now=Math.floor(Date.now()/1000);
  const rumor={kind:3313,pubkey:senderPk,created_at:now,tags:[],content:JSON.stringify(bundle)};   // NO id
  const seal=NT.finalizeEvent({kind:13,created_at:now,tags:[],content:enc(JSON.stringify(rumor),sender,me)},sender);
  return NT.finalizeEvent({kind:1059,created_at:now,tags:[['p',me],['k','3313']],content:enc(JSON.stringify(seal),eph,me)},eph);
})'''


@CHROME
def test_an_invitation_without_a_rumor_id_is_listed_and_opens():
    async def main():
        with R.Node() as n:
            async with R.Device(n, ME, phone=True) as d:
                b = d.b
                wrap = await b.js(WIRE + '(%d,%d,%s)' % (SENDER, EPHEMERAL, json.dumps('Direct fixture')))
                ok = n.home.publish(wrap)
                assert ok[2] is True, ok
                # The person's NEXT start: the inbox reads the wrap from this node's relay.
                await b.call('Page.reload')
                await R.wait(b, '!!window.__PC && !!__PC.me && !!__PC.me() && Relay.status==="ok"', 60, 'signed in again')
                await R.wait(b, "(JSON.parse(localStorage.getItem('pc.concord.direct.v1.'+__PC.me().pubkey)||'{}').wraps||[]).length>0",
                             30, 'the invitation kept in the Concord inbox')
                await b.js("__PC.switchMessagesTab('concord')")
                await R.wait(b, "!!document.querySelector('#cc-direct-inbox')", 40, 'Concord with its Invites control')
                await b.js("document.querySelector('#cc-direct-inbox').click()")
                await R.wait(b, "[...document.querySelectorAll('button')].some(x=>x.textContent==='Review invitation')",
                             20, "Invites lists the invitation ('Review invitation')")
                await b.js("[...document.querySelectorAll('button')].find(x=>x.textContent==='Review invitation').click()")
                await R.wait(b, "document.body.innerText.includes('Direct fixture')", 20,
                             'the invitation opens on the community it names')
                bad = [t for t in await d.toasts() if 'invalid' in t.lower() or 'rumor' in t.lower()]
                assert not bad, bad
    asyncio.run(main())
