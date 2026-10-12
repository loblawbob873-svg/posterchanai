"""OUTBOUND FEDERATION CANNOT STALL SILENTLY.

2026-10-11, measured on server1: ActivityPub delivery stalled for ~23 hours. ~9,400 operator kind-5s
a day (the node's DocTables deleting their app documents -- the operator is a member there) filled the
delivery window; every tick ran out of the worker's 240-second limit before its pass saved the cursor,
so the next tick started from the same place; the newest-first read was capped at 10,000 events, so
the oldest ~16 hours of real member posts, likes and a report fell out of it altogether; and the only
trace was one INFO line in the WORKER's journal. Nobody was told.

These run the SHIPPED tick, under the worker's real `wait_for` (the tick length is scaled down -- every
budget inside it is a fraction of that one number), against a relay holding the PRODUCTION SHAPE of
that backlog: a flood of operator kind-5 app-document deletions over 29 hours, real member posts and
a report spread across the stall, one fast, one slow and one dead follower inbox, and a full retry
queue. Each was checked to fail against the pre-fix outbox (see the commit message).
"""
import asyncio
import bisect
import time

import pytest

from app.services import nostr_store
from app.services.activitypub import actors, health, outbox, remote, state
from tests.test_activitypub import ALICE, member_post, run
from tests.test_activitypub import world  # noqa: F401  (fixture)
from tests.test_activitypub import _mirrored_carol_note
from tests.test_activitypub_flag import CAROL_INBOX, MIRROR_ID, _report

FAST = "https://fast.example/inbox"
SLOW = "https://slow.example/inbox"
DEAD = "https://dead.example/inbox"
HOUR = 3600


class Relay:
    """`nostr_store._ws_query` over a fixed event set, honouring ids/kinds/authors/since/until/limit and
    answering NEWEST FIRST, as the relay does."""

    def __init__(self, events):
        self.events = sorted(events, key=lambda e: (e["created_at"], e["id"]))
        self.times = [e["created_at"] for e in self.events]
        self.by_id = {e["id"]: e for e in self.events}
        self.queries = 0

    async def query(self, port, filters, strict=False, **kw):
        self.queries += 1
        f = filters[0]
        if f.get("ids"):
            return [self.by_id[i] for i in f["ids"] if i in self.by_id]
        lo = bisect.bisect_left(self.times, f.get("since", 0))
        hi = bisect.bisect_right(self.times, f["until"]) if "until" in f else len(self.events)
        kinds, authors, out = f.get("kinds"), f.get("authors"), []
        for e in reversed(self.events[lo:hi]):
            if (kinds is None or e["kind"] in kinds) and (authors is None or e["pubkey"] in authors):
                out.append(e)
                if len(out) >= f.get("limit", 500):
                    break
        return out


