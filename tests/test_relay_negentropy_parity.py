"""NEGENTROPY'S FINGERPRINTS ARE PREFIX SUMS NOW, AND THE WIRE MUST NOT HAVE MOVED BY ONE BYTE.

A range fingerprint is sha256(sum of the ids as little-endian 256-bit ints, mod 2^256 || varint(count)). The relay
used to sum each range in a Python loop, every round, for every range the client asked about -- a 50k set cost
~90 ms of event-loop time per session, 200k ~800 ms. `ItemSet` builds 32-bit-limb prefix sums once per session
(numpy cumsum) and answers a range's sum with one subtraction; range lookup became `bisect_left` on the tuples.

Both sides compute fingerprints INDEPENDENTLY, so a wrong one is not an error: the two sides simply disagree for
ever and "some notes never arrive". So nothing here checks the new code against itself. The reference is the
module exactly as it shipped before the change (`tests/fixtures/negentropy_reference_before_itemset.py`, a
verbatim copy), and every comparison is byte-for-byte on the wire: whole multi-round sessions in both roles,
random client messages (random bounds and prefixes -- which produce OUT-OF-ORDER ranges, where lo > hi --
random modes, fingerprints and IdLists, frame limits), empty sets, the split thresholds, many events sharing one
timestamp (so bounds need id prefixes), and malformed input, which must raise wherever the reference raised.
"""
import importlib.util
import pathlib
import random

import pytest

from app.services.nostr_relay import negentropy as ng

_REF = pathlib.Path(__file__).parent / "fixtures" / "negentropy_reference_before_itemset.py"
_spec = importlib.util.spec_from_file_location("negentropy_reference_before_itemset", _REF)
ref = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ref)


def items(rnd, n, ts_span):
    """n unique ids over a timestamp span; a small span makes many events share a second."""
    out = {}
    while len(out) < n:
        out[rnd.randbytes(32)] = 1_700_000_000 + rnd.randrange(ts_span)
    return sorted((ts, i) for i, ts in out.items())


def variant(rnd, base, drop, add, ts_span):
    kept = [x for x in base if rnd.random() >= drop]
    return sorted(set(kept) | set(items(rnd, add, ts_span)))


def both(fn_ref, fn_new):
    """Call both; they must return the same bytes or both raise."""
    try:
        want = fn_ref()
    except Exception as e:                       # noqa: BLE001
        with pytest.raises(Exception):
            fn_new()
        return type(e)
    assert fn_new() == want
    return want


SIZES = [0, 1, 2, 31, 32, 33, 64, 500, 4000]


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("n", SIZES)
def test_a_whole_session_is_byte_identical_in_both_roles(seed, n):
    rnd = random.Random(seed * 1000 + n)
    span = rnd.choice([3, 50, 10_000_000])
    ours = items(rnd, n, span)
    theirs = variant(rnd, ours, rnd.choice([0, 0.001, 0.05, 0.5, 1.0]), rnd.choice([0, 1, 7, 300]), span)
    a, b = ref.Initiator(theirs), ng.Initiator(theirs)
    msg = a.initiate()
    assert b.initiate() == msg
    S = ng.ItemSet(ours)
    for _ in range(40):
        resp = ref.reconcile(ours, msg)
        assert ng.reconcile(S, msg) == resp
        assert ng.reconcile(ours, msg) == resp           # a plain list still works, wrapped per call
        msg = a.reconcile(resp)
        assert b.reconcile(resp) == msg
        if msg is None:
            break
    else:
        pytest.fail("the session never finished")
    assert (a.need, a.have) == (b.need, b.have)
    assert a.need == {i for _, i in ours} - {i for _, i in theirs}
    assert a.have == {i for _, i in theirs} - {i for _, i in ours}


def random_message(rnd, pool):
    m = bytearray([ng.PROTOCOL_VERSION])
    state = [0]
    ts = 1_700_000_000 + rnd.randrange(-5, 60)
    for _ in range(rnd.randrange(0, 40)):
        if rnd.random() < 0.05:
            b = ng.Bound.infinity()
        else:
            ts = max(ts + rnd.choice([0, 0, 0, 1, 2, 7]), state[0] if state[0] != ng.MAX_U64 else 0)
            src = rnd.choice(pool)[1] if pool and rnd.random() < 0.7 else rnd.randbytes(32)
            b = ng.Bound(ts, src[:rnd.randrange(0, 33)])
        if state[0] == ng.MAX_U64 and b.timestamp != ng.MAX_U64:
            break                                   # nothing can follow infinity on the wire
        ref._encode_bound(m, b, state)
        mode = rnd.choice([0, 1, 1, 1, 2])
        m += ng.encode_varint(mode)
        if mode == 1:
            m += rnd.randbytes(ng.FINGERPRINT_SIZE)
        elif mode == 2:
            k = rnd.randrange(0, 4)
            m += ng.encode_varint(k) + rnd.randbytes(32 * k)
        if b.timestamp == ng.MAX_U64:
            break
    return bytes(m)


