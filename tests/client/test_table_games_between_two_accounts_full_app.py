"""TWO PEOPLE AT A CARD TABLE -- Blackjack and Texas Hold'em, two accounts, two shipped clients, this node's REAL
relay and the REAL dealer bot (tests/client/gamebot_harness.py runs botframework/main.py as the bot manager does).

Both games are multiplayer: "seat a table with friends" posts one public note tagging the bot and the friends, the
bot deals a persistent table (a kind-30388 document both clients read), and every action is a private kind-30388
command the client signs (the "command channel"). The cards are random, so the tests assert what must hold for ANY
deal: both screens show the same table, each action one player presses reaches the other's screen, the round ends,
and both screens agree on the outcome.

  blackjack  Alice seats Bob and deals; both see both hands and the dealer's up card; each stands (a natural is
             already locked in); the dealer plays; each screen's own banner matches what the OTHER screen shows for
             that seat, the dealer's final hand is the same on both, and Bob's result and chip count are still
             there on a FRESH device that signs in later.
  holdem     Alice seats Bob and deals heads-up; each sees only their own hole cards (NIP-44, decrypted in the
             client); they check/call every street; after every action both screens show the same pot and board;
             at the showdown both screens report the same result, the winner's screen says so, and the chip
             counts agree.
"""
import asyncio
import json
import re

import pytest

from tests.client import realnode_harness as R
from tests.client.gamebot_harness import BOT_PK, Bots

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ("blackjack", "holdem")   # scripts/feature_coverage.py
ALICE, BOB = 43, 44                        # account keys no other suite uses
CFG = {'wot_enabled': True}


def run(game, scenario):
    async def main():
        with R.Node(cfg=CFG, members=[R.pubkey(ALICE), R.pubkey(BOB), BOT_PK]) as n, Bots(n, game) as bots:
            await scenario(n, bots)
    asyncio.run(main())


async def name(d, label):
    await d.b.js("__PC.publish(0, JSON.stringify({name:%s}), []).then(()=>1)" % json.dumps(label))


async def open_game(d, view, list_sel):
    await d.b.js("__PC.switchView(%s);true" % json.dumps(view))
    await R.wait(d.b, "!!document.querySelector(%s) && !document.querySelector(%s+' .spinner')"
                 % (json.dumps(list_sel), json.dumps(list_sel)), 30, 'the %s screen loaded its tables' % view)


async def seat_and_deal(d, inp, res, deal, friend_fill):
    """Seat the friend by npub, press Deal table -- as a person does."""
    npub = await d.b.js('NostrTools.nip19.npubEncode(%s)' % json.dumps(R.pubkey(friend_fill)))
    await d.b.js("(()=>{const i=document.querySelector(%s);i.value=%s;i.dispatchEvent(new Event('input',{bubbles:true}));"
                 "return true})()" % (json.dumps(inp), json.dumps(npub)))
    await R.wait(d.b, "!!document.querySelector('%s [data-add]')" % res, 20, 'the friend is offered a seat')
    await d.b.js("document.querySelector('%s [data-add]').click();true" % res)
    await R.wait(d.b, "getComputedStyle(document.querySelector('%s')).display!=='none'" % deal, 10, 'Deal table shows')
    await d.b.js("document.querySelector('%s').click();true" % deal)


CARD = "document.querySelector('.chess-games .chess-game-card')"


def q(sel, all_=False):
    return ("[...(%s?%s.querySelectorAll(%s):[])].map(x=>x.textContent.trim())" % (CARD, CARD, json.dumps(sel)) if all_
            else "(((%s&&%s.querySelector(%s))||{}).textContent||'').trim()" % (CARD, CARD, json.dumps(sel)))


# ------------------------------------------------------------------------------------------------------ blackjack
BJ = ("({badge:%s,banner:%s,mine:%s,myval:%s,chips:%s,dealer:%s,dealerCards:%s,seats:%s,controls:%s})"
      % (q('.cc-badge'), q('.chess-result'), q('.pk-mycards .pk-card', True), q('.bj-myval'), q('.pk-mychipline'),
         q('.pk-felt .pk-street'), q('.pk-felt .pk-board .pk-card', True), q('.pk-seats .pk-seat', True),
         "[...document.querySelectorAll('.chess-game-card .pk-controls button')].map(b=>b.textContent.trim())"))


