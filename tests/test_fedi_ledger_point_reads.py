"""The delivered-notes ledger answers by POINT reads, so the ActivityPub inbox never waits for it to load in whole.

Production 2026-10-10: the ledger holds ~390k rows and its whole-table load took ~20 minutes after a restart. The
inbox asked `fedi_tables.aready()`, which demanded that whole load, so every POST /ap/inbox answered 503 until it
finished. Each test here fails on the code before the fix:
  * a new activity is ACCEPTED (202) while the ledger has not been loaded in whole -- single-document reads work;
  * the inbox still answers 503 when a single document cannot be asked (never a 202 for work that might deliver
    a duplicate);
  * dedup still holds through the point lookups -- the inbox's own rows (deterministic key), a row recorded under
    another key (the retired Pleroma bridge's, found through the index after the backfill), and the event-id
    lookups the outbox uses;
  * forget and prune keep the index in step.
"""
import asyncio

import pytest

from app.services import fedi_tables
from app.services.doc_table import DocTable, Loading
from app.services.relay_reader import Unavailable
from tests.test_activitypub import (ALICE, REMOTE, _create, _post_signed, client, run, world)  # noqa: F401

URI = "https://mastodon.example/notes/1"
INDEX = "fedi_ledger_index"           # literal: the test must also RUN (and fail) against the code before it


def _ledger_never_loads(monkeypatch):
    """The ledger's whole-table load cannot finish (a big table, a busy relay); everything else loads."""
    real = DocTable.aload

    async def aload(self, *, force=False):
        if self.name == fedi_tables.LEDGER:
            raise Loading("table %s is still loading" % self.name)
        return await real(self, force=force)
    monkeypatch.setattr(DocTable, "aload", aload)
    monkeypatch.setattr(DocTable, "start_background_load", lambda self: None)


def _index_complete(mem):
    mem.put(INDEX, "_complete", {"ok": True})


def test_the_inbox_accepts_while_the_ledger_is_not_loaded_in_whole(client, world, monkeypatch):
    _index_complete(world["mem"])
    _ledger_never_loads(monkeypatch)
    r = _post_signed(client, _create())
    assert r.status_code == 202, "the inbox waited for the whole ledger"
    assert client.scheduled, "nothing was scheduled"
    assert not DocTable(fedi_tables.LEDGER).loaded


def test_the_inbox_answers_503_when_one_document_cannot_be_asked(client, world):
    _index_complete(world["mem"])
    run(fedi_tables.aready())                      # both tables were readable a moment ago...
    world["mem"].down = True                       # ...and now the relay cannot be asked
    r = _post_signed(client, _create())
    assert r.status_code == 503, "a 202 for work whose dedup could not be checked"
    assert client.scheduled == []


def test_a_note_is_stored_once_through_point_lookups(world, monkeypatch):
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    _index_complete(world["mem"])
    _ledger_never_loads(monkeypatch)
    assert run(inbox_process(_create())) == "stored"
    assert run(inbox_process(_create())) == "already stored"
    assert len([e for e in world["relay"].values() if e["kind"] == 1]) == 1
    ev = next(e for e in world["relay"].values() if e["kind"] == 1)
    assert fedi_tables.delivered_by_uri(URI).nostr_event_id == ev["id"]
    assert run(fedi_tables.amirrored_among([ev["id"], "0" * 64])) == {ev["id"]}
    assert run(fedi_tables.adelivered_by_event(ev["id"])).note_uri == URI
    assert not DocTable(fedi_tables.LEDGER).loaded, "a lookup loaded the whole ledger"


def inbox_process(act):
    from app.services.activitypub import inbox
    return inbox.process(act, REMOTE)


def test_a_row_from_before_the_index_is_found_after_the_backfill(world, monkeypatch):
    """A row recorded under another key (the Pleroma bridge's) has no deterministic key: the backfill indexes it,
    and only then does the inbox stop needing the whole ledger."""
    from app.models import FediBridgeDelivered
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    s = world["Session"]()
    s.add(FediBridgeDelivered(platform="pleroma", instance_url="https://pleroma.example", note_id="A1",
                              note_uri=URI, nostr_event_id="e" * 64, nostr_pubkey="f" * 64))
    s.commit()
    assert run(fedi_tables.abackfill_index()) == 2              # u:<uri> and e:<event>
    assert run(fedi_tables.abackfill_index()) == 0, "the backfill is once"
    DocTable._registry.clear()                                  # a fresh process: nothing held in memory
    _ledger_never_loads(monkeypatch)
    assert run(inbox_process(_create())) == "already stored"
    assert not [e for e in world["relay"].values() if e["kind"] == 1]
    assert run(fedi_tables.amirrored_among(["e" * 64])) == {"e" * 64}


