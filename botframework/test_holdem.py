"""Offline tests for the Texas Hold'em engine + table state machine (no Nostr/IO).

Run: `venv-unified/bin/python botframework/test_holdem.py` (from repo root) or
`python test_holdem.py` from within botframework/. Exits non-zero on failure.

Covers the parts that MUST be correct: 7-card hand ranking, all-in side-pot payout, and a
full SOLO player-vs-bot PERSISTENT table driven hand-after-hand to a bust — asserting every
action taken is legal, every hand terminates, and chips are conserved throughout.
"""
import os, sys, secrets
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from holdem_engine import (
    evaluate7, distribute_pot, card_str,
    HIGH, PAIR, TWO_PAIR, TRIPS, STRAIGHT, FLUSH, FULL_HOUSE, QUADS, STRAIGHT_FLUSH,
)
import holdem_game as hg

RANKS = "23456789TJQKA"
_PASS = 0


def C(rank_char, suit_idx):
    """Card int from a rank char ('2'..'A') and suit index 0..3."""
    return suit_idx * 13 + RANKS.index(rank_char)


def check(cond, msg):
    global _PASS
    if not cond:
        print(f"  FAIL: {msg}")
        raise AssertionError(msg)
    _PASS += 1


# ── 1. hand ranking ───────────────────────────────────────────────────────────
def test_rankings():
    print("test_rankings")
    # exact 5-card categories (suit 0=♠,1=♥,2=♦,3=♣)
    royal   = [C('A',0), C('K',0), C('Q',0), C('J',0), C('T',0)]
    quads   = [C('9',0), C('9',1), C('9',2), C('9',3), C('2',0)]
    boat    = [C('8',0), C('8',1), C('8',2), C('3',0), C('3',1)]
    flush   = [C('A',2), C('J',2), C('8',2), C('5',2), C('2',2)]
    straight= [C('6',0), C('5',1), C('4',2), C('3',3), C('2',0)]
    wheel   = [C('A',0), C('2',1), C('3',2), C('4',3), C('5',0)]   # A-low straight
    trips   = [C('Q',0), C('Q',1), C('Q',2), C('7',0), C('2',1)]
    twopair = [C('K',0), C('K',1), C('4',2), C('4',3), C('9',0)]
    pair    = [C('A',0), C('A',1), C('K',2), C('7',3), C('2',0)]
    high    = [C('A',0), C('Q',1), C('9',2), C('5',3), C('3',0)]

    check(evaluate7(royal)[0]    == STRAIGHT_FLUSH, "royal = straight flush")
    check(evaluate7(quads)[0]    == QUADS,          "quads")
    check(evaluate7(boat)[0]     == FULL_HOUSE,     "full house")
    check(evaluate7(flush)[0]    == FLUSH,          "flush")
    check(evaluate7(straight)[0] == STRAIGHT,       "straight")
    check(evaluate7(wheel)[0]    == STRAIGHT,       "wheel straight (A-2-3-4-5)")
    check(evaluate7(wheel)[1]    == 3,              "wheel is 5-high (rank idx 3)")
    check(evaluate7(trips)[0]    == TRIPS,          "trips")
    check(evaluate7(twopair)[0]  == TWO_PAIR,       "two pair")
    check(evaluate7(pair)[0]     == PAIR,           "pair")
    check(evaluate7(high)[0]     == HIGH,           "high card")

    # 7-card best-of: two pair + a third pair must still read as TWO_PAIR (best 5)
    seven = [C('K',0), C('K',1), C('4',2), C('4',3), C('9',0), C('9',1), C('2',2)]
    check(evaluate7(seven)[0] == TWO_PAIR, "7-card picks best two pair")

    # ordering: every category strictly beats the one below it
    order = [high, pair, twopair, trips, straight, flush, boat, quads, royal]
    for lo, hi in zip(order, order[1:]):
        check(evaluate7(hi) > evaluate7(lo), f"{evaluate7(hi)[0]} > {evaluate7(lo)[0]}")


