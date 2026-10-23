"""TWO PEOPLE PLAY CHESS TO CHECKMATE -- two accounts, two shipped clients, this node's REAL relay, the REAL chess
bot (tests/client/gamebot_harness.py). The same shape as test_board_games_between_two_accounts_full_app.py, kept in
its own file because a chess game is the longest of them.

Alice challenges Bob by npub; Bob is White (the invited side) and accepts by playing 1.f3; fool's mate follows
(1.f3 e5 2.g4 Qh4#), every move made by tapping a piece and then its square, each arriving on the other board.
Black (Alice) mates; BOTH screens then show the finished game -- Alice "You won", Bob "You lost", the same result
line, the mating queen on h4, and no move box. Until 2026-10-23 a finished chess game simply vanished from both
players' lists (only `active` games were drawn), so neither ever saw the checkmate or who won.
"""
import json

import pytest

from tests.client import realnode_harness as R
from tests.client.test_board_games_between_two_accounts_full_app import (ALICE, BOB, BADGE, RESULT, card_state,
                                                                          challenge, name, open_game, run)

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ("chess",)   # scripts/feature_coverage.py


CHESS_BOARD = ("Object.fromEntries([...document.querySelectorAll('.chess-games .chess-game-card .csq')]"
               ".filter(c=>c.dataset.pc).map(c=>[c.dataset.sq,c.dataset.pc]))")


async def chess_sees(d, sq, pc, what):
    await R.wait(d.b, '(%s)[%s]===%s' % (CHESS_BOARD, json.dumps(sq), json.dumps(pc)), 40, what,
                 state=card_state(CHESS_BOARD))


async def chess_move(d, frm, to):
    """Tap the piece, then its square -- the board's own two-tap move."""
    await R.wait(d.b, "/Your move|move to accept/.test(%s)" % BADGE, 40, 'my turn to play %s-%s' % (frm, to),
                 state=card_state(CHESS_BOARD))
    for sq in (frm, to):
        await d.b.js("document.querySelector('.chess-game-card .csq[data-sq=\"%s\"]').click();true" % sq)


@CHROME
def test_chess_challenge_accept_fools_mate_and_both_agree_who_won():
    async def scenario(n, bots):
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob:
            await name(alice, 'alice')
            await name(bob, 'bob')
            await open_game(alice, 'chess', '#chess-games-list')
            await open_game(bob, 'chess', '#chess-games-list')
            await challenge(alice, '#chess-inv', '#chess-inv-res', BOB, '[data-challenge]')
            await R.wait(bob.b, "/move to accept/.test(%s)" % BADGE, 40, "Bob sees Alice's challenge, his to accept",
                         state=card_state(CHESS_BOARD))
            await R.wait(alice.b, "/Invite sent/.test(%s)" % BADGE, 40, 'Alice sees her invite pending',
                         state=card_state(CHESS_BOARD))
            # Fool's mate: 1.f3 e5 2.g4 Qh4#. White = Bob (the invited side), Black = Alice.
            for who, frm, to, pc in ((bob, 'f2', 'f3', 'P'), (alice, 'e7', 'e5', 'p'), (bob, 'g2', 'g4', 'P')):
                other = alice if who is bob else bob
                await chess_move(who, frm, to)
                await chess_sees(other, to, pc, 'the other player sees %s-%s' % (frm, to))
                await chess_sees(who, to, pc, 'the mover sees %s-%s' % (frm, to))
            await chess_move(alice, 'd8', 'h4')
            await R.wait(alice.b, "/You won/.test(%s)" % RESULT, 40, 'Alice is told she mated Bob',
                         state=card_state(CHESS_BOARD))
            await R.wait(bob.b, "/You lost/.test(%s)" % RESULT, 40, 'Bob is told he was mated',
                         state=card_state(CHESS_BOARD))
            for d in (alice, bob):
                await chess_sees(d, 'h4', 'q', 'the mating queen is on h4')
            ra, rb = await alice.b.js(RESULT), await bob.b.js(RESULT)
            assert ra.split('·', 1)[1].strip() == rb.split('·', 1)[1].strip(), (ra, rb)
            assert 'checkmate' in ra.lower(), ra
            assert not await bob.b.js("!!document.querySelector('.chess-game-card .chess-move-in')"), \
                'a finished game still offers a move box'
    run('chess', scenario)
