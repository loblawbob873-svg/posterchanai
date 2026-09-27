"""The game bots play whole games -- through the same loop, relay, DMs and state documents as in
production (tests/botgame_harness.py fakes only the network).

Run: venv-unified/bin/python -m pytest tests/test_game_bots_play.py

Until this file the four board-game bots had one test between them: that their result posts call
`mentionify`. Nothing had ever started a game, made a move, rejected a bad one, reached a result or
survived a restart. Each game here is played to the end by real players sending real signed mentions
and real encrypted DM moves.
"""
import importlib
import sys

import pytest

from tests.botgame_harness import BOT_PK, GameWorld

pytest.importorskip("PIL")


def _last(lst):
    return lst[-1] if lst else ""


# ================================ tic-tac-toe ======================================================

@pytest.fixture
def ttt(monkeypatch, tmp_path):
    return GameWorld(monkeypatch, tmp_path, "tttListener", "process_ttt")


def test_ttt_two_players_play_to_a_win(ttt):
    alice, bob = ttt.player("alice"), ttt.player("bob")
    gid = ttt.mention(alice, "tictactoe let's go", tag=[bob])
    ttt.tick()
    st = ttt.state(gid)
    assert st and st["x"] == bob.pk and st["o"] == alice.pk, "the invited player is X and moves first"
    assert len(ttt.posts()) == 1 and "#tictactoe" in ttt.posts()[0]["content"], "one public opening post"
    assert "Your move" in _last(ttt.dms_to(bob)), "X was not DM'd their board"
    for who, cell in ((bob, 1), (alice, 4), (bob, 2), (alice, 5), (bob, 3)):
        ttt.dm(who, str(cell))
        ttt.tick()
    st = ttt.state(gid)
    assert st["status"] == "win" and st["winner_pk"] == bob.pk, st
    result = ttt.posts()[-1]["content"]
    assert "wins" in result and "gg" in result
    assert "nostr:npub1" in result, "the result must MENTION the players, not print bare handles"
    assert all(p in {t[1] for t in ttt.posts()[-1]["tags"] if t[0] == "p"} for p in (alice.pk, bob.pk))


def test_ttt_the_bot_never_loses(ttt):
    alice = ttt.player("alice")
    gid = ttt.mention(alice, "ttt")
    ttt.tick()
    for _ in range(9):
        st = ttt.state(gid)
        if st["status"] != "active":
            break
        ttt.dm(alice, str(st["cells"].index("") + 1))    # a naive human: always the first free cell
        ttt.tick()
    st = ttt.state(gid)
    assert st["status"] in ("win", "draw") and st.get("winner_pk") in (BOT_PK, None), st
    assert "🏁" in ttt.posts()[-1]["content"], "no result was posted"


def test_ttt_bad_moves_are_refused_and_change_nothing(ttt):
    alice, bob = ttt.player("alice"), ttt.player("bob")
    gid = ttt.mention(alice, "tictactoe", tag=[bob])
    ttt.tick()
    # alice is O and has not been sent a board yet, so her move goes in the thread, publicly
    nid = ttt.reply(alice, "5", gid); ttt.tick()
    assert any("not your turn" in r for r in ttt.replies_to(nid)), ttt.replies_to(nid)
    ttt.dm(bob, "banana"); ttt.tick()
    assert "cell number" in _last(ttt.dms_to(bob))
    ttt.dm(bob, "5"); ttt.tick()
    ttt.dm(alice, "5"); ttt.tick()
    assert "is taken" in _last(ttt.dms_to(alice))
    assert ttt.state(gid)["cells"].count("") == 8, "a refused move changed the board"


def test_ttt_resign_and_a_move_after_the_end(ttt):
    alice, bob = ttt.player("alice"), ttt.player("bob")
    gid = ttt.mention(alice, "tictactoe", tag=[bob])
    ttt.tick()
    ttt.dm(bob, "resign"); ttt.tick()
    st = ttt.state(gid)
    assert st["status"] == "resigned" and st["winner_pk"] == alice.pk
    nid = ttt.reply(alice, "5", gid); ttt.tick()
    assert any("over" in r for r in ttt.replies_to(nid)), "a move after the end must say the game is over"


