"""Compaction carries a segment's markers forward without a lookup for the records it just moved.

Measured on server1 2026-10-18 (py-spy, 60 s): ~17% of the relay's CPU was `compact` -> `_seq_of` -> the on-disk id
index, one search per marker in the segment, all of it under the store lock (every query waited behind it). A
derived tag sits beside its own record, so nearly every marker names a record the same pass moved -- whose answer is
known: live, now in a later segment, tags already re-emitted. Asserted on behaviour (tags and deletions survive a
compaction + restart) and on cost (no lookup for a moved record's markers).
"""
from app.services.posterchandb.store import Store
from tests.test_posterchandb import hx, mk
from tests.test_posterchandb_maintenance import NOW, _fill, ids


def test_moved_records_markers_need_no_lookup_and_still_survive(tmp_path):
    p = str(tmp_path / "db")
    s = Store(p, flush_interval=3600, direct_durable=False, segment_bytes=16384)
    in_seg1 = []
    for i in range(40):                      # each record's tag written right after it: same segment
        e = mk(kind=1, created_at=NOW - i, content=hx(300))
        s.put(e, origin="wot")
        s.add_derived_tag(e["id"], "_quote_author", "q" * 64)
        if s.seg[s.seq_of(e["id"])] == 1:
            in_seg1.append(e)
    assert len(in_seg1) >= 5 and any(m[0] == 3 for m in s._markers_of(1)), "no tag markers in segment 1"
    _fill(s, 60)
    s.flush()
    calls = []
    real = s._seq_of

    def counting(eid):
        calls.append(eid)
        return real(eid)
    s._seq_of = counting
    s.compact(sid=1, pace=lambda *a: None)
    tagged = {e["id"] for e in in_seg1}
    assert not tagged & set(calls), "compaction looked up %d records it had just moved" % len(tagged & set(calls))
    s.close()
    s = Store(p)
    quoted = {e["id"] for e in s.query({"#p": ["q" * 64], "_include_quotes": True, "limit": 500}, now=NOW)}
    assert tagged <= quoted, "a derived tag was lost when its record moved"
    assert tagged <= ids(s)
    s.close()
