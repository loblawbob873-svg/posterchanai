"""A BINARY WIRE PARSER FED BY ANY CLIENT THAT CONNECTS, AND NOTHING TESTED IT.

`nostr_relay/negentropy.py` had ZERO test references. It implements NIP-77 range-based set
reconciliation — the non-initiator half — and every byte it parses arrives from a stranger's
websocket. There is no authentication in front of it: a NEG-OPEN is something any client may send.

That makes two failure modes worth more than the feature itself:

  * IT MUST NEVER HANG OR CONSUME UNBOUNDED RESOURCES. An IdList carries a varint count followed by
    that many 32-byte ids. A count of 10^9 must not become a billion-iteration loop.
  * IT MUST RAISE, NOT IMPROVISE. `server.py` wraps both entry points and answers NEG-ERR so the
    client falls back to a normal REQ. That only works while malformed input raises — a parser that
    silently returned a partial or wrong answer would hand the client a reconciliation that quietly
    omits events, which is a SYNC HOLE: the client concludes it is up to date and stops asking.

The protocol half matters for the same reason. Both sides compute fingerprints independently, so a
change to `_fingerprint`, to the bucket count, or to the bound delta-coding does not error — the two
sides simply disagree for ever, and the symptom is "some notes never arrive".

Every expectation here was measured against the shipped code first. The client messages are built
with the module's OWN `_encode_bound`, because hand-encoding them got the bound wrong on the first
try (the format is `<ts delta><prefix len><prefix>` and it is easy to omit the length) — a test
whose fixture is subtly malformed tests the error path and calls it the happy one.
"""
import pytest

from app.services.nostr_relay import negentropy as ng


def eid(b: int) -> bytes:
    """A 32-byte event id built from one repeated byte, so ordering is obvious."""
    return bytes([b]) * 32


def items_of(n: int, start_ts: int = 1000):
    return sorted((start_ts + i, eid(i)) for i in range(n))


def query(mode: int, payload: bytes = b"", bound: "ng.Bound | None" = None) -> bytes:
    """A client message with a single range, encoded by the module's own bound encoder."""
    m = bytearray([ng.PROTOCOL_VERSION])
    ng._encode_bound(m, bound or ng.Bound.infinity(), [0])
    m += ng.encode_varint(mode)
    m += payload
    return bytes(m)


def parse(resp: bytes):
    """Decode a response into [(mode, bound_ts, payload)] — the initiator's job."""
    r = ng._Reader(resp)
    assert r.read(1)[0] == ng.PROTOCOL_VERSION
    state, out = [0], []
    while r.remaining() > 0:
        b = ng._decode_bound(r, state)
        mode = r.read_varint()
        if mode == 0:
            out.append(("skip", b.timestamp, None))
        elif mode == 1:
            out.append(("fingerprint", b.timestamp, r.read(ng.FINGERPRINT_SIZE)))
        elif mode == 2:
            n = r.read_varint()
            out.append(("idlist", b.timestamp, [r.read(ng.ID_SIZE) for _ in range(n)]))
        else:
            raise AssertionError(f"we emitted an unknown mode {mode}")
    return out


# --------------------------------------------------------------------------- varint


@pytest.mark.parametrize("n", [0, 1, 2, 126, 127, 128, 129, 255, 16383, 16384, 16385,
                               1 << 20, (1 << 32) - 1, ng.MAX_U64])
def test_a_varint_round_trips(n):
    """Both sides encode and decode these independently; a disagreement is not an error, it is two
    relays that quietly never converge."""
    assert ng._Reader(ng.encode_varint(n)).read_varint() == n


def test_zero_is_a_single_byte():
    """The infinity bound is encoded as varint 0, so this one value is load-bearing in the format."""
    assert ng.encode_varint(0) == b"\x00"


def test_the_continuation_bit_is_set_on_every_byte_but_the_last():
    enc = ng.encode_varint(300)
    assert all(b & 0x80 for b in enc[:-1])
    assert not (enc[-1] & 0x80)