def test_ttt_a_move_is_applied_once_and_a_game_survives_a_restart(ttt, monkeypatch, tmp_path):
    alice, bob = ttt.player("alice"), ttt.player("bob")
    gid = ttt.mention(alice, "tictactoe", tag=[bob])
    ttt.tick()
    ttt.dm(bob, "1")
    ttt.tick(3)                                           # the same DM, polled three times
    assert ttt.state(gid)["cells"].count("X") == 1, "one DM was applied more than once"
    ttt.restart()                                         # the bot process restarts
    ttt.tick()
    assert ttt.state(gid)["cells"].count("X") == 1, "a restart re-applied an old move"
    ttt.dm(alice, "5"); ttt.tick()
    assert ttt.state(gid)["cells"][4] == "O", "the game did not continue after the restart"


def test_ttt_a_stray_dm_from_before_the_game_is_never_a_move(ttt):
    """A bare DM is routed by the player's game pointer. One sent BEFORE that pointer existed (here,
    before the game did) used to lie unclaimed for the 3-day lookback and then be applied as the
    first move of the next game the player joined."""
    alice, bob = ttt.player("alice"), ttt.player("bob")
    ttt.dm(bob, "5")                                      # about nothing -- there is no game yet
    ttt.tick()
    gid = ttt.mention(alice, "tictactoe", tag=[bob])      # bob becomes X, and gets a pointer
    ttt.tick(2)
    assert ttt.state(gid)["cells"] == [""] * 9, "a DM from before the game was played as its first move"
    ttt.dm(bob, "1"); ttt.tick()
    assert ttt.state(gid)["cells"][0] == "X", "a real move after the pointer must still work"


# ================================ Connect 4 ========================================================

@pytest.fixture
def c4(monkeypatch, tmp_path):
    return GameWorld(monkeypatch, tmp_path, "connect4Listener", "process_connect4")


def test_connect4_a_vertical_four_wins_and_gravity_holds(c4):
    alice, bob = c4.player("alice"), c4.player("bob")
    gid = c4.mention(alice, "connect4", tag=[bob])
    c4.tick()
    st = c4.state(gid)
    first, second = (bob, alice) if st["p1"] == bob.pk else (alice, bob)
    for who, col in ((first, 1), (second, 2), (first, 1), (second, 2), (first, 1), (second, 2), (first, 1)):
        c4.dm(who, str(col)); c4.tick()
    st = c4.state(gid)
    cols, rows = 7, 6
    assert [st["cells"][r * cols + 0] for r in range(rows)][-4:] == ["1"] * 4, "discs did not fall to the bottom"
    assert st["status"] == "win" and st["winner_pk"] == first.pk, st


def test_connect4_a_full_column_is_refused(c4):
    alice, bob = c4.player("alice"), c4.player("bob")
    gid = c4.mention(alice, "connect4", tag=[bob])
    c4.tick()
    st = c4.state(gid)
    order = [bob, alice] if st["p1"] == bob.pk else [alice, bob]
    for i in range(6):                                    # fill column 3 without four-in-a-row
        c4.dm(order[i % 2], "3"); c4.tick()
    c4.dm(order[0], "3"); c4.tick()
    assert "full" in _last(c4.dms_to(order[0])).lower()
    assert c4.state(gid)["status"] == "active"


def test_connect4_the_bot_blocks_an_open_three(c4):
    alice = c4.player("alice")
    gid = c4.mention(alice, "connect4")
    c4.tick()
    st = c4.state(gid)
    assert st["p1"] == alice.pk and st["p2"] == BOT_PK
    for col in (1, 2, 3):                                 # alice builds 1-2-3 on the bottom row
        st = c4.state(gid)
        if st["status"] != "active":
            break
        if st["cells"][5 * 7 + col - 1]:
            continue
        c4.dm(alice, str(col)); c4.tick()
    st = c4.state(gid)
    bottom = st["cells"][5 * 7: 5 * 7 + 7]
    assert not (bottom[0] == bottom[1] == bottom[2] == bottom[3] == "1"), "the bot let a bottom-row four through"
    assert st["cells"].count("2") >= 2, "the bot did not answer each move"


# ================================ Hangman ==========================================================

@pytest.fixture
def hm(monkeypatch, tmp_path):
    return GameWorld(monkeypatch, tmp_path, "hangmanListener", "process_hangman")


