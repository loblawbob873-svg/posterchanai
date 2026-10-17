"""A delivery tick re-reads only the last two minutes; the FULL trailing window once every 15 minutes.

Measured on server1 2026-10-16: every 30-second tick re-read the whole overlap -- six hours of members' events
(~25,000 operator kind-5s among them) and 30 minutes of the firehose (6,725 events) for the everyone pass -- which
was most of what the relay did all day. A late arrival (signed offline, relayed late) must still federate: it is
picked up by the next full re-read. What was already sent inside the full window is still never sent twice.
"""
import time

from app.services import nostr_store
from app.services.activitypub import actors, outbox, remote, state
from tests.test_activitypub import ALICE, member_post, run
from tests.test_activitypub import world  # noqa: F401  (fixture)
from tests.test_activitypub_delivery_never_stalls import FAST, Relay, _followers

HOUR = 3600


def test_ticks_read_two_minutes_and_a_late_post_still_goes_out(world, monkeypatch):  # noqa: F811
    now = int(time.time())
    first = member_post("on time", created=now - 30)
    relay = Relay([first])
    asked = []
    real_query = relay.query

    async def query(port, filters, strict=False, **kw):
        if filters[0].get("authors") and "since" in filters[0]:
            asked.append(filters[0]["since"])
        return await real_query(port, filters, strict=strict, **kw)
    monkeypatch.setattr(nostr_store, "_ws_query", query)
    monkeypatch.setattr(actors, "all_actors", lambda: [ALICE])
    world["settings"]["activitypub_everyone"] = "false"
    world["docs"][state.CURSOR] = {"since": now - 60}
    _followers(world)
    sent = world["sent"]

    async def deliver(inbox_url, activity, *, key_id, private_pem):
        sent.append({"inbox": inbox_url, "activity": activity})
        return 202
    monkeypatch.setattr(remote, "deliver", deliver)

    def created(note):
        return sum(1 for s in sent if s["inbox"] == FAST and s["activity"]["type"] == "Create"
                   and s["activity"]["object"]["id"].endswith(note["id"]))

    run(outbox.tick())                                       # first tick of a process: the full window
    assert asked and min(asked) <= now - 60 - 6 * HOUR + 5, asked
    assert created(first) == 1

    asked.clear()
    late = member_post("signed offline, relayed two hours late", created=now - 2 * HOUR)
    relay.__init__(relay.events + [late])
    run(outbox.tick())                                       # an ordinary tick: two minutes, not six hours
    assert asked and min(asked) >= now - 60 - outbox._SHORT_OVERLAP - 5, ("a tick re-read the six-hour window", asked)
    assert created(late) == 0

    outbox._last_full[state.CURSOR] -= outbox._FULL_OVERLAP_EVERY + 1   # 15 minutes later
    asked.clear()
    run(outbox.tick())
    assert min(asked) <= now - 6 * HOUR, asked
    assert created(late) == 1, "the late post never federated"

    outbox._seen.clear()                                     # a restart: memory gone, the cursor doc's `done` kept
    outbox._last_full.clear()
    run(outbox.tick())
    assert created(first) == 1 and created(late) == 1, "a full re-read after a restart sent something twice"