def test_a_truncated_varint_raises_rather_than_returning_a_partial_value():
    """A trailing 0x80 says "more bytes follow" and there are none. Returning what it had so far
    would silently change the number the peer sent."""
    with pytest.raises(ValueError):
        ng._Reader(b"\x80").read_varint()


# --------------------------------------------------------------------------- the reader


def test_reading_past_the_end_raises():
    r = ng._Reader(b"abc")
    assert r.read(3) == b"abc"
    with pytest.raises(ValueError, match="read past end"):
        r.read(1)


def test_a_short_read_is_never_silently_short():
    """The whole parser is built on this. A `read(32)` that returned 4 bytes would produce a
    plausible-looking id nobody sent."""
    with pytest.raises(ValueError):
        ng._Reader(b"abc").read(32)


def test_remaining_tracks_consumption():
    r = ng._Reader(b"abcdef")
    assert r.remaining() == 6
    r.read(2)
    assert r.remaining() == 4


# --------------------------------------------------------------------------- fingerprints


def test_a_fingerprint_is_the_declared_width():
    assert len(ng._fingerprint([eid(1), eid(2)])) == ng.FINGERPRINT_SIZE


def test_a_fingerprint_is_deterministic():
    assert ng._fingerprint([eid(1), eid(2)]) == ng._fingerprint([eid(1), eid(2)])


def test_a_fingerprint_does_not_depend_on_order():
    """It accumulates by ADDITION, which is commutative on purpose: two peers holding the same set
    must agree regardless of how they enumerate it. An order-sensitive hash would make identical
    relays disagree and re-sync for ever."""
    assert ng._fingerprint([eid(1), eid(2), eid(3)]) == ng._fingerprint([eid(3), eid(1), eid(2)])


def test_different_sets_fingerprint_differently():
    assert ng._fingerprint([eid(1)]) != ng._fingerprint([eid(2)])
    assert ng._fingerprint([eid(1), eid(2)]) != ng._fingerprint([eid(1)])


def test_the_count_is_part_of_the_fingerprint():
    """The accumulator alone is a sum, and sums collide easily — the empty set and a set summing to
    zero would otherwise match, and a peer would be told it is in sync with nothing."""
    assert ng._fingerprint([]) != ng._fingerprint([bytes(32)])


def test_an_empty_set_has_a_stable_fingerprint():
    assert ng._fingerprint([]) == ng._fingerprint([])


# --------------------------------------------------------------------------- bounds


def test_a_bound_round_trips_through_the_wire_format():
    out = bytearray()
    ng._encode_bound(out, ng.Bound(1234, b"\xab\xcd"), [0])
    got = ng._decode_bound(ng._Reader(bytes(out)), [0])
    assert got.timestamp == 1234 and got.id == b"\xab\xcd"


def test_infinity_round_trips():
    """Encoded as varint 0 rather than as MAX_U64, so it is its own case in both directions."""
    out = bytearray()
    ng._encode_bound(out, ng.Bound.infinity(), [0])
    assert ng._decode_bound(ng._Reader(bytes(out)), [0]).timestamp == ng.MAX_U64


def test_consecutive_bounds_delta_code_against_the_running_state():
    """Timestamps are sent as deltas from the previous bound. Encoder and decoder each keep their
    own state, and if the two ever disagree every bound after the first is wrong — which reads as
    "reconciliation returns the wrong ranges", not as a parse error."""
    out, enc_state = bytearray(), [0]
    for ts in (1000, 1005, 1005, 2000):
        ng._encode_bound(out, ng.Bound(ts, b""), enc_state)
    r, dec_state = ng._Reader(bytes(out)), [0]
    assert [ng._decode_bound(r, dec_state).timestamp for _ in range(4)] == [1000, 1005, 1005, 2000]


def test_an_over_long_id_prefix_is_refused():
    """An id is 32 bytes. A larger prefix length is either a broken peer or an attempt to make us
    read an arbitrary span of the buffer."""
    bad = ng.encode_varint(1) + ng.encode_varint(ng.ID_SIZE + 1)
    with pytest.raises(ValueError, match="bad id prefix len"):
        ng._decode_bound(ng._Reader(bad), [0])


