"""Blackjack TABLE state machine — pure (no Nostr/IO), so the betting + payout flow is unit-testable.

Each player plays their OWN hand vs the SHARED dealer. A bet is placed (deducted from the stack) at
round start; once every seat is done the dealer draws to 17 and `settle` pays out (natural blackjack
3:2, a win 1:1, a push returns the bet, a loss forfeits it). `next_round` re-deals carrying chip
stacks and dropping anyone who left or busted (0 chips) — the persistent table. Cards are "<rank><suit>"
strings (e.g. "AS", "TD"); ranks A,2..9,T,J,Q,K and suits S,H,D,C.

State is a plain JSON-able dict (it lives in a kind-30078 doc). Chips are play-money.
"""
import random

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K"]
SUITS = ["S", "H", "D", "C"]
START_STACK = 500
DEFAULT_BET = 25
MIN_BET = 5
DEALER_STANDS = 17


def new_deck():
    deck = [r + s for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck


def card_value(rank):
    if rank == "A":
        return 11
    if rank in ("T", "J", "Q", "K"):
        return 10
    return int(rank)


def hand_value(hand):
    """Best value of a hand, demoting aces from 11→1 as needed to avoid busting."""
    total = sum(card_value(c[:-1]) for c in hand)
    aces = sum(1 for c in hand if c[0] == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_blackjack(hand):
    return len(hand) == 2 and hand_value(hand) == 21


def _clamp_bet(bet, stack):
    """A legal bet: at least MIN_BET (or the whole stack if short), never more than the stack."""
    if stack <= 0:
        return 0
    bet = int(bet or DEFAULT_BET)
    bet = max(MIN_BET, bet)
    return min(bet, stack)


def new_table(seats, stacks=None):
    """A table waiting for the FIRST bet — no cards dealt yet (status 'betting'). The player places a
    bet IN the game and deals each hand from here; next_round deals from this just like from 'over'."""
    seats = list(seats)
    stacks = dict(stacks or {p: START_STACK for p in seats})
    return {
        "seats": seats, "button": 0, "stacks": stacks, "bet": {}, "bet_pref": {},
        "deck": [], "hands": {}, "dhand": [], "done": {}, "busted": [], "folded": [], "left": [],
        "status": "betting", "results": {}, "payouts": {}, "result": "",
        "dealer_stands": DEALER_STANDS, "round_no": 0,
    }


def start_round(seats, stacks=None, bets=None, button=0):
    """Deal a new round. `seats` = player ids; `stacks` = {pk->chips} (defaults to START_STACK each);
    `bets` = {pk->desired bet} (defaults to DEFAULT_BET, clamped to the stack). Returns the state.

    Players with a 0 stack are NOT dealt in (they've busted out) — callers should drop them first via
    next_round, but we guard here too. A natural blackjack auto-stands."""
    seats = [p for p in seats]
    stacks = dict(stacks or {p: START_STACK for p in seats})
    bets = dict(bets or {})
    deck = new_deck()
    bet = {}
    hands = {}
    done = {}
    for p in seats:
        b = _clamp_bet(bets.get(p, DEFAULT_BET), stacks.get(p, 0))
        bet[p] = b
        stacks[p] = stacks.get(p, 0) - b               # post the bet
        hands[p] = [deck.pop(), deck.pop()]
        done[p] = is_blackjack(hands[p])               # naturals auto-stand
    dhand = [deck.pop(), deck.pop()]
    st = {
        "seats": list(seats), "button": button % len(seats) if seats else 0,
        "stacks": stacks, "bet": bet, "deck": deck, "hands": hands, "dhand": dhand,
        "done": done, "busted": [], "folded": [], "left": [],
        "status": "playing", "results": {}, "payouts": {}, "result": "",
        "dealer_stands": DEALER_STANDS,
    }
    return st


def legal_actions(state, pk):
    """What `pk` may do now: hit/stand while their hand is live (not done, not busted)."""
    if state.get("status") != "playing" or pk not in state.get("seats", []):
        return {}
    if state["done"].get(pk):
        return {}
    return {"hit": True, "stand": True}


def hit(state, pk):
    """Draw a card for pk. Returns (state, busted_bool). A bust ends their hand."""
    if not legal_actions(state, pk).get("hit"):
        return state, False
    if state["deck"]:
        state["hands"][pk].append(state["deck"].pop())
    if hand_value(state["hands"][pk]) > 21:
        state["done"][pk] = True
        if pk not in state["busted"]:
            state["busted"].append(pk)
        return state, True
    return state, False


def stand(state, pk):
    if not legal_actions(state, pk).get("stand"):
        return state
    state["done"][pk] = True
    return state


def leave(state, pk):
    """Mark pk gone: stand them out of the current round (if any) and don't re-seat them next round."""
    if pk not in state.get("seats", []):
        return state
    state.setdefault("left", [])
    if pk not in state["left"]:
        state["left"].append(pk)
    if state.get("status") == "playing" and not state["done"].get(pk):
        state["done"][pk] = True
        if pk not in state["folded"]:
            state["folded"].append(pk)
    return state


def all_done(state):
    return all(state["done"].get(p) for p in state.get("seats", []))


def dealer_play(state):
    """Dealer draws to DEALER_STANDS (only if at least one player can still win — else no point)."""
    live = [p for p in state["seats"] if p not in state["folded"] and hand_value(state["hands"][p]) <= 21]
    if live:
        while hand_value(state["dhand"]) < state.get("dealer_stands", DEALER_STANDS) and state["deck"]:
            state["dhand"].append(state["deck"].pop())


def settle(state):
    """Dealer plays, then pay out every seat against the dealer. Mutates stacks; sets results/payouts/
    status. Returns the state. Payout = net chips change for the player (incl. their returned bet)."""
    if state.get("status") != "playing":
        return state
    dealer_play(state)
    dv = hand_value(state["dhand"])
    dbj = is_blackjack(state["dhand"])
    dbust = dv > 21
    folded = set(state.get("folded", []))
    results, payouts = {}, {}
    for pk in state["seats"]:
        hand = state["hands"][pk]
        pv = hand_value(hand)
        bet = state["bet"].get(pk, 0)
        pbj = is_blackjack(hand)
        if pk in folded or pv > 21:
            outcome, ret = "lose", 0                          # forfeit the bet
        elif pbj and not dbj:
            outcome, ret = "blackjack", bet + int(bet * 3 / 2)  # 3:2 → bet back + 1.5x
        elif dbj and not pbj:
            outcome, ret = "lose", 0
        elif dbust or pv > dv:
            outcome, ret = "win", bet * 2                      # 1:1 → bet back + 1x
        elif pv < dv:
            outcome, ret = "lose", 0
        else:
            outcome, ret = "push", bet                          # bet returned
        state["stacks"][pk] = state["stacks"].get(pk, 0) + ret
        results[pk] = outcome
        payouts[pk] = ret - bet                                 # net change for display (+win / -loss)
    state["results"] = results
    state["payouts"] = payouts
    state["status"] = "over"
    state["result"] = _summary(state)
    return state


def _summary(state):
    dv = hand_value(state["dhand"])
    dbj = is_blackjack(state["dhand"])
    tail = " (BJ)" if dbj else (" bust" if dv > 21 else "")
    word = {"blackjack": "BLACKJACK", "win": "won", "push": "push", "lose": "lost"}
    parts = []
    for pk in state["seats"]:
        nm = (state.get("names", {}) or {}).get(pk, pk)
        parts.append(f"{nm} {word.get(state['results'].get(pk, 'lose'), 'lost')}")
    return f"Dealer {dv}{tail} — " + ", ".join(parts)


def next_round(state, bets=None):
    """Persistent table: deal the next round. Carry chip stacks, drop anyone who LEFT or busted out
    (0 chips), and re-post bets. Returns (state, None) or (None, ...) if <1 player remains (closed)."""
    left = set(state.get("left", []))
    seats = [p for p in state["seats"] if p not in left and state["stacks"].get(p, 0) > 0]
    if not seats:
        return None, None
    carry = {p: state["stacks"][p] for p in seats}
    # default each player's next bet to their previous bet (clamped later), unless overridden
    prev = state.get("bet", {})
    nb = {p: (bets or {}).get(p, prev.get(p, DEFAULT_BET)) for p in seats}
    btn = (state.get("button", 0) + 1) % len(seats)
    st = start_round(seats, stacks=carry, bets=nb, button=btn)
    st["names"] = {p: state.get("names", {}).get(p, p) for p in seats}
    st["bot"] = state.get("bot")
    st["root"] = state.get("root")
    st["gameid"] = state.get("gameid")
    st["private"] = state.get("private")
    st["round_no"] = state.get("round_no", 1) + 1
    return st, None