def _production_backlog(world, now, *, junk=15_000, posts=120):
    """The stall window as it was: the cursor 23h back, operator kind-5s deleting app documents every
    few seconds from 6h before it to now, real posts across the whole stall, the report near its start."""
    cursor = now - 23 * HOUR
    start = cursor - 6 * HOUR
    evs = []
    for i in range(junk):
        t = start + (now - start) * i // junk
        evs.append(member_post("", kind=5, created=t,
                               tags=[["e", f"{i:064x}"], ["a", f"30078:{ALICE}:pcai:t:fedi_ledger_index:u:{i:x}"]]))
    real = [member_post(f"post {i}", created=cursor + 60 + (now - cursor - 180) * i // posts) for i in range(posts)]
    carol = _mirrored_carol_note(world)
    report = _report([["e", MIRROR_ID, "spam"], ["p", carol, "spam"]], created=cursor + 120)
    for e in real + [report]:
        world["relay"][e["id"]] = e
    return cursor, evs + real + [report], real, report


def _followers(world):
    for n, inbox in enumerate((FAST, SLOW, DEAD)):
        world["docs"][f"pcai:ap:follower:{ALICE}:{n}"] = {"actor": f"{inbox[:-6]}/users/f{n}", "inbox": inbox}


@pytest.fixture
def stalled(world, monkeypatch):
    """The world of tests/test_activitypub.py with the production backlog and a scaled tick."""
    now = int(time.time())
    cursor, events, real, report = _production_backlog(world, now)
    relay = Relay(events)
    monkeypatch.setattr(nostr_store, "_ws_query", relay.query)
    monkeypatch.setattr(actors, "all_actors", lambda: [ALICE])
    monkeypatch.setattr(outbox, "_TICK_SECONDS", 0.5)        # the worker's limit = 4s instead of 240s
    monkeypatch.setattr(outbox, "_SAVE_EVERY", 0.25)
    monkeypatch.setattr(outbox, "MAX_RETRIES_QUEUED", 400)
    world["settings"]["activitypub_everyone"] = "false"
    world["docs"][state.CURSOR] = {"since": cursor}
    _followers(world)
    sent = world["sent"]

    async def deliver(inbox_url, activity, *, key_id, private_pem):
        if inbox_url == SLOW:
            await asyncio.sleep(1.2)                         # answers, eventually -- longer than an event's window
        elif inbox_url == DEAD:
            await asyncio.sleep(0.3)                         # never answers
            return 0
        sent.append({"inbox": inbox_url, "activity": activity, "key_id": key_id})
        return 202
    monkeypatch.setattr(remote, "deliver", deliver)
    # A FULL retry queue, all of it due now, all of it for the slow server.
    filler = {"type": "Announce", "actor": "x"}
    outbox._retries[:] = [(0.0, SLOW, dict(filler, id=f"https://poster.test/ap/a/{i}"), ALICE, 1, f"k{i}")
                          for i in range(400)]
    outbox._retries_loaded = True
    outbox._retries_loaded_at = time.time()
    outbox._failures.clear()
    outbox._inflight.clear()
    health.reset()
    alerts = []

    async def send_admin_alert(text, **kw):
        alerts.append(text)
        return {"telegram": True, "nostr": None}
    from app.services import uptime_service
    monkeypatch.setattr(uptime_service, "send_admin_alert", send_admin_alert)
    monkeypatch.setattr(uptime_service, "_refresh_settings", lambda: None)
    return {"now": now, "cursor": cursor, "real": real, "report": report, "relay": relay, "alerts": alerts,
            "events": events}


def _created_to(sent, inbox):
    return {s["activity"]["object"]["id"].rsplit("/", 1)[-1] for s in sent
            if s["inbox"] == inbox and s["activity"]["type"] == "Create"}


def test_the_production_backlog_is_delivered_and_the_cursor_reaches_the_present(world, stalled):
    newest = max(e["created_at"] for e in stalled["events"])      # the present, as the relay knows it

    async def ticks():
        verdicts = []
        for _ in range(6):
            verdicts.append(await outbox.run_tick())
            if world["docs"][state.CURSOR]["since"] >= newest:
                break
        # Let anything handed off to the background finish, as the worker's loop would.
        await asyncio.gather(*list(outbox._inflight), return_exceptions=True)
        return verdicts
    verdicts = run(ticks())
    sent = world["sent"]
    real_ids = {e["id"] for e in stalled["real"]}
    missing = real_ids - _created_to(sent, FAST)
    assert not missing, (f"{len(missing)} of {len(real_ids)} member posts never reached a working follower "
                         f"-- the oldest {min(e['created_at'] for e in stalled['real'] if e['id'] in missing) - stalled['cursor']}s "
                         "after the stalled cursor")
    flags = [s for s in sent if s["activity"]["type"] == "Flag"]
    assert len(flags) == 1 and flags[0]["inbox"] == CAROL_INBOX, "the report filed during the stall never went out"
    assert not [s for s in sent if s["activity"]["type"] == "Delete"], "an app document's deletion was federated"
    assert world["docs"][state.CURSOR]["since"] >= newest, "the cursor never reached the present"
    assert len(verdicts) <= 4, f"took {len(verdicts)} ticks"
    # The slow server is OWED what it did not get -- delivered, or in the persistent retry queue.
    owed = {r[2]["object"]["id"].rsplit("/", 1)[-1] for r in outbox._retries
            if r[1] == SLOW and r[2].get("type") == "Create"}
    assert real_ids <= _created_to(sent, SLOW) | owed, "a post owed to the slow server was lost"
    assert verdicts[-1]["state"] == "ok", verdicts[-1]


def test_a_flood_of_one_kind_never_pushes_posts_out_of_the_read(world, stalled, monkeypatch):
    """The capped newest-first read handed over only the newest 10,000 events of the window; everything
    older -- here, the report and the first posts of the stall -- was never sent, and the cursor moved past
    it. No time pressure in this one: the READ alone must not drop anything."""
    monkeypatch.setattr(outbox, "_TICK_SECONDS", 30)
    monkeypatch.setattr(remote, "deliver", _fast_deliver(world))
    run(outbox.tick())
    sent = world["sent"]
    missing = {e["id"] for e in stalled["real"]} - _created_to(sent, FAST)
    assert not missing, f"{len(missing)} posts were dropped by the read"
    assert [s for s in sent if s["activity"]["type"] == "Flag"], "the report at the start of the stall was dropped"


def _fast_deliver(world):
    async def deliver(inbox_url, activity, *, key_id, private_pem):
        world["sent"].append({"inbox": inbox_url, "activity": activity, "key_id": key_id})
        return 202
    return deliver


def test_a_pass_cut_off_by_the_hard_limit_keeps_its_place(world, stalled, monkeypatch):
    """Real cancellation, mid-send: the worker's wait_for fires while an event's sends are in flight. The
    cursor must hold everything finished before it, and nothing after."""
    real_plan = outbox.plan
    target = stalled["real"][40]

    async def plan(ev, member):
        if ev["id"] == target["id"]:
            await asyncio.sleep(3600)
        return await real_plan(ev, member)
    monkeypatch.setattr(outbox, "plan", plan)
    monkeypatch.setattr(outbox, "_plan_limit", lambda: 3600, raising=False)   # only the hard limit can stop it
    monkeypatch.setattr(remote, "deliver", _fast_deliver(world))

    async def go():
        try:
            await asyncio.wait_for(outbox.tick(), timeout=3)
        except asyncio.TimeoutError:
            pass
    run(go())
    since = world["docs"][state.CURSOR]["since"]
    assert since >= stalled["real"][39]["created_at"], "work finished before the cut-off was lost"
    # The cursor is inclusive of its second (re-read with the overlap), so EQUAL still holds the event.
    assert since <= target["created_at"], "the cursor moved past an event that was never sent"
    assert target["id"] not in outbox._seen


# ------------------------------------------------------------------------------------ the alarm

def _drive(world, monkeypatch, readings):
    """after_tick over a scripted sequence of (tick ok?, oldest unsent created_at or None)."""
    from app.services import uptime_service
    alerts = []

    async def send_admin_alert(text, **kw):
        alerts.append(text.split("\n")[0])
        return {"telegram": True, "nostr": True}
    monkeypatch.setattr(uptime_service, "send_admin_alert", send_admin_alert)
    monkeypatch.setattr(uptime_service, "_refresh_settings", lambda: None)
    world["docs"][state.CURSOR] = {"since": 1}
    monkeypatch.setattr(actors, "all_actors", lambda: [ALICE])
    out = []
    for ok, oldest in readings:
        async def canary(cursor, members, _o=oldest):
            return _o
        monkeypatch.setattr(health, "oldest_unsent", canary)
        out.append(run(health.after_tick({"delivered": 0, "last_error": "" if ok else "TimeoutError"},
                                         ok=ok, why="" if ok else "TimeoutError: ")))
    return alerts, out


def test_a_stall_is_alerted_once_and_its_recovery_once(world, monkeypatch):
    health.reset()
    now = int(time.time())
    behind = now - 23 * HOUR
    alerts, verdicts = _drive(world, monkeypatch,
                              [(True, 0)] * 3 + [(True, behind)] * 10 + [(True, 0)] * 10)
    assert [v["state"] for v in verdicts[:3]] == ["ok"] * 3
    assert verdicts[3]["state"] == "behind"
    assert alerts == ["🔴 FEDERATION STALLED — poster.test", "🟢 FEDERATION RECOVERED — poster.test"], alerts
    # A worker restart in the MIDDLE of a stall (every deploy restarts it) does not tell the admin a
    # second time: what they were told is read back from the report document.
    alerts, _ = _drive(world, monkeypatch, [(True, behind)] * 3)
    assert alerts == ["🔴 FEDERATION STALLED — poster.test"]
    health.reset()
    alerts, _ = _drive(world, monkeypatch, [(True, behind)] * 3)
    assert alerts == [], "the stall was re-announced after a restart"
    alerts, _ = _drive(world, monkeypatch, [(True, 0)] * 4)
    assert alerts == ["🟢 FEDERATION RECOVERED — poster.test"]


def test_failing_ticks_raise_the_alarm_even_when_nothing_can_be_measured(world, monkeypatch):
    """A relay that cannot be asked makes every tick fail AND the canary unanswerable: "could not ask" is
    never "caught up", so the failures alone must raise it."""
    health.reset()
    alerts, verdicts = _drive(world, monkeypatch, [(False, None)] * 8)
    assert verdicts[0]["state"] == "unknown"
    assert verdicts[-1]["state"] == "failing"
    assert alerts == ["🔴 FEDERATION STALLED — poster.test"]


def test_an_alert_nobody_received_is_tried_again_later_not_every_tick(world, monkeypatch):
    health.reset()
    from app.services import uptime_service
    tries = []

    async def broken(text, **kw):
        tries.append(1)
        return {"telegram": False, "nostr": False}
    monkeypatch.setattr(uptime_service, "send_admin_alert", broken)
    monkeypatch.setattr(uptime_service, "_refresh_settings", lambda: None)
    monkeypatch.setattr(actors, "all_actors", lambda: [ALICE])
    world["docs"][state.CURSOR] = {"since": 1}

    async def canary(cursor, members):
        return int(time.time()) - 23 * HOUR
    monkeypatch.setattr(health, "oldest_unsent", canary)
    for _ in range(5):
        run(health.after_tick({}, ok=True))
    assert tries == [1], "a failed alert was re-sent on every tick"


# ------------------------------------------------------------------------------------ the line

def test_the_health_line_is_red_when_delivery_is_far_behind():
    now = int(time.time())
    doc = {"delivered": 5, "at": now, "last_error": "TimeoutError",
           "health": {"state": "behind", "lag": 23 * HOUR, "fails": 0, "error": "TimeoutError"}}
    v = health.view(doc, now)
    assert v["ok"] is False and v["text"] == "Delivery 23h behind — last error TimeoutError", v
    assert health.board_row(doc, now) == ("red", "23h behind — last error TimeoutError")
    from app.services.logs_scheduler import _render_board
    assert "🌐 Federation: 🔴 23h behind" in _render_board("", {"federation": health.board_row(doc, now)})
    ok = {"at": now, "health": {"state": "ok", "lag": 0, "fails": 0, "per_min": 4}}
    assert health.view(ok, now) == {"ok": True, "state": "ok", "text": "Delivery: caught up · 4 sent/min"}
    # A report the worker stopped writing is itself the alarm: nobody is delivering.
    stale = dict(ok, at=now - 2 * HOUR)
    v = health.view(stale, now)
    assert v["ok"] is False and "no report from the delivery worker for 2h" in v["text"]


def test_the_admin_status_carries_the_line(world):
    now = int(time.time())
    world["docs"][health.DOC] = {"at": now, "health": {"state": "behind", "lag": 23 * HOUR, "error": "TimeoutError"}}
    from app.routers.activitypub import admin_status
    j = run(admin_status(user=None))
    assert j["delivery_health"]["ok"] is False
    assert j["delivery_health"]["text"].startswith("Delivery 23h behind")


def test_the_canary_sees_a_waiting_post_and_not_a_deleted_app_document(world, monkeypatch):
    now = int(time.time())
    junk = member_post("", kind=5, created=now - 2 * HOUR, tags=[["a", f"30078:{ALICE}:pcai:x"]])
    relay = Relay([junk])
    monkeypatch.setattr(nostr_store, "_ws_query", relay.query)
    assert run(health.oldest_unsent(now - 3 * HOUR, [ALICE])) == 0, "an app document's deletion read as unsent"
    post = member_post("waiting", created=now - 2 * HOUR)
    monkeypatch.setattr(nostr_store, "_ws_query", Relay([junk, post]).query)
    assert run(health.oldest_unsent(now - 3 * HOUR, [ALICE])) == post["created_at"]

    async def down(*a, **k):
        raise ConnectionError("relay down")
    monkeypatch.setattr(nostr_store, "_ws_query", down)
    assert run(health.oldest_unsent(now - 3 * HOUR, [ALICE])) is None, "could not ask read as caught up"


# ------------------------------------------------------------------------------------ the repair

def test_redeliver_sends_what_the_stall_stranded_and_nothing_twice(world, stalled, monkeypatch):
    """After the capped reader moved the cursor past them, the stranded events are sent by hand
    (scripts/ap_redeliver.py). What already went out is not sent again: an event in the cursor's `done`
    set, one with a `pcai:ap:sent:` record (the report), and anything a previous run sent."""
    monkeypatch.setattr(remote, "deliver", _fast_deliver(world))
    real = stalled["real"]
    already = real[-10:]                             # what the post-fix pass delivered itself
    world["docs"][state.CURSOR] = {"since": stalled["now"],
                                   "done": {e["id"][:16]: e["created_at"] for e in already}}
    world["docs"]["pcai:ap:sent:" + stalled["report"]["id"]] = {"inbox": CAROL_INBOX, "inboxes": [CAROL_INBOX],
                                                               "kind": 1984, "by": ALICE}
    out = run(outbox.redeliver(stalled["cursor"]))
    sent = world["sent"]
    got = _created_to(sent, FAST)
    assert {e["id"] for e in real[:-10]} <= got, "a stranded post was not delivered"
    assert not got & {e["id"] for e in already}, "a post the pass had already sent went out twice"
    assert not [s for s in sent if s["activity"]["type"] == "Flag"], "the report was filed a second time"
    assert not [s for s in sent if s["activity"]["type"] == "Delete"], "app-document deletions went out"
    assert out["delivered"] == len(real) - 10 and out["already_sent"] == 10 and out["recorded"] == 1, out
    # A second run of the same window sends nothing at all.
    sent.clear()
    again = run(outbox.redeliver(stalled["cursor"], until=out["window"][1]))
    assert sent == [] and again["delivered"] == 0, again


def test_redeliver_dry_run_sends_nothing(world, stalled, monkeypatch):
    monkeypatch.setattr(remote, "deliver", _fast_deliver(world))
    out = run(outbox.redeliver(stalled["cursor"], dry_run=True))
    assert world["sent"] == [] and out["by_kind"].get("1") == len(stalled["real"])


def test_the_capped_read_boundary_is_where_the_old_reader_stopped(world, stalled):
    """`--boundary-at` replays the OLD capped read: everything older than this was never handed over."""
    b = run(outbox.capped_read_boundary(stalled["cursor"], stalled["now"]))
    newest_first = sorted((e for e in stalled["events"] if e["created_at"] >= stalled["cursor"] - 6 * HOUR),
                          key=lambda e: -e["created_at"])
    cap = outbox._MAX_PAGES * outbox._PAGE
    # (Each page re-reads the second it ended on, so a capped read holds a few fewer than `cap` events.)
    assert newest_first[cap - 1]["created_at"] <= b <= newest_first[cap - 50]["created_at"]
    assert b > stalled["cursor"] + 3 * HOUR, "the production shape no longer reproduces the drop"


def test_the_redelivery_script_starts():
    """The repair is run by hand, rarely, under pressure: an import or argparse slip must show up here."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, str(root / "scripts" / "ap_redeliver.py"), "--help"],
                       capture_output=True, text=True, timeout=60, cwd=root)
    assert r.returncode == 0 and "--boundary-at" in r.stdout, r.stderr