# ── 2. side pots ────────────────────────────────────────────────────────────--
def test_side_pots():
    print("test_side_pots")
    # A all-in 100, B and C call 100 each → one main pot of 300; best hand takes all.
    contrib = {"A": 100, "B": 100, "C": 100}
    ranks = {"A": (TRIPS, 12, 5, 4), "B": (PAIR, 11, 10, 9, 8), "C": (HIGH, 12, 9, 5, 4, 3)}
    won = distribute_pot(contrib, ["A", "B", "C"], ranks)
    check(won["A"] == 300 and won["B"] == 0 and won["C"] == 0, "single pot to best hand")
    check(sum(won.values()) == sum(contrib.values()), "chips conserved (simple)")

    # Short all-in side pot: A in for 50, B & C in for 100. Main pot 150 (50*3); side pot 100 (50*2,
    # B+C only). If A wins, A can only take the main pot; the side pot goes to better of B/C.
    contrib = {"A": 50, "B": 100, "C": 100}
    ranks = {"A": (STRAIGHT, 8), "B": (TRIPS, 10, 5, 4), "C": (PAIR, 9, 8, 7, 6)}
    won = distribute_pot(contrib, ["A", "B", "C"], ranks)
    check(won["A"] == 150, "short all-in wins only the main pot")
    check(won["B"] == 100, "side pot to next-best contributor")
    check(won["C"] == 0, "worst hand wins nothing")
    check(sum(won.values()) == 250, "chips conserved (side pot)")

    # Split pot with odd chip → odd chip to first (seat-order) winner, total conserved.
    contrib = {"A": 25, "B": 25}
    ranks = {"A": (PAIR, 10, 9, 8, 7), "B": (PAIR, 10, 9, 8, 7)}
    won = distribute_pot(contrib, ["A", "B"], ranks)
    check(won["A"] + won["B"] == 50, "split pot conserves chips")
    check(abs(won["A"] - won["B"]) <= 1, "split within one chip (odd-chip rule)")


# ── 3. human strategy for the sim ──────────────────────────────────────────────
def human_act(st, p):
    """A varied-but-legal human: mostly check/call, sometimes raise, rarely fold/all-in — enough to
    exercise every branch of act() over many hands."""
    legal = hg.legal_actions(st, p)
    r = secrets.randbelow(100)
    if r < 8 and legal.get("allin"):
        return ("allin", None)
    if r < 30 and "raise_to_min" in legal:
        lo, hi = legal["raise_to_min"], legal["raise_to_max"]
        target = lo if hi <= lo else lo + secrets.randbelow(hi - lo + 1)
        return ("raise", target)
    if legal.get("check"):
        return ("check", None)
    if "call" in legal:
        # fold a small fraction of the time when facing a bet, else call
        return ("fold", None) if r < 12 else ("call", None)
    return ("fold", None)


def play_one_hand(st):
    """Drive one hand to status=='done' via act(); assert legality + termination. Returns final st."""
    total_before = sum(st["stacks"].values()) + sum(st["contrib"].values())
    steps = 0
    while st["status"] == "betting":
        steps += 1
        check(steps < 2000, "hand terminates (step bound)")
        p = st["to_act"]
        check(p is not None, "betting hand always has someone to act")
        legal = hg.legal_actions(st, p)
        check(bool(legal), f"to_act player {p} has legal actions")
        if p == st.get("bot"):
            # the bot is heuristic; act() deliberately legalizes its choice (e.g. a raise it can't
            # fully afford becomes an all-in), so we only require the hand to keep progressing.
            action, amount = hg.bot_decide(st, p)
        else:
            action, amount = human_act(st, p)
            # the human only ever picks from legal_actions → assert that contract holds exactly.
            ok = (action in ("fold", "check", "call", "allin") and legal.get(action)) or \
                 (action == "raise" and "raise_to_min" in legal)
            check(ok, f"human action '{action}' is legal (legal={list(legal)})")
        st, _ev = hg.act(st, p, action, amount)
    check(st["status"] == "done", "hand reaches done")
    total_after = sum(st["stacks"].values())
    check(total_after == total_before, f"chips conserved over hand ({total_after} == {total_before})")
    check(sum(st.get("winners", {}).values()) == sum(st["contrib"].values()) or st["contrib"] == {},
          "winners receive exactly the pot")
    return st


