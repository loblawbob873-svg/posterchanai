"""Texas Hold'em poker engine — pure, deterministic, no Nostr/IO (so it's unit-testable offline).

The listener (holdemListener.py) owns the Nostr plumbing + betting-round turn state; this module
owns the parts that MUST be correct: shuffling, 7-card hand evaluation, and pot/side-pot payout.

Cards are ints 0..51: rank = card % 13 (0=2 … 12=A), suit = card // 13. `evaluate7` returns a
comparable tuple (bigger = better), so ranking/showdown is just `max`/sort over those tuples.
"""

import secrets
from itertools import combinations

RANKS = "23456789TJQKA"
SUITS = "♠♥♦♣"

# hand categories (higher = stronger)
HIGH, PAIR, TWO_PAIR, TRIPS, STRAIGHT, FLUSH, FULL_HOUSE, QUADS, STRAIGHT_FLUSH = range(9)
CATEGORY_NAME = {
    HIGH: "high card", PAIR: "pair", TWO_PAIR: "two pair", TRIPS: "three of a kind",
    STRAIGHT: "straight", FLUSH: "flush", FULL_HOUSE: "full house", QUADS: "four of a kind",
    STRAIGHT_FLUSH: "straight flush",
}


def rank_of(card):
    return card % 13


def suit_of(card):
    return card // 13


def card_str(card):
    return RANKS[card % 13] + SUITS[card // 13]


def new_deck():
    """A freshly shuffled 52-card deck (secrets-based; Math.random-free per the env constraints)."""
    deck = list(range(52))
    for i in range(len(deck) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        deck[i], deck[j] = deck[j], deck[i]
    return deck


def _eval5(cards):
    """Rank exactly 5 cards → a comparable tuple (category, *tiebreakers), bigger = better."""
    ranks = sorted((c % 13 for c in cards), reverse=True)
    suits = [c // 13 for c in cards]
    flush = len(set(suits)) == 1

    # straight: unique consecutive ranks, with the wheel (A-2-3-4-5) special-cased.
    uniq = sorted(set(ranks), reverse=True)
    straight_high = None
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            straight_high = uniq[0]
        elif uniq == [12, 3, 2, 1, 0]:        # A,5,4,3,2 → 5-high wheel
            straight_high = 3

    # rank multiplicities, ordered by (count, rank) desc so kickers compare correctly.
    counts = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    by = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    shape = [c for _, c in by]               # e.g. [3,2]=full house, [2,2,1]=two pair
    ordered = [r for r, _ in by]             # ranks ordered by strength

    if straight_high is not None and flush:
        return (STRAIGHT_FLUSH, straight_high)
    if shape[0] == 4:
        return (QUADS, ordered[0], ordered[1])
    if shape[0] == 3 and shape[1] == 2:
        return (FULL_HOUSE, ordered[0], ordered[1])
    if flush:
        return (FLUSH, *ranks)
    if straight_high is not None:
        return (STRAIGHT, straight_high)
    if shape[0] == 3:
        return (TRIPS, ordered[0], ordered[1], ordered[2])
    if shape[0] == 2 and shape[1] == 2:
        return (TWO_PAIR, ordered[0], ordered[1], ordered[2])
    if shape[0] == 2:
        return (PAIR, ordered[0], ordered[1], ordered[2], ordered[3])
    return (HIGH, *ranks)


def evaluate7(cards):
    """Best 5-card rank tuple out of up to 7 cards (2 hole + up to 5 community)."""
    cards = list(cards)
    if len(cards) < 5:
        # pre-flop / partial board: pad-free best-of-available (still comparable within a street)
        return _eval5(sorted(cards, reverse=True)[:5]) if len(cards) >= 5 else (HIGH, *sorted((c % 13 for c in cards), reverse=True))
    return max(_eval5(c) for c in combinations(cards, 5))


def hand_name(rank_tuple):
    return CATEGORY_NAME.get(rank_tuple[0], "?")


def distribute_pot(contrib, in_showdown, ranks):
    """Split the pot — including ALL-IN SIDE POTS — among winners.

    contrib       : {player -> total chips they put in this hand} (everyone, incl. folders)
    in_showdown   : set/list of players eligible to win (didn't fold)
    ranks         : {player -> evaluate7 tuple} for players in_showdown

    Returns {player -> chips won}. Builds side pots at each distinct all-in/contribution level so a
    short-stacked all-in player can only win the portion they matched. Odd chips go to the earliest
    eligible winner (deterministic). Verified to conserve chips (sum out == sum in)."""
    contrib = {p: int(v) for p, v in contrib.items() if v > 0}
    elig = set(in_showdown)
    won = {p: 0 for p in contrib}
    levels = sorted(set(contrib.values()))
    prev = 0
    for lvl in levels:
        # this side pot covers the slice (prev, lvl] across everyone who contributed at least `lvl`.
        contributors = [p for p, v in contrib.items() if v >= lvl]
        pot = (lvl - prev) * len(contributors)
        prev = lvl
        if pot <= 0:
            continue
        # eligible winners for THIS pot: didn't fold AND contributed enough to reach this slice.
        pot_elig = [p for p in contributors if p in elig]
        if not pot_elig:
            # everyone who could win this slice folded → refund to the contributors of this slice
            # (rare: all-in then all fold). Give it back proportionally-ish to the highest contributor.
            best = max(contributors, key=lambda p: contrib[p])
            won[best] += pot
            continue
        best_rank = max(ranks[p] for p in pot_elig)
        winners = [p for p in pot_elig if ranks[p] == best_rank]
        share, odd = divmod(pot, len(winners))
        for p in winners:
            won[p] += share
        if odd:
            won[winners[0]] += odd      # odd chip to first (seat-order) winner
    return won
