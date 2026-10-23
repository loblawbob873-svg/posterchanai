"""TWO PEOPLE PLAY A BOARD GAME TO THE END -- two accounts, two shipped clients, this node's REAL relay, the REAL
game bot (tests/client/gamebot_harness.py runs botframework/main.py exactly as the bot manager does).

Until this file every game screen had a browser test that it OPENS and that its start button puts a note on the
wire, and the bots had tests that they referee a game fed by hand-signed DMs (tests/test_game_bots_play.py). Nothing
crossed the seam: a challenge pressed in one person's client, the bot starting the game, the OTHER person's client
showing it, each move pressed on a board (sent by the shipped code as a NIP-17 DM), the bot applying it, and the
first person's board redrawing with the opponent's piece -- to a finished game whose result both screens agree on.

  ttt       Alice challenges Bob by npub; Bob (X, the invited side) accepts by moving; five moves alternate across
            the two clients, each seeing the other's mark appear; Bob wins on the top row. Bob's card says he won,
            Alice's that she lost, and both name the same result.
  connect4  the same shape with columns: Bob drops four in column 1 while Alice plays column 2.
  (chess: test_chess_between_two_accounts_full_app.py, same harness)
  hangman   Alice challenges Bob to guess; Alice SETS the word from her own screen; Bob guesses it letter by
            letter from the keyboard; both see the solved word and agree who won.
"""
import asyncio
import json

import pytest

from tests.client import realnode_harness as R
from tests.client.gamebot_harness import BOT_PK, Bots

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ("ttt", "connect4", "hangman")   # scripts/feature_coverage.py
ALICE, BOB = 41, 42                        # account keys no other suite uses
CFG = {'wot_enabled': True}                # a node as it runs: the players and the bot are members of its WoT


def run(game, scenario):
    async def main():
        with R.Node(cfg=CFG, members=[R.pubkey(ALICE), R.pubkey(BOB), BOT_PK]) as n, Bots(n, game) as bots:
            await scenario(n, bots)
    asyncio.run(main())


async def name(d, label):
    """A display name, so the cards and the bot's posts say who is who (a kind-0, published by the client)."""
    await d.b.js("__PC.publish(0, JSON.stringify({name:%s}), []).then(()=>1)" % json.dumps(label))


async def open_game(d, view, list_sel):
    await d.b.js("__PC.switchView(%s);true" % json.dumps(view))
    await R.wait(d.b, "!!document.querySelector(%s) && !document.querySelector(%s+' .spinner')"
                 % (json.dumps(list_sel), json.dumps(list_sel)), 30, 'the %s screen loaded its games' % view)


async def challenge(d, inp, res, opponent_fill, button='[data-ch]'):
    """Type the opponent's npub into the invite box and press Challenge, as a person does."""
    npub = await d.b.js('NostrTools.nip19.npubEncode(%s)' % json.dumps(R.pubkey(opponent_fill)))
    await d.b.js("(()=>{const i=document.querySelector(%s);i.value=%s;i.dispatchEvent(new Event('input',{bubbles:true}));"
                 "return true})()" % (json.dumps(inp), json.dumps(npub)))
    sel = '%s %s' % (res, button)
    await R.wait(d.b, '!!document.querySelector(%s)' % json.dumps(sel), 20, 'the opponent is offered to challenge')
    await d.b.js('document.querySelector(%s).click();true' % json.dumps(sel))


CARD = "document.querySelector('.chess-games .chess-game-card')"
BADGE = "((%s||{}).querySelector&&(%s.querySelector('.cc-badge')||{}).textContent||'')" % (CARD, CARD)
RESULT = "((%s&&%s.querySelector('.chess-result'))||{}).textContent||''" % (CARD, CARD)


def card_state(extra):
    return "({badge:%s,result:%s,board:%s,toasts:(window.__toastLog||[]).slice(-5),text:document.querySelector('#feed').innerText.slice(0,400)})" % (BADGE, RESULT, extra)


# ----------------------------------------------------------------------------------------------------- tic-tac-toe
TTT_CELLS = "[...document.querySelectorAll('.chess-games .chess-game-card .ttt-cell')].map(c=>(c.querySelector('.ttt-mark')||{}).textContent||'')"


async def ttt_shows(d, cells, what):
    await R.wait(d.b, 'JSON.stringify(%s)===%s' % (TTT_CELLS, json.dumps(json.dumps(cells, separators=(',', ':')))),
                 40, what, state=card_state(TTT_CELLS))