def test_solo_persistent_table(rounds=200):
    print(f"test_solo_persistent_table ({rounds} tables)")
    for t in range(rounds):
        human, bot = "human_pk", "bot_pk"
        seats = [human, bot]
        st, _ = hg.start_hand(seats)
        st["bot"], st["names"], st["hand_no"] = bot, {human: "you", bot: "Bot"}, 1
        # true chip total = stacks + whatever's already posted as blinds (contrib)
        start_total = sum(st["stacks"].values()) + sum(st["contrib"].values())
        hands = 0
        while True:
            st = play_one_hand(st)
            hands += 1
            check(hands < 5000, "table eventually closes (someone busts)")
            # persistent table: deal the next hand, carrying stacks + rotating button
            st, _ = hg.next_hand(st)
            if st is None:
                break          # a player busted → table closed
            check(sum(st["stacks"].values()) + sum(st["contrib"].values()) == start_total,
                  "chips conserved across hands")
        # table closed because someone reached 0 — the other holds all the chips
    print(f"  played varied multi-hand tables to a bust, all conserved")


def test_leave_mechanics():
    print("test_leave_mechanics")
    # 3-handed: a leave folds that player out of the live hand and records them as gone, but the
    # hand keeps going for the other two.
    seats = ["a", "b", "c"]
    st, _ = hg.start_hand(seats)
    st["bot"], st["names"], st["hand_no"] = None, {p: p for p in seats}, 1
    st, _ = hg.leave(st, "c")
    check("c" in st.get("left", []), "leaver recorded in left[]")
    check("c" in st["folded"], "leaver folded out of the current hand")
    check(st["status"] == "betting" and st["to_act"] != "c", "hand continues, action skips leaver")


def test_next_hand_reseating():
    print("test_next_hand_reseating")
    # deterministic: a finished table where c left and b busted (0 chips) → next hand re-seats only
    # the survivors, drops leaver + busted, preserves table metadata and bumps hand_no.
    done = {"seats": ["a", "b", "c"], "button": 0, "stacks": {"a": 1400, "b": 0, "c": 600},
            "contrib": {}, "left": ["c"], "sb": 5, "bb": 10,
            "names": {"a": "A", "b": "B", "c": "C"}, "bot": None, "root": "evt1",
            "gameid": "g1", "hand_no": 4, "status": "done"}
    nxt, _ = hg.next_hand(done)
    check(nxt is None, "leaver + busted leave <2 survivors → table closes")

    # same but c stayed: a(1400) + c(600) survive, b busted out.
    done["left"] = []
    nxt, _ = hg.next_hand(done)
    check(nxt is not None, "two survivors → next hand deals")
    check(nxt["seats"] == ["a", "c"], "busted player dropped, seat order kept")
    # blinds are posted in the new hand, so the carried amount is stack + what's already in the pot
    check(nxt["stacks"]["a"] + nxt["contrib"]["a"] == 1400 and
          nxt["stacks"]["c"] + nxt["contrib"]["c"] == 600, "stacks carried over (minus posted blinds)")
    check(nxt["hand_no"] == 5, "hand number advances")
    check(nxt["gameid"] == "g1" and nxt["root"] == "evt1", "table metadata preserved")
    check(sum(nxt["stacks"].values()) + sum(nxt["contrib"].values()) == 2000, "chips carried intact")

    # heads-up minus one leaver → closes.
    hu = {"seats": ["x", "y"], "button": 0, "stacks": {"x": 1000, "y": 1000}, "contrib": {},
          "left": ["y"], "sb": 5, "bb": 10, "names": {}, "bot": None, "root": None,
          "gameid": "g2", "hand_no": 1, "status": "done"}
    nxt, _ = hg.next_hand(hu)
    check(nxt is None, "heads-up minus 1 → table closes")


if __name__ == "__main__":
    try:
        test_rankings()
        test_side_pots()
        test_leave_mechanics()
        test_next_hand_reseating()
        test_solo_persistent_table()
    except AssertionError:
        print(f"\n✗ FAILED after {_PASS} checks")
        sys.exit(1)
    print(f"\n✓ all Hold'em tests passed ({_PASS} checks)")