def test_a_prefix_of_exactly_the_id_size_is_allowed():
    """The boundary in the other direction, so the check cannot become off-by-one and reject a
    legitimate full-id bound."""
    ok = ng.encode_varint(1) + ng.encode_varint(ng.ID_SIZE) + eid(7)
    assert ng._decode_bound(ng._Reader(ok), [0]).id == eid(7)


@pytest.mark.parametrize("n", [0, 1, 5, 17, 64])
def test_lower_index_agrees_with_a_linear_scan(n):
    """The binary search decides which of our events fall in a range. Off by one and we send the
    peer a neighbouring event and omit theirs — silently, and only for some ranges."""
    items = items_of(n)
    for ts in (999, 1000, 1002, 1000 + n, 1 << 40):
        for prefix in (b"", eid(2)[:1], eid(200)[:1]):
            b = ng.Bound(ts, prefix)
            linear = sum(1 for (t, i) in items if ng._item_lt_bound(t, i, b))
            assert ng._lower_index(items, b) == linear


def test_a_minimal_bound_separates_two_items():
    """It must be strictly above `prev` and at-or-below `curr`, or a split loses or duplicates an
    event at every bucket edge."""
    prev, curr = (1000, eid(1)), (1000, eid(2))
    b = ng._minimal_bound(prev, curr)
    assert ng._item_lt_bound(*prev, b)
    assert not ng._item_lt_bound(*curr, b)


def test_a_minimal_bound_across_timestamps_needs_no_prefix():
    assert ng._minimal_bound((1000, eid(1)), (2000, eid(2))) .timestamp == 2000


# --------------------------------------------------------------------------- reconcile: protocol


def test_a_matching_fingerprint_answers_skip():
    """The point of the whole protocol: when both sides hold the same events over a range, the
    range costs one Skip and no ids move."""
    items = items_of(5)
    resp = parse(ng.reconcile(items, query(1, ng._fingerprint([i[1] for i in items]))))
    assert [m for m, _ts, _p in resp] == ["skip"]


def test_a_differing_fingerprint_on_a_small_range_answers_with_our_ids():
    items = items_of(5)
    resp = parse(ng.reconcile(items, query(1, ng._fingerprint([eid(0x99)]))))
    assert resp[0][0] == "idlist"
    assert resp[0][2] == [i[1] for i in items]


def test_a_large_differing_range_is_split_into_buckets_rather_than_dumped():
    """Sending 50,000 ids in one frame is what the protocol exists to avoid: a large mismatched
    range comes back as fingerprint sub-ranges for the client to narrow.

    The literal 16 is deliberate. Writing `["fingerprint"] * ng._BUCKETS` compares the constant to
    ITSELF — measured, changing _BUCKETS to 8 left that version green. It is a tuning trade (bigger
    frames against more round trips), so a change should be a decision, not a drift."""
    resp = parse(ng.reconcile(items_of(64), query(1, ng._fingerprint([eid(0x99)]))))
    assert [m for m, _ts, _p in resp] == ["fingerprint"] * 16


def test_a_split_covers_every_event_exactly_once():
    """THE CORRECTNESS RULE UNDER THE BUCKET COUNT, and the one that does not care what it is.

    The buckets are cut with `per = num // _BUCKETS` plus a remainder spread over the first few, and
    each boundary is a `_minimal_bound` between two adjacent items. An off-by-one there does not
    error: it drops an event from every bucket edge or double-counts one, and the client simply
    never receives those events. 70 items over 16 buckets exercises the uneven remainder."""
    items = items_of(70)
    resp = ng.reconcile(items, query(1, ng._fingerprint([eid(0x99)])))

    r = ng._Reader(resp)
    r.read(1)
    state, lower, covered = [0], ng.Bound(0, b""), []
    while r.remaining() > 0:
        upper = ng._decode_bound(r, state)
        assert r.read_varint() == 1
        r.read(ng.FINGERPRINT_SIZE)
        covered.extend(items[ng._lower_index(items, lower):ng._lower_index(items, upper)])
        lower = upper

    assert covered == items, "the buckets do not tile the range — events fall between them"
    assert lower.timestamp == ng.MAX_U64, "the last bucket must carry the original upper bound"