def test_hangman_solo_solved_letter_by_letter(hm):
    alice = hm.player("alice")
    gid = hm.mention(alice, "hangman")
    hm.tick()
    st = hm.state(gid)
    word = hm.game._word_of(st)
    assert word and st["wordlen"] == len(word) and word not in str(st), "the word must be stored ENCRYPTED"
    for letter in dict.fromkeys(word):
        hm.dm(alice, letter); hm.tick()
    st = hm.state(gid)
    assert st["status"] == "won", st
    assert word.upper() in hm.posts()[-1]["content"]


def test_hangman_six_misses_lose_and_reveal_the_word(hm):
    alice = hm.player("alice")
    gid = hm.mention(alice, "hangman")
    hm.tick()
    word = hm.game._word_of(hm.state(gid))
    misses = [c for c in "zqxjvkwyfbgmpd" if c not in word][:hm.game._MAX_WRONG]
    for i, c in enumerate(misses):
        if i == 1:
            hm.dm(alice, c); hm.tick()                    # guessing the same letter twice costs nothing
        hm.dm(alice, c); hm.tick()
    st = hm.state(gid)
    assert st["status"] == "lost" and st["wrong"] == hm.game._MAX_WRONG, st
    assert "already tried" in " ".join(hm.dms_to(alice))
    assert word.upper() in hm.posts()[-1]["content"], "a lost game must reveal the word"


def test_hangman_challenge_a_friend_with_your_own_word(hm):
    alice, bob = hm.player("alice"), hm.player("bob")
    gid = hm.mention(alice, "hangman", tag=[bob])
    hm.tick()
    assert hm.state(gid)["status"] == "awaiting_word" and "secret WORD" in _last(hm.dms_to(alice))
    hm.dm(bob, "pancake"); hm.tick()                      # only the setter may set the word
    assert hm.state(gid)["status"] == "awaiting_word"
    hm.dm(alice, "pancake | breakfast favorite"); hm.tick()
    st = hm.state(gid)
    assert st["status"] == "active" and st["hint"] == "breakfast favorite"
    assert hm.dms_to(bob), "the guesser was not sent the puzzle"
    hm.dm(bob, "pancake"); hm.tick()                      # a whole-word guess
    assert hm.state(gid)["status"] == "won"


# ================================ chess ============================================================

@pytest.fixture
def chs(monkeypatch, tmp_path):
    pytest.importorskip("chess")
    return GameWorld(monkeypatch, tmp_path, "chessListener", "process_chess")


def test_chess_scholars_mate_ends_the_game(chs):
    alice, bob = chs.player("alice"), chs.player("bob")
    gid = chs.mention(alice, "chess", tag=[bob])
    chs.tick()
    st = chs.state(gid)
    white, black = (alice, bob) if st["white"] == alice.pk else (bob, alice)
    for who, mv in ((white, "e2e4"), (black, "e7e5"), (white, "f1c4"), (black, "b8c6"),
                    (white, "d1h5"), (black, "g8f6"), (white, "h5f7")):
        chs.dm(who, mv); chs.tick()
    st = chs.state(gid)
    assert st["status"] != "active", st
    assert st.get("winner_pk") == white.pk, st
    assert "🏁" in chs.posts()[-1]["content"] or "mate" in chs.posts()[-1]["content"].lower()


def test_chess_an_illegal_move_is_refused(chs):
    alice, bob = chs.player("alice"), chs.player("bob")
    gid = chs.mention(alice, "chess", tag=[bob])
    chs.tick()
    st = chs.state(gid)
    white = alice if st["white"] == alice.pk else bob
    fen = st["fen"]
    chs.dm(white, "e2e5"); chs.tick()
    assert chs.state(gid)["fen"] == fen, "an illegal move changed the position"
    assert chs.dms_to(white)[-1] != chs.dms_to(white)[0] or len(chs.dms_to(white)) > 1, "no reply to an illegal move"


def test_chess_the_bot_answers_with_a_legal_move(chs):
    import chess
    alice = chs.player("alice")
    gid = chs.mention(alice, "chess")
    chs.tick()
    st = chs.state(gid)
    assert st["white"] == alice.pk and st["black"] == BOT_PK
    chs.dm(alice, "e2e4"); chs.tick()
    st = chs.state(gid)
    board = chess.Board(st["fen"])
    assert len(st["moves"]) == 2 and board.turn == chess.WHITE, "the bot did not reply with a move"
    assert board.is_valid()