def _net(banner):
    """A seat's net from its own banner: 'You won 25!' -> 25, 'You lost 25' -> -25, 'Push' -> 0."""
    if 'Push' in banner:
        return 0
    m = re.search(r'You (won|lost) (\d+)', banner)
    assert m, banner
    return int(m.group(2)) * (1 if m.group(1) == 'won' else -1)


def _seat_net(row):
    """What the OTHER screen shows for a seat: '... WIN +25 ...', 'LOSE -25', 'BUST...', 'PUSH'."""
    m = re.search(r'\b(WIN|BLACKJACK|LOSE|PUSH|BUST)\b ?([+-]\d+)?', row)
    assert m, row
    return int(m.group(2)) if m.group(2) else 0


@CHROME
def test_blackjack_table_with_a_friend_plays_a_round_and_both_screens_agree():
    async def scenario(n, bots):
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob:
            await name(alice, 'alice')
            await name(bob, 'bob')
            await open_game(alice, 'blackjack', '#bj-games')
            await open_game(bob, 'blackjack', '#bj-games')
            await seat_and_deal(alice, '#bj-inv', '#bj-inv-res', '#bj-deal', BOB)
            # Bob did nothing: the dealt table arrives on his screen, two cards each, dealer's up card + one down.
            for d, who in ((alice, 'Alice'), (bob, 'Bob')):
                await R.wait(d.b, "(%s).mine.length===2 && (%s).seats.length===1 && (%s).dealerCards.length===2"
                             % (BJ, BJ, BJ), 40, '%s sees the dealt table' % who, state=BJ)
            a0, b0 = await alice.b.js(BJ), await bob.b.js(BJ)
            assert a0['dealerCards'][0] == b0['dealerCards'][0] != '?' and a0['dealerCards'][1] == '?', (a0, b0)
            # Each screen shows the OTHER player's real hand: Alice's two cards are what Bob sees in her seat.
            assert all(c in b0['seats'][0] for c in a0['mine']), (a0['mine'], b0['seats'])
            assert all(c in a0['seats'][0] for c in b0['mine']), (b0['mine'], a0['seats'])
            # Each stands (a natural 21 is already locked in and offers no Stand).
            for d in (alice, bob):
                if 'Stand' in (await d.b.js(BJ))['controls']:
                    await d.b.js("document.querySelector('.chess-game-card .bj-stand').click();true")
                    await R.wait(d.b, "!(%s).controls.includes('Stand')" % BJ, 40, 'the stand is taken', state=BJ)
            for d, who in ((alice, 'Alice'), (bob, 'Bob')):
                await R.wait(d.b, "/Push|You won|You lost/.test((%s).banner)" % BJ, 40, '%s sees the round settled' % who,
                             state=BJ)
            a, b = await alice.b.js(BJ), await bob.b.js(BJ)
            assert a['dealerCards'] == b['dealerCards'] and '?' not in a['dealerCards'], (a, b)
            assert a['dealer'] == b['dealer'] and re.search(r'DEALER · \d+', a['dealer']), (a['dealer'], b['dealer'])
            # Both agree: my banner is what the other screen shows for my seat.
            assert _net(a['banner']) == _seat_net(b['seats'][0]), (a['banner'], b['seats'])
            assert _net(b['banner']) == _seat_net(a['seats'][0]), (b['banner'], a['seats'])
            bob_chips = b['chips']
        # Bob signs in on a fresh device later: the settled table, his result and chips are still there.
        async with R.Device(n, BOB) as bob2:
            await open_game(bob2, 'blackjack', '#bj-games')
            await R.wait(bob2.b, "(%s).chips===%s && (%s).banner===%s" % (BJ, json.dumps(bob_chips), BJ, json.dumps(b['banner'])),
                         40, "Bob's result and chips on a fresh device", state=BJ)
    run('blackjack', scenario)


# --------------------------------------------------------------------------------------------------------- holdem
HE = ("({badge:%s,pot:%s,street:%s,board:%s,mine:%s,chips:%s,seat:%s,last:%s,controls:%s,hand:%s,toasts:(window.__toastLog||[]).slice(-4)})"
      % (q('.cc-badge'), q('.pk-pot b'), q('.pk-felt .pk-street'), q('.pk-felt .pk-board .pk-card:not(.empty)', True),
         q('.pk-mycards .pk-card', True), q('.pk-mychipline b'), q('.pk-seats .pk-seat .pk-stk'), q('.pk-last'),
         "[...document.querySelectorAll('.chess-game-card .pk-controls button')].map(b=>b.className)",
         q('.cc-meta .muted')))