async def ttt_tap(d, i):
    await R.wait(d.b, "/Your move/.test(%s) && !!document.querySelector('.chess-game-card .ttt-cell.empty[data-i=\"%d\"]')" % (BADGE, i),
                 40, 'my turn, cell %d free' % (i + 1), state=card_state(TTT_CELLS))
    await d.b.js("document.querySelector('.chess-game-card .ttt-cell[data-i=\"%d\"]').click();true" % i)


@CHROME
def test_tic_tac_toe_challenge_accept_play_and_both_agree_who_won():
    async def scenario(n, bots):
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob:
            await name(alice, 'alice')
            await name(bob, 'bob')
            await open_game(alice, 'ttt', '#ttt-games')
            await open_game(bob, 'ttt', '#ttt-games')
            await challenge(alice, '#ttt-inv', '#ttt-inv-res', BOB)
            # Bob did nothing: the game the bot started shows on his screen, his turn (the invited side is X).
            await R.wait(bob.b, "/Your move/.test(%s)" % BADGE, 40, "Bob sees Alice's challenge as his move",
                         state=card_state(TTT_CELLS))
            await R.wait(alice.b, "/Waiting on/.test(%s)" % BADGE, 40, 'Alice sees the game, waiting on Bob',
                         state=card_state(TTT_CELLS))
            board = [''] * 9
            for who, mark, cell in ((bob, 'X', 0), (alice, 'O', 3), (bob, 'X', 1), (alice, 'O', 4), (bob, 'X', 2)):
                other = alice if who is bob else bob
                await ttt_tap(who, cell)
                board[cell] = mark
                await ttt_shows(other, board, 'the other player sees %s in cell %d' % (mark, cell + 1))
                await ttt_shows(who, board, 'the mover sees %s in cell %d' % (mark, cell + 1))
            await R.wait(bob.b, "/You won/.test(%s)" % RESULT, 40, 'Bob is told he won', state=card_state(TTT_CELLS))
            await R.wait(alice.b, "/You lost/.test(%s)" % RESULT, 40, 'Alice is told she lost', state=card_state(TTT_CELLS))
            rb, ra = await bob.b.js(RESULT), await alice.b.js(RESULT)
            assert rb.split('·', 1)[1].strip() == ra.split('·', 1)[1].strip(), (rb, ra)
            assert 'bob' in ra and 'wins' in ra, ra
    run('ttt', scenario)


# ------------------------------------------------------------------------------------------------------ connect 4
C4_CELLS = ("[...document.querySelectorAll('.chess-games .chess-game-card .c4-cell .c4-disc')]"
            ".map(d=>d.classList.contains('d1')?'1':(d.classList.contains('d2')?'2':''))")


async def c4_shows(d, cells, what):
    await R.wait(d.b, 'JSON.stringify(%s)===%s' % (C4_CELLS, json.dumps(json.dumps(cells, separators=(',', ':')))),
                 40, what, state=card_state(C4_CELLS))


async def c4_drop(d, col):
    await R.wait(d.b, "/Your move/.test(%s)" % BADGE, 40, 'my turn to drop in column %d' % (col + 1),
                 state=card_state(C4_CELLS))
    # the TOP cell of the column -- any cell of a column drops into it, as on the real board
    await d.b.js("document.querySelectorAll('.chess-game-card .c4-cell')[%d].click();true" % col)


@CHROME
def test_connect_four_challenge_accept_play_and_both_agree_who_won():
    async def scenario(n, bots):
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob:
            await name(alice, 'alice')
            await name(bob, 'bob')
            await open_game(alice, 'connect4', '#c4-games')
            await open_game(bob, 'connect4', '#c4-games')
            await challenge(alice, '#c4-inv', '#c4-inv-res', BOB)
            await R.wait(bob.b, "/Your move/.test(%s)" % BADGE, 40, "Bob sees Alice's challenge as his move",
                         state=card_state(C4_CELLS))
            await R.wait(alice.b, "/Waiting on/.test(%s)" % BADGE, 40, 'Alice sees the game, waiting on Bob',
                         state=card_state(C4_CELLS))
            cells, height = [''] * 42, [0] * 7
            moves = [(bob, '1', 0), (alice, '2', 1), (bob, '1', 0), (alice, '2', 1), (bob, '1', 0), (alice, '2', 1),
                     (bob, '1', 0)]
            for who, disc, col in moves:
                other = alice if who is bob else bob
                await c4_drop(who, col)
                row = 5 - height[col]
                height[col] += 1
                cells[row * 7 + col] = disc
                await c4_shows(other, cells, 'the other player sees the disc land in column %d' % (col + 1))
                await c4_shows(who, cells, 'the mover sees the disc land in column %d' % (col + 1))
            await R.wait(bob.b, "/You won/.test(%s)" % RESULT, 40, 'Bob is told he won', state=card_state(C4_CELLS))
            await R.wait(alice.b, "/You lost/.test(%s)" % RESULT, 40, 'Alice is told she lost', state=card_state(C4_CELLS))
            rb, ra = await bob.b.js(RESULT), await alice.b.js(RESULT)
            assert rb.split('·', 1)[1].strip() == ra.split('·', 1)[1].strip(), (rb, ra)
            assert 'bob' in ra, ra
    run('connect4', scenario)


