"""Offline tests for the Blackjack engine + table state machine (no Nostr/IO).

Run: `venv-unified/bin/python botframework/test_blackjack.py`. Exits non-zero on failure.
Covers hand valuation, 3:2/1:1/push/lose payouts, a full round, persistent next_round (carry stacks,
drop busted/left), leave, and a fuzz of many rounds (always-legal, terminating, no negative stacks).
"""
import os, sys, secrets
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blackjack_game as bj

_PASS = 0


def check(cond, msg):
    global _PASS
    if not cond:
        print(f"  FAIL: {msg}")
        raise AssertionError(msg)
    _PASS += 1


def test_values():
    print("test_values")
    check(bj.hand_value(["AS", "KD"]) == 21, "A+K = 21")
    check(bj.is_blackjack(["AS", "KD"]), "A+K is blackjack")
    check(not bj.is_blackjack(["AS", "5D", "5C"]), "3-card 21 is NOT blackjack")
    check(bj.hand_value(["AS", "AD", "9C"]) == 21, "A+A+9 = 21 (one ace soft)")
    check(bj.hand_value(["AS", "AD", "AC", "8H"]) == 21, "three aces + 8 = 21")
    check(bj.hand_value(["KS", "QD", "2C"]) == 22, "K+Q+2 busts at 22")
    check(bj.hand_value(["AS", "6D", "KC"]) == 17, "A+6+K = 17 (ace demoted)")


def _force(state, pk, cards, dealer=None):
    """Stamp exact cards for a deterministic settle test."""
    state["hands"][pk] = list(cards)
    if dealer is not None:
        state["dhand"] = list(dealer)


def test_payouts():
    print("test_payouts")
    # player blackjack vs dealer 20 → 3:2. bet 100 → +150 net, stack from 400 → 400+250.
    st = bj.start_round(["p"], stacks={"p": 500}, bets={"p": 100})
    check(st["stacks"]["p"] == 400, "bet posted (500-100)")
    _force(st, "p", ["AS", "KD"], dealer=["KS", "QD"])
    st["done"]["p"] = True
    bj.settle(st)
    check(st["results"]["p"] == "blackjack", "natural blackjack detected")
    check(st["payouts"]["p"] == 150, f"blackjack pays 3:2 net +150 (got {st['payouts']['p']})")
    check(st["stacks"]["p"] == 650, f"stack 400+250 = 650 (got {st['stacks']['p']})")

    # plain win 1:1
    st = bj.start_round(["p"], stacks={"p": 500}, bets={"p": 100})
    _force(st, "p", ["TS", "9D"], dealer=["TS", "8D"]); st["done"]["p"] = True
    bj.settle(st)
    check(st["results"]["p"] == "win" and st["payouts"]["p"] == 100, "win pays 1:1 net +100")
    check(st["stacks"]["p"] == 600, "win stack 400+200")

    # push returns the bet
    st = bj.start_round(["p"], stacks={"p": 500}, bets={"p": 100})
    _force(st, "p", ["TS", "8D"], dealer=["TS", "8C"]); st["done"]["p"] = True
    bj.settle(st)
    check(st["results"]["p"] == "push" and st["payouts"]["p"] == 0 and st["stacks"]["p"] == 500,
          "push returns bet (net 0, stack restored)")

    # loss forfeits the bet
    st = bj.start_round(["p"], stacks={"p": 500}, bets={"p": 100})
    _force(st, "p", ["TS", "6D"], dealer=["TS", "9C"]); st["done"]["p"] = True
    bj.settle(st)
    check(st["results"]["p"] == "lose" and st["payouts"]["p"] == -100 and st["stacks"]["p"] == 400,
          "loss forfeits bet (net -100)")

    # dealer busts → player wins
    st = bj.start_round(["p"], stacks={"p": 500}, bets={"p": 100})
    _force(st, "p", ["TS", "7D"], dealer=["KS", "QD", "5C"]); st["done"]["p"] = True
    bj.settle(st)
    check(st["results"]["p"] == "win", "dealer bust → player wins")

    # player busts → loss even if dealer also busts
    st = bj.start_round(["p"], stacks={"p": 500}, bets={"p": 100})
    _force(st, "p", ["KS", "QD", "5C"], dealer=["KS", "QH", "6C"]); st["done"]["p"] = True
    bj.settle(st)
    check(st["results"]["p"] == "lose", "player bust loses regardless of dealer")