def test_without_a_complete_index_the_lookups_still_refuse_rather_than_guess(world, monkeypatch):
    _ledger_never_loads(monkeypatch)
    with pytest.raises(Unavailable):
        fedi_tables.delivered_by_uri(URI)
    with pytest.raises(Unavailable):
        run(fedi_tables.aready())


def test_forget_and_prune_keep_the_index_in_step(world):
    mem = world["mem"]
    _index_complete(mem)

    async def go():
        await fedi_tables.arecord(platform="activitypub", instance_url="https://mastodon.example", note_id=URI,
                                  note_uri=URI, nostr_event_id="a" * 64)
        await fedi_tables.arecord(platform="activitypub", instance_url="https://mastodon.example",
                                  note_id=URI + "/2", note_uri=URI + "/2", nostr_event_id="b" * 64)
        assert (INDEX, "e:" + "a" * 64) in mem.rows
        assert await fedi_tables.aforget_uri(URI) == 1
        assert (INDEX, "e:" + "a" * 64) not in mem.rows
        assert await fedi_tables.adelivered_by_uri(URI) is None
        k = fedi_tables._ap_key(URI + "/2")
        row = dict(mem.rows[(fedi_tables.LEDGER, k)], created_at="2000-01-01T00:00:00")
        mem.put(fedi_tables.LEDGER, k, row)
        assert await fedi_tables.aprune(30) == 1
        assert (INDEX, "e:" + "b" * 64) not in mem.rows
        assert await fedi_tables.amirrored_among(["b" * 64]) == set()
    asyncio.run(go())


def test_the_index_table_name_is_the_shipped_one():
    assert fedi_tables.INDEX == INDEX


def test_repeated_questions_reach_the_relay_once_and_a_new_row_is_seen_at_once(world, monkeypatch):
    """~25,000 ledger point reads per 5 minutes reached production's relay (2026-10-17): the delivery pass asks about
    the same events every tick. Positive answers are kept; a negative one only where it is about something old; a
    row this process records is visible the moment it is written; the inbox's dedup never keeps a "no"."""
    _index_complete(world["mem"])
    fedi_tables.forget_cached()
    reads = []
    real = DocTable.aget_many

    async def counting(self, keys):
        reads.append(self.name)
        return await real(self, keys)
    monkeypatch.setattr(DocTable, "aget_many", counting)
    eid = "e" * 64
    run(fedi_tables.arecord(platform="activitypub", instance_url="https://mastodon.example", note_id=URI,
                            note_uri=URI, nostr_event_id=eid, nostr_pubkey="p" * 64))
    reads.clear()
    for _ in range(10):
        assert run(fedi_tables.amirrored_among([eid])) == {eid}
        assert run(fedi_tables.adelivered_by_uri(URI)).nostr_event_id == eid
    assert len(reads) <= 3, ("the same questions went to the relay every time", len(reads))   # each asked once
    # a "no" about a referenced event is kept briefly -- and dropped the moment this process records the row
    other = "f" * 64
    assert run(fedi_tables.amirrored_among([other])) == set()
    run(fedi_tables.arecord(platform="activitypub", instance_url="https://mastodon.example", note_id=URI + "x",
                            note_uri=URI + "x", nostr_event_id=other, nostr_pubkey="p" * 64))
    assert run(fedi_tables.amirrored_among([other])) == {other}, "a cached 'no' hid a row this process just wrote"
    # the inbox dedup never caches a "no": a note recorded by ANOTHER process is seen at once
    new = "https://mastodon.example/notes/new"
    assert run(fedi_tables.adelivered_by_uri(new)) is None
    world["mem"].put(fedi_tables.LEDGER, fedi_tables.ledger_key("activitypub", "https://mastodon.example", new),
                     {"platform": "activitypub", "instance_url": "https://mastodon.example", "note_id": new,
                      "note_uri": new, "nostr_event_id": "a" * 64, "created_at": "2026-10-17T00:00:00"})
    fedi_tables.forget_cached()   # the index entry is what another process writes; the point is only "no" was not kept
