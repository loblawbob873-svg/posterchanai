"""Texas Hold'em TABLE state machine — pure (no Nostr/IO), so the betting flow is unit-testable.

`start_hand` builds a hand state; `act` applies one player action and advances the game (posting
blinds, opening streets, triggering showdown) deterministically. The listener (holdemListener.py)
just translates Nostr DMs/replies into act() calls and renders the resulting state + events.

State is a plain JSON-able dict (it lives in a kind-30078 doc). Chips are play-money.
"""

from holdem_engine import new_deck, evaluate7, distribute_pot, card_str, hand_name

START_STACK = 1000
SMALL_BLIND = 5
BIG_BLIND = 10
STREETS = ["preflop", "flop", "turn", "river"]
_BOARD_BY_STREET = {"flop": 3, "turn": 4, "river": 5}


def start_hand(seats, button=0, stacks=None, sb=SMALL_BLIND, bb=BIG_BLIND):
    """Deal a new hand. `seats` = player ids in seat order. Returns (state, events)."""
    seats = list(seats)
    n = len(seats)
    stacks = dict(stacks or {p: START_STACK for p in seats})
    deck = new_deck()
    hole = {p: [deck.pop(), deck.pop()] for p in seats}
    st = {
        "seats": seats, "button": button % n, "stacks": stacks, "deck": deck,
        "hole": hole, "board": [], "street": "preflop",
        "contrib": {p: 0 for p in seats},      # total in the pot this hand (for side pots)
        "street_bet": {p: 0 for p in seats},    # committed THIS street (to compute call amount)
        "to_call": 0, "min_raise": bb, "last_raiser": None,
        "folded": [], "allin": [], "acted": [],  # acted = players who've acted since last raise
        "to_act": None, "status": "betting", "winners": {}, "result": "",
        "sb": sb, "bb": bb,
    }
    events = ["dealt"]
    # blinds: SB is left of button, BB next (heads-up: button is SB).
    sb_i = (st["button"] + (0 if n == 2 else 1)) % n
    bb_i = (sb_i + 1) % n
    _post(st, seats[sb_i], sb)
    _post(st, seats[bb_i], bb)
    st["to_call"] = bb
    st["min_raise"] = bb
    st["last_raiser"] = seats[bb_i]
    st["acted"] = []                            # BB has option, so blinds don't count as "acted"
    st["to_act"] = seats[(bb_i + 1) % n]        # action starts left of BB
    _skip_done(st)
    return st, events


def _post(st, p, amount):
    amount = min(amount, st["stacks"][p])
    st["stacks"][p] -= amount
    st["contrib"][p] += amount
    st["street_bet"][p] += amount
    if st["stacks"][p] == 0 and p not in st["allin"]:
        st["allin"].append(p)


def _active(st):
    """Players still in the hand (not folded)."""
    return [p for p in st["seats"] if p not in st["folded"]]


def _can_act(st):
    """Players who can still take an action (not folded, not all-in)."""
    return [p for p in st["seats"] if p not in st["folded"] and p not in st["allin"]]


def _skip_done(st):
    """Advance `to_act` past folded/all-in players; close the round if nobody can act."""
    able = _can_act(st)
    if len(_active(st)) <= 1:
        return
    if not able:
        st["to_act"] = None
        return
    # walk forward from current to_act to the next able player
    seats = st["seats"]
    if st["to_act"] not in able:
        i = seats.index(st["to_act"]) if st["to_act"] in seats else st["button"]
        for k in range(1, len(seats) + 1):
            cand = seats[(i + k) % len(seats)]
            if cand in able:
                st["to_act"] = cand
                break


def legal_actions(st, p):
    """What `p` may do right now: subset of fold/check/call/raise/allin, plus call/min-raise sizes."""
    if st["status"] != "betting" or st["to_act"] != p:
        return {}
    call = max(0, st["to_call"] - st["street_bet"][p])
    stack = st["stacks"][p]
    acts = {"fold": True, "allin": stack > 0}
    if call == 0:
        acts["check"] = True
    else:
        acts["call"] = min(call, stack)
    # a raise must reach to_call + min_raise (or be an all-in for less)
    if stack > call:
        acts["raise_to_min"] = st["to_call"] + st["min_raise"]
        acts["raise_to_max"] = st["street_bet"][p] + stack   # all-in cap
    return acts