def test_bet_clamping():
    print("test_bet_clamping")
    st = bj.start_round(["p"], stacks={"p": 30}, bets={"p": 100})
    check(st["bet"]["p"] == 30 and st["stacks"]["p"] == 0, "bet clamped to stack (all-in 30)")
    st = bj.start_round(["p"], stacks={"p": 500}, bets={"p": 1})
    check(st["bet"]["p"] == bj.MIN_BET, "bet floored to MIN_BET")


def test_persistent_and_leave():
    print("test_persistent_and_leave")
    st = bj.start_round(["a", "b"], stacks={"a": 500, "b": 500}, bets={"a": 50, "b": 50})
    st["names"] = {"a": "A", "b": "B"}; st["round_no"] = 1
    _force(st, "a", ["KS", "QD", "5C"])   # a busts → loses the round → payout 0
    st["stacks"]["a"] = 0                   # and is broke
    st["done"]["a"] = True; st["done"]["b"] = True
    bj.settle(st)
    check(st["stacks"]["a"] == 0, "busted+broke player stays at 0 after settle")
    nxt, _ = bj.next_round(st)
    check(nxt is not None, "table continues while >=1 player has chips")
    check("a" not in nxt["seats"], "busted (0-chip) player dropped")
    check(nxt["round_no"] == 2, "round number advances")
    check("b" in nxt["seats"] and nxt["names"]["b"] == "B", "survivor kept with name")

    # leave mid-round folds you and you're not re-seated
    st = bj.start_round(["x", "y"], stacks={"x": 500, "y": 500})
    bj.leave(st, "x")
    check("x" in st["left"] and st["done"]["x"] and "x" in st["folded"], "leaver folded + recorded")
    bj.settle(st)
    check(st["results"]["x"] == "lose", "folded leaver loses the round")
    nxt, _ = bj.next_round(st)
    check(nxt is not None and "x" not in nxt["seats"], "leaver not re-seated; y continues")

    # everyone gone → table closes
    st = bj.start_round(["z"], stacks={"z": 500})
    bj.leave(st, "z")
    bj.settle(st)
    nxt, _ = bj.next_round(st)
    check(nxt is None, "last player left → table closes")


def test_fuzz(rounds=400):
    print(f"test_fuzz ({rounds} rounds)")
    for _ in range(rounds):
        seats = ["h", "bot"]
        st = bj.start_round(seats, bets={p: 25 for p in seats})
        st["names"] = {"h": "you", "bot": "Bot"}; st["round_no"] = 1
        plays = 0
        while not bj.all_done(st):
            plays += 1
            check(plays < 200, "round terminates")
            for p in seats:
                if st["done"].get(p):
                    continue
                la = bj.legal_actions(st, p)
                check("hit" in la and "stand" in la, "live player can hit/stand")
                # simple strategy: hit below 17, else stand
                if bj.hand_value(st["hands"][p]) < 17:
                    bj.hit(st, p)
                else:
                    bj.stand(st, p)
        bj.settle(st)
        for p in seats:
            check(st["stacks"][p] >= 0, "stack never negative")
            check(st["results"][p] in ("win", "lose", "push", "blackjack"), "valid result")


if __name__ == "__main__":
    try:
        test_values()
        test_payouts()
        test_bet_clamping()
        test_persistent_and_leave()
        test_fuzz()
    except AssertionError:
        print(f"\n✗ FAILED after {_PASS} checks")
        sys.exit(1)
    print(f"\n✓ all Blackjack engine tests passed ({_PASS} checks)")