@pytest.mark.parametrize("seed", range(40))
def test_random_client_messages_get_byte_identical_answers(seed):
    rnd = random.Random(seed)
    ours = items(rnd, rnd.choice([0, 5, 40, 300, 3000]), rnd.choice([2, 20, 60]))
    S = ng.ItemSet(ours)
    for _ in range(25):
        msg = random_message(rnd, ours)
        limit = rnd.choice([0, 0, 300, 1000, 5000])
        both(lambda: ref.reconcile(ours, msg, limit), lambda: ng.reconcile(S, msg, limit))


@pytest.mark.parametrize("seed", range(20))
def test_malformed_messages_raise_exactly_where_they_used_to(seed):
    rnd = random.Random(seed)
    ours = items(rnd, 200, 30)
    S = ng.ItemSet(ours)
    for _ in range(50):
        good = random_message(rnd, ours)
        cut = good[:rnd.randrange(0, len(good) + 1)]                        # truncated anywhere
        junk = bytes([ng.PROTOCOL_VERSION]) + rnd.randbytes(rnd.randrange(0, 60))
        for msg in (cut, junk, bytes([0x60]) + good[1:]):
            both(lambda: ref.reconcile(ours, msg), lambda: ng.reconcile(S, msg))


def test_an_absurd_idlist_count_still_raises_quickly():
    m = bytearray([ng.PROTOCOL_VERSION])
    ng._encode_bound(m, ng.Bound.infinity(), [0])
    m += ng.encode_varint(2) + ng.encode_varint(10 ** 12)
    with pytest.raises(ValueError):
        ng.reconcile(ng.ItemSet([]), bytes(m))


def test_range_fingerprints_equal_the_summed_definition_for_every_span():
    rnd = random.Random(7)
    its = items(rnd, 300, 1000)
    S = ng.ItemSet(its)
    for lo in range(0, 301, 13):
        for hi in range(lo, 301, 17):
            assert S.fingerprint(lo, hi) == ref._fingerprint([i for _, i in its[lo:hi]])
    assert S.fingerprint(50, 10) == ref._fingerprint([])          # out-of-order bounds read as empty


def test_ids_whose_limbs_all_carry_still_sum_exactly():
    """All-0xff ids make every 32-bit limb overflow into the next on every addition -- the case where a limb
    recombination that forgot a carry, or a mask applied per limb, would disagree with the 256-bit sum."""
    its = [(1, b"\xff" * 31 + bytes([i])) for i in range(200)]
    S = ng.ItemSet(sorted(its))
    for lo, hi in ((0, 200), (3, 199), (0, 1), (100, 101)):
        assert S.fingerprint(lo, hi) == ref._fingerprint([i for _, i in sorted(its)[lo:hi]])


def test_bisect_lookup_agrees_with_the_reference_for_every_kind_of_bound():
    rnd = random.Random(3)
    its = items(rnd, 400, 5)                     # 400 events over 5 seconds: prefixes decide everything
    S = ng.ItemSet(its)
    probes = [ng.Bound(0), ng.Bound.infinity()]
    for ts, i in its[::7]:
        for k in (0, 1, 2, 5, 31, 32):
            probes += [ng.Bound(ts, i[:k]), ng.Bound(ts + 1, i[:k]), ng.Bound(ts - 1, i[:k])]
    probes += [ng.Bound(1_700_000_002, rnd.randbytes(rnd.randrange(33))) for _ in range(200)]
    for b in probes:
        assert S.lower(b) == ref._lower_index(its, b)


def test_a_store_buffer_of_ids_gives_the_same_fingerprints_as_joining_them():
    """PosterChanDB hands back `IdItems` (a list that also carries its ids as one buffer); the prefix sums are built
    from that buffer. It must be the same answer, and a buffer that does not match the list is not trusted."""
    rnd = random.Random(11)
    its = items(rnd, 1000, 100)
    raw = b"".join(i for _, i in its)
    plain, buffered = ng.ItemSet(its), ng.ItemSet(ng.IdItems(its, raw))
    wrong = ng.ItemSet(ng.IdItems(its, raw[:-32]))
    for lo, hi in ((0, 1000), (10, 990), (500, 501)):
        want = plain.fingerprint(lo, hi)
        assert buffered.fingerprint(lo, hi) == want
        assert wrong.fingerprint(lo, hi) == want
    assert ng.IdItems(its, raw) == its                     # still an ordinary list to everybody else