def act(st, p, action, amount=None):
    """Apply one action. Returns (state, events). Events: 'flop'/'turn'/'river'/'showdown'/'folded_win'."""
    events = []
    if st["status"] != "betting":
        return st, events
    if st["to_act"] != p:
        return st, events
    action = (action or "").lower()
    call = max(0, st["to_call"] - st["street_bet"][p])
    stack = st["stacks"][p]

    if action == "fold":
        st["folded"].append(p)
    elif action in ("check",) and call == 0:
        pass
    elif action in ("call", "check") :
        _post(st, p, min(call, stack))
    elif action == "allin":
        put = stack
        was_to_call = st["to_call"]
        _post(st, p, put)
        if st["street_bet"][p] > was_to_call:           # all-in that raises
            inc = st["street_bet"][p] - was_to_call
            if inc >= st["min_raise"]:
                st["min_raise"] = inc
            st["to_call"] = st["street_bet"][p]
            st["last_raiser"] = p
            st["acted"] = []                            # re-open action
    elif action == "raise":
        target = int(amount or 0)                       # raise TO this street total
        target = max(target, st["to_call"] + st["min_raise"])
        need = target - st["street_bet"][p]
        if need >= stack:                               # not enough → treat as all-in
            return act(st, p, "allin")
        inc = target - st["to_call"]
        _post(st, p, need)
        st["min_raise"] = max(st["min_raise"], inc)
        st["to_call"] = target
        st["last_raiser"] = p
        st["acted"] = []
    else:
        return st, events                               # illegal/no-op

    if p not in st["acted"]:
        st["acted"].append(p)

    # everyone folded but one → that player wins the whole pot, no showdown.
    if len(_active(st)) == 1:
        _award_folded(st)
        events.append("folded_win")
        return st, events

    # round closes when every able player has acted AND matched to_call (or is all-in).
    able = _can_act(st)
    matched = all(st["street_bet"][q] == st["to_call"] or q in st["allin"] for q in _active(st))
    everyone_acted = all(q in st["acted"] for q in able)
    if matched and (everyone_acted or not able):
        _advance_street(st, events)
    else:
        # next to act
        seats = st["seats"]; i = seats.index(p)
        for k in range(1, len(seats) + 1):
            cand = seats[(i + k) % len(seats)]
            if cand in able:
                st["to_act"] = cand
                break
        _skip_done(st)
    return st, events


def _advance_street(st, events):
    # if ≤1 player can still act but ≥2 are live (all-ins), run out the board to showdown.
    while True:
        if st["street"] == "river":
            _showdown(st); events.append("showdown"); return
        nxt = STREETS[STREETS.index(st["street"]) + 1]
        st["street"] = nxt
        need = _BOARD_BY_STREET[nxt]
        while len(st["board"]) < need:
            st["board"].append(st["deck"].pop())
        events.append(nxt)
        # reset street betting
        for q in st["seats"]:
            st["street_bet"][q] = 0
        st["to_call"] = 0; st["min_raise"] = st["bb"]; st["last_raiser"] = None; st["acted"] = []
        able = _can_act(st)
        if len(able) >= 2:
            # action starts left of button
            seats = st["seats"]
            for k in range(1, len(seats) + 1):
                cand = seats[(st["button"] + k) % len(seats)]
                if cand in able:
                    st["to_act"] = cand
                    break
            return
        # else: nobody (or one) can act → keep dealing to the river, then showdown.


def _award_folded(st):
    winner = _active(st)[0]
    pot = sum(st["contrib"].values())
    st["stacks"][winner] += pot
    st["winners"] = {winner: pot}
    st["status"] = "done"
    st["to_act"] = None


def _showdown(st):
    live = _active(st)
    ranks = {p: evaluate7(st["hole"][p] + st["board"]) for p in live}
    won = distribute_pot(st["contrib"], live, ranks)
    for p, c in won.items():
        st["stacks"][p] += c
    st["winners"] = {p: c for p, c in won.items() if c > 0}
    st["ranks"] = {p: hand_name(ranks[p]) for p in live}
    st["status"] = "done"
    st["to_act"] = None
