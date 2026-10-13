"""VOTING BAD POST OF THE DAY FROM A PHONE SIGNED IN WITH AN NSEC -- against this node's REAL relay.

Reported 2026-10-12: "voting for bad post of the day says couldn't send vote" (a phone, nsec login). Not
reproduced at the time. The existing test (test_badpost_vote_full_app.py) drives the vote against an in-page
socket that answers OK or refuses on a flag, so it can only show that a refusal is SAID -- never WHY a real relay
refuses one. Here the vote goes to the shipped RelayServer (app/services/nostr_relay/server.py over PosterChanDB,
tests/client/realnode_harness.py), configured the ways a node really runs:

  * a member of this node's web of trust, plain relay              -> the vote lands, "Voted Bad Post…"
  * a member, with "PosterChan clients only" ON (the socket is confined until the client answers NIP-42 AUTH as
    a member -- the shape a phone's WebView origin gets)          -> the vote lands
  * NOT in this node's web of trust (a fresh account on a phone)  -> the relay refuses it
    ("blocked: not in web of trust"), and the phone says "couldn't send the vote -- try again". Trying again can
    never work, so the toast should say why. That is the reproduction; it is XFAIL until the toast carries the
    relay's reason.

Each scenario is a 390x844 phone, signing in by typing the nsec into the login screen.
"""
import asyncio

import pytest

from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
VOTER, AUTHOR = 1, 9
ROW = "document.querySelector('.menu-pop [data-m=badpost]')"


async def vote(n, d, post):
    b = d.b
    await b.js("__PC.switchView('global')")
    card = "'.note[data-id=\"%s\"] .act[data-a=menu]'" % post['id']
    await R.wait(b, "!!document.querySelector(%s)" % card, 40, 'the post on the Nostrverse timeline')
    await b.js("document.querySelector(%s).click()" % card)
    await R.wait(b, "!!" + ROW, 15, "the post's menu offers 'Vote Bad Post of the Day'")
    await b.js(ROW + ".click()")
    await R.wait(b, "(window.__toastLog||[]).some(t=>/Bad Post|vote/i.test(t))", 30, 'a toast about the vote')
    return [t for t in await d.toasts() if 'vote' in t.lower() or 'bad post' in t.lower()]


def run(cfg, members_fn, check):
    async def main():
        voter_pk, author_pk = R.pubkey(VOTER), R.pubkey(AUTHOR)
        with R.Node(cfg=cfg, members=members_fn(voter_pk, author_pk)) as n:
            post = R.sign(AUTHOR, 1, 'pineapple belongs on everything')
            n.home.add([R.sign(AUTHOR, 0, '{"name":"pineapple fan"}'), post])
            async with R.Device(n, VOTER, phone=True) as d:
                said = await vote(n, d, post)
                stored = n.home.query({'kinds': [1985], 'authors': [voter_pk], '#e': [post['id']]})
                await check(said, stored, post)
    asyncio.run(main())


@CHROME
def test_a_member_votes_from_a_phone_and_the_relay_keeps_it():
    async def check(said, stored, post):
        assert stored, ('the vote never reached this node\'s relay', said)
        assert any('Voted Bad Post' in t for t in said), said
    run({'wot_enabled': True}, lambda v, a: [v, a], check)


@CHROME
def test_a_member_votes_with_posterchan_clients_only_on():
    """The socket is confined (no PosterChan Origin/UA from this browser) until the client answers AUTH."""
    async def check(said, stored, post):
        assert stored, ('the vote never reached this node\'s relay', said)
        assert any('Voted Bad Post' in t for t in said), said
    run({'wot_enabled': True, 'posterchan_clients_only': True, 'posterchan_origins': []},
        lambda v, a: [v, a], check)


@CHROME
@pytest.mark.xfail(strict=True, reason="REPORT 3 (2026-10-12), REPRODUCED: a voter outside this node's web of trust "
                                       "is refused by the relay ('blocked: not in web of trust') and the phone says "
                                       "\"couldn't send the vote -- try again\", which can never succeed. Pending: the "
                                       "toast must say the relay's reason (menus.js toggleBadPostVote drops r.msg).")
def test_a_voter_the_relay_refuses_is_told_why():
    async def check(said, stored, post):
        assert not stored                                  # the relay really did refuse it
        assert any('web of trust' in t.lower() or 'member' in t.lower() for t in said), said
        assert not any('try again' in t for t in said), said
    run({'wot_enabled': True}, lambda v, a: [a], check)