def test_each_bucket_fingerprint_describes_the_span_its_bound_claims():
    """WHAT THE PEER ACTUALLY CHECKS, and the only thing that catches a shifted boundary.

    Coverage alone does not: moving every bucket edge one item along still tiles the range
    perfectly — measured, that mutation left the test above green. What breaks is the PAIRING. The
    client takes each returned bound, selects its own events in that span, fingerprints them, and
    compares. If our fingerprint was computed over a different span than the bound describes, the
    two never agree: the client re-splits the same range for ever, and the events in it never
    arrive. Nothing errors on either side.

    So this recomputes each fingerprint from the bound, exactly as the initiator would."""
    items = items_of(70)
    resp = ng.reconcile(items, query(1, ng._fingerprint([eid(0x99)])))

    r = ng._Reader(resp)
    r.read(1)
    state, lower, checked = [0], ng.Bound(0, b""), 0
    while r.remaining() > 0:
        upper = ng._decode_bound(r, state)
        assert r.read_varint() == 1
        theirs = r.read(ng.FINGERPRINT_SIZE)
        lo, hi = ng._lower_index(items, lower), ng._lower_index(items, upper)
        assert theirs == ng._fingerprint([i[1] for i in items[lo:hi]]), (
            f"the fingerprint for the range ending at {upper.timestamp} does not match our own "
            f"events in that range — the client will re-split it for ever")
        lower = upper
        checked += 1
    assert checked == 16


def test_just_below_the_split_threshold_still_sends_ids():
    """The cutover is `_BUCKETS * 2`. Pinned with literals for the same reason as above — the
    symbolic form was self-referential — and measured on both sides so the threshold cannot drift
    into splitting tiny ranges (a round trip per event) or dumping huge ones in one frame."""
    assert parse(ng.reconcile(items_of(31),
                              query(1, ng._fingerprint([eid(0x99)]))))[0][0] == "idlist"
    assert parse(ng.reconcile(items_of(32),
                              query(1, ng._fingerprint([eid(0x99)]))))[0][0] == "fingerprint"


def test_an_idlist_from_the_client_is_answered_with_our_ids():
    """We do not need the client's ids — it is telling us what it has so it can diff against ours.
    They still have to be CONSUMED from the buffer, or every range after this one is misparsed."""
    items = items_of(3)
    payload = ng.encode_varint(2) + eid(0x90) + eid(0x91)
    resp = parse(ng.reconcile(items, query(2, payload)))
    assert resp[0][0] == "idlist" and resp[0][2] == [i[1] for i in items]


def test_a_skip_from_the_client_is_answered_with_skip():
    assert parse(ng.reconcile(items_of(5), query(0)))[0][0] == "skip"


def test_a_relay_holding_nothing_answers_an_empty_id_list():
    """Not an error and not silence: the client must learn we have nothing in that range, or it
    keeps asking for ever."""
    resp = parse(ng.reconcile([], query(1, ng._fingerprint([eid(1)]))))
    assert resp[0][0] == "idlist" and resp[0][2] == []


def test_the_response_always_starts_with_the_protocol_version():
    for q in (query(0), query(1, ng._fingerprint([])), query(2, ng.encode_varint(0))):
        assert ng.reconcile(items_of(3), q)[0] == ng.PROTOCOL_VERSION