async def he(d):
    return await d.b.js(HE)


async def whose_turn(alice, bob, seconds=40):
    """'Alice' / 'Bob' when one screen offers its player the move, None once the hand is settled on both."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        a, b = await he(alice), await he(bob)
        if a['last'] and b['last']:
            return None
        if a['badge'] == 'Your move':
            return 'Alice'
        if b['badge'] == 'Your move':
            return 'Bob'
        await asyncio.sleep(.2)
    raise R.StepFailed({'expected': 'somebody to act', 'seen': [await he(alice), await he(bob)]})


async def agree(x, y, what, seconds=40):
    """Both screens show the same pot, board and hand (an action reached both), or both show the hand settled."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        a, b = await he(x), await he(y)
        if (a['last'] and b['last']) or ([a['pot'], a['board'], a['hand']] == [b['pot'], b['board'], b['hand']]
                                         and a['badge'] != b['badge']):
            return
        await asyncio.sleep(.2)
    raise R.StepFailed({'expected': what, 'seen': [await he(x), await he(y)]})


@CHROME
def test_holdem_table_with_a_friend_plays_to_showdown_and_both_screens_agree():
    async def scenario(n, bots):
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob:
            await name(alice, 'alice')
            await name(bob, 'bob')
            await open_game(alice, 'holdem', '#hm-games')
            await open_game(bob, 'holdem', '#hm-games')
            await seat_and_deal(alice, '#hm-inv', '#hm-inv-res', '#hm-deal', BOB)
            for d, who in ((alice, 'Alice'), (bob, 'Bob')):
                await R.wait(d.b, "(%s).mine.length===2 && /hand #1/.test((%s).hand)" % (HE, HE), 40,
                             '%s sees the table and her/his own two hole cards' % who, state=HE)
            a0, b0 = await he(alice), await he(bob)
            assert not set(a0['mine']) & set(b0['mine']), ('two players were shown the same hole cards', a0, b0)
            assert a0['pot'] == b0['pot'], (a0['pot'], b0['pot'])
            players = {'Alice': alice, 'Bob': bob}
            for _ in range(12):                          # heads-up check/call down: at most 2 actions per street
                turn = await whose_turn(alice, bob)
                if turn is None:
                    break                                # the hand is settled
                who = players[turn]
                other = bob if who is alice else alice
                before = await he(who)
                btn = '.pk-check' if any('pk-check' in c for c in before['controls']) else '.pk-call'
                await who.b.js("document.querySelector('.chess-game-card %s').click();true" % btn)
                # Taken = the table moved on: the pot, the board, whose turn or the hand changed. (Not "it is no
                # longer my move": after the big blind checks pre-flop it is the big blind's move again, on the flop.)
                key = "JSON.stringify([(%s).pot,(%s).board,(%s).badge,(%s).hand])" % (HE, HE, HE, HE)
                await R.wait(who.b, "%s!==%s || !!(%s).last" % (key, json.dumps(json.dumps(
                    [before['pot'], before['board'], before['badge'], before['hand']], separators=(',', ':'),
                    ensure_ascii=False)), HE), 40, '%s %s is taken' % (turn, btn[4:]), state=HE)
                await agree(who, other, '%s %s reaches the other screen' % (turn, btn[4:]))
            for d, who in ((alice, 'Alice'), (bob, 'Bob')):
                await R.wait(d.b, "/hand #1 done/i.test((%s).last)" % HE, 40, '%s sees the hand settled' % who, state=HE)
            a, b = await he(alice), await he(bob)
            sa, sb = a['last'].split('·', 1)[1].strip(), b['last'].split('·', 1)[1].strip()
            assert sa == sb and 'wins' in sa, (a['last'], b['last'])
            # the winner named in the shared summary is the one whose screen says "You won"
            for label, s in (('alice', a), ('bob', b)):
                assert (label in sa) == ('You won' in s['last']), (label, s['last'], sa)
            # chips agree: what I hold is what the other screen shows at my seat (start of hand #2, blinds posted)
            await R.wait(alice.b, "/hand #2/.test((%s).hand)" % HE, 40, 'hand #2 dealt', state=HE)
            await R.wait(bob.b, "/hand #2/.test((%s).hand)" % HE, 40, 'hand #2 dealt', state=HE)
            a, b = await he(alice), await he(bob)
            assert a['chips'] == b['seat'].split()[0] and b['chips'] == a['seat'].split()[0], (a, b)
    run('holdem', scenario)
