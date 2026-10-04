"""A flood of gift wraps at one person is cut off; private messages and Concord rooms are not.

"Someone is spamming kind 1059 gift-wrap events right now. About 13k per minute. And 96%+ of them are all
being directed at one npub" -- and the relay accepted ANY gift wrap written to it (Concord's wraps carry
random cover tags, so no recipient test applies) and pulled any wrap addressed to a member; one member
here had already been sent 32,262. The budget is per recipient, by ARRIVAL time (NIP-59 backdates
created_at by up to two days), default 300 per 10 minutes. Runs the shipped guard and the relay's own
event path with real signed events.
"""
import asyncio
import inspect
import os
import random
import time

from app.services.nostr.event import build_event
from app.services.nostr_relay import thread
from app.services.nostr_relay.spamguard import GIFT_WINDOW, SpamGuard
from tests.test_relay_new_post_limits import _server

VICTIM = "d0" * 32


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t
    def __call__(self):
        return self.t


def wrap(to=VICTIM, backdate=0):
    # A real NIP-59 wrap is signed by a fresh throwaway key and backdated by a random amount.
    return build_event(os.urandom(32), 1059, "ciphertext", [["p", to]],
                       created_at=int(time.time()) - backdate)


def test_a_flood_at_one_recipient_stops_at_the_budget_and_backdating_does_not_spread_it():
    clock = Clock()
    g = SpamGuard({}, clock=clock)
    out = [g.check_gift(wrap(backdate=random.randint(0, 2 * 86400))) for _ in range(1000)]
    assert out[:300] == [""] * 300, "a normal inbox's worth was refused"
    assert all(r.startswith("rate-limited:") for r in out[300:]), "the flood got past the budget"


def test_other_recipients_are_untouched_and_the_next_window_opens_again():
    clock = Clock()
    g = SpamGuard({}, clock=clock)
    for _ in range(300):
        g.check_gift(wrap())
    assert g.check_gift(wrap()).startswith("rate-limited:")
    assert g.check_gift(wrap(to="ab" * 32)) == "", "somebody else's DMs were held up by the flood"
    clock.t += GIFT_WINDOW
    assert g.check_gift(wrap()) == "", "the budget never came back"


def test_concord_rooms_random_cover_tags_are_never_limited():
    g = SpamGuard({}, clock=Clock())
    out = [g.check_gift(wrap(to=os.urandom(32).hex())) for _ in range(2000)]
    assert set(out) == {""}


def test_zero_turns_it_off_and_other_kinds_are_never_counted():
    g = SpamGuard({"gift_per_recipient": 0}, clock=Clock())
    assert {g.check_gift(wrap()) for _ in range(1000)} == {""}
    g = SpamGuard({}, clock=Clock())
    dm4 = build_event(os.urandom(32), 4, "x", [["p", VICTIM]])
    assert {g.check_gift(dm4) for _ in range(1000)} == {""}


def test_a_flood_written_to_this_relay_is_refused_and_told_why():
    srv = _server()
    srv.spam.clock = Clock()

    async def run():
        for _ in range(400):
            await srv._on_event("you", wrap(backdate=random.randint(0, 172800)))
    asyncio.new_event_loop().run_until_complete(run())
    oks = [m for _c, m in srv.sent if isinstance(m, list) and m[0] == "OK"]
    assert sum(1 for m in oks if m[2] is True) == 300, "the relay stored past the budget"
    assert sum(1 for m in oks if m[2] is False and m[3].startswith("rate-limited:")) == 100
    assert len(srv.store.saved) == 300


def test_the_firehose_consults_it_too():
    """The pull path is where 32,262 wraps reached one member; it must ask the same guard."""
    assert "_spam.check_gift(ev)" in inspect.getsource(thread)