def test_several_ranges_in_one_message_are_all_answered():
    """Ranges are consecutive and each one's upper bound is the next one's lower bound. Losing
    track of that is how a reconciliation omits a slice of time without failing."""
    items = items_of(10)
    m = bytearray([ng.PROTOCOL_VERSION])
    state = [0]
    ng._encode_bound(m, ng.Bound(1005, b""), state)
    m += ng.encode_varint(0)                                   # skip everything before 1005
    ng._encode_bound(m, ng.Bound.infinity(), state)
    m += ng.encode_varint(1) + ng._fingerprint([eid(0x99)])    # mismatch on the rest
    resp = parse(ng.reconcile(items, bytes(m)))
    assert [x[0] for x in resp] == ["skip", "idlist"]
    assert resp[1][2] == [i[1] for i in items[5:]], "the second range covered the wrong events"


# --------------------------------------------------------------------------- reconcile: hostile


@pytest.mark.parametrize("bad,why", [
    (b"", "empty message"),
    (b"\x62", "wrong protocol version"),
    (bytes([ng.PROTOCOL_VERSION, 0x80]), "truncated varint in the bound"),
    (bytes([ng.PROTOCOL_VERSION]) + ng.encode_varint(1) + ng.encode_varint(99), "over-long prefix"),
    (bytes([ng.PROTOCOL_VERSION]) + ng.encode_varint(0) + ng.encode_varint(0)
     + ng.encode_varint(1) + b"\x00" * 4, "fingerprint cut short"),
    (bytes([ng.PROTOCOL_VERSION]) + ng.encode_varint(0) + ng.encode_varint(0)
     + ng.encode_varint(2) + ng.encode_varint(5) + eid(1), "idlist shorter than its count"),
])
def test_malformed_input_raises_rather_than_answering(bad, why):
    """`server.py` catches and sends NEG-ERR so the client falls back to a normal REQ. That only
    works while this raises. A parser that returned a partial answer instead would hand the client
    a reconciliation quietly missing events — the client would conclude it is up to date and stop
    asking, which is a sync hole rather than a visible failure."""
    with pytest.raises(ValueError):
        ng.reconcile(items_of(8), bad)


def test_an_unknown_mode_is_refused():
    """Modes 3+ are undefined in v1. Ignoring one would leave its payload in the buffer and
    misparse every range after it."""
    with pytest.raises(ValueError, match="unknown mode"):
        ng.reconcile(items_of(4), query(7))


def test_an_absurd_idlist_count_does_not_become_an_absurd_loop():
    """The count is a varint from a stranger. It must be bounded by the bytes actually present,
    not trusted — otherwise one small message costs a billion iterations."""
    with pytest.raises(ValueError):
        ng.reconcile(items_of(4), query(2, ng.encode_varint(10 ** 9)))


def test_a_long_run_of_continuation_bytes_is_bounded_by_the_message():
    """A varint has no declared width, so a run of 0x80 keeps the decoder going. It has to end when
    the buffer does."""
    with pytest.raises(ValueError):
        ng.reconcile(items_of(4), bytes([ng.PROTOCOL_VERSION]) + b"\x80" * 4096)


def test_a_version_byte_alone_is_a_valid_empty_message():
    """Measured: not an error. It carries no ranges, so the answer is the bare version byte — which
    is exactly what `_on_neg_msg` reads as "nothing left to reconcile" before closing the session."""
    assert ng.reconcile(items_of(4), bytes([ng.PROTOCOL_VERSION])) == bytes([ng.PROTOCOL_VERSION])


# --------------------------------------------------------------------------- the caller


def test_both_entry_points_catch_what_reconcile_raises():
    """The contract is split across two files: this one raises, `server.py` turns that into NEG-ERR.
    An uncaught raise there would take down the connection handler on a message anyone can send."""
    import pathlib
    src = (pathlib.Path(ng.__file__).parent / "server.py").read_text(encoding="utf-8")
    for entry in ("_on_neg_open", "_on_neg_msg"):
        body = src[src.index(f"async def {entry}"):]
        body = body[:body.index("\n    async def ", 1) if "\n    async def " in body[1:] else len(body)]
        assert "negentropy.reconcile" in body, f"{entry} no longer reconciles"
        assert "except Exception" in body, \
            f"{entry} calls reconcile without catching — malformed input kills the connection"
        assert "NEG-ERR" in body, f"{entry} does not tell the client to fall back"