# -------------------------------------------------------------------------------------------------------- hangman
HM = ("({word:((%s&&%s.querySelector('.hm-word'))||{}).textContent||'',"
      "misses:((%s&&%s.querySelector('.cc-meta .muted'))||{}).textContent||''})" % (CARD, CARD, CARD, CARD))


async def hm_guess(d, letter):
    key = ".chess-game-card .hm-key[data-l=\"%s\"]:not([disabled])" % letter
    await R.wait(d.b, '!!document.querySelector(%s)' % json.dumps(key), 40, 'the %s key is there to press' % letter.upper(),
                 state=card_state(HM))
    await d.b.js('document.querySelector(%s).click();true' % json.dumps(key))


async def hm_shows(d, word, what):
    await R.wait(d.b, '(%s).word.replace(/\\s+/g," ").trim()===%s' % (HM, json.dumps(word)), 40, what, state=card_state(HM))


@CHROME
def test_hangman_challenger_sets_the_word_friend_guesses_and_both_agree_who_won():
    async def scenario(n, bots):
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob:
            await name(alice, 'alice')
            await name(bob, 'bob')
            await open_game(alice, 'hangman', '#hm-games')
            await open_game(bob, 'hangman', '#hm-games')
            await challenge(alice, '#hm-inv', '#hm-inv-res', BOB)
            # The challenger picks the word, from her own screen -- not by finding the bot's DM.
            await R.wait(alice.b, "!!document.querySelector('.chess-game-card .hm-setword-in')", 40,
                         'Alice is asked for the secret word on her card', state=card_state(HM))
            assert not await alice.b.js("/You lost|You won/.test(%s)" % RESULT), 'a game nobody has played reads as over'
            await R.wait(bob.b, "/pick|word/i.test(%s) && !/You lost|You won/.test(%s)" % (BADGE, RESULT), 40,
                         'Bob sees the challenge, waiting for the word', state=card_state(HM))
            await alice.b.js("(()=>{const i=document.querySelector('.chess-game-card .hm-setword-in');i.value='wolf | a wild animal';"
                             "i.dispatchEvent(new Event('input',{bubbles:true}));"
                             "document.querySelector('.chess-game-card .hm-setword-go').click();return true})()")
            await hm_shows(bob, '_ _ _ _', 'Bob gets the masked word to guess')
            await hm_shows(alice, '_ _ _ _', 'Alice watches the same masked word')
            await hm_guess(bob, 'z')
            for d in (bob, alice):
                await R.wait(d.b, "/Misses 1\\/6/.test((%s).misses)" % HM, 40, 'the miss is counted on both screens',
                             state=card_state(HM))
            shown = ['_', '_', '_', '_']
            for i, letter in enumerate('wolf'):
                await hm_guess(bob, letter)
                shown[i] = letter.upper()
                if i < 3:
                    await hm_shows(alice, ' '.join(shown), 'Alice sees Bob uncover %s' % letter.upper())
            await R.wait(bob.b, "/You won/.test(%s)" % RESULT, 40, 'Bob is told he guessed it', state=card_state(HM))
            await R.wait(alice.b, "/You lost/.test(%s)" % RESULT, 40, 'Alice is told her word was guessed',
                         state=card_state(HM))
            for d in (alice, bob):
                await hm_shows(d, 'W O L F', 'the solved word is shown')
            rb, ra = await bob.b.js(RESULT), await alice.b.js(RESULT)
            assert rb.split('·', 1)[1].strip() == ra.split('·', 1)[1].strip(), (rb, ra)
            assert 'WOLF' in ra, ra
    run('hangman', scenario)
