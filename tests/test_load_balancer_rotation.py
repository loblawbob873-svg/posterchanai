"""THE FOUR BUGS THAT HAVE EACH BROKEN THE LOAD BALANCER — CHECKED ON EVERY FACTORY, NOT TWO.

CLAUDE.md and the project memory both list four regressions that have each taken the LB out, and
the user has flagged the load balancer as a RECURRING problem. Two of the four were already
guarded — in `test_voice_wiring.py` (voice) and `test_search_load_balance.py` (search).

The three factories the memory actually names — image, music, video — were guarded by nothing.

That is the same shape as the native-dialog bug: a real rule, correctly written down, enforced at a
subset of the places it applies to. A rule that holds for two of five factories is not a rule, it is
a coincidence that survives until somebody edits a third. So this file DISCOVERS every
`app/services/*_factory.py` that round-robins and applies the rules to all of them — a sixth factory
is covered the day it is written, without anybody remembering to add it here.

WHY THESE FAIL SILENTLY, which is why they keep coming back. None of them raise. Nothing appears in
a log except the candidate line, which still looks plausible. The whole symptom is that one machine
stops receiving work: nas idles, this node queues, and everything still answers — slower. That is
indistinguishable from "the other box is just quiet" unless something measures the rotation.

The rotation rules are tested by RUNNING `_rotated`, not by grepping for the fix. The two failures
this session were both assertions that had drifted from their code (a case mismatch, an over-broad
ban), and a string match for `% 1_000_000` would be exactly that kind of assertion.
"""
import asyncio
import importlib
import pathlib
import re

import pytest


SERVICES = pathlib.Path(__file__).resolve().parents[1] / "app" / "services"


def _strip_prose(src: str) -> str:
    """Docstrings and `#` comments removed.

    MEASURED, not precautionary. These factories DOCUMENT the rules they implement — image_factory
    carries the sentence "THIS node is already represented by _LOCAL" three lines above the code
    that represents it. Scanning the raw text, deleting the real candidate still left the comment
    behind and the assertion passed over a factory that had stopped using its own GPU. A rule
    checked against prose is checked against the intention, which is the one thing that never
    regresses."""
    src = re.sub(r'"""(?:.|\n)*?"""', " ", src)
    src = re.sub(r"'''(?:.|\n)*?'''", " ", src)
    return re.sub(r"#[^\n]*", " ", src)


def _factory_sources():
    return {p.stem: _strip_prose(p.read_text(encoding="utf-8"))
            for p in sorted(SERVICES.glob("*_factory.py"))}


#: Every factory that round-robins over a node list. Discovered, never typed out.
ROTATING = sorted(n for n, src in _factory_sources().items() if "async def _rotated" in src)

#: Of those, the ones dispatching GPU work — they alone need the busy-aware deferral.
#: `GPUResourceLock` is the anchor rather than `gpu_busy`, so a factory that LOSES the busy check
#: cannot drop out of its own sweep. (search_factory rotates but does no GPU work.)
GPU_DISPATCH = sorted(n for n in ROTATING if "GPUResourceLock" in _factory_sources()[n])


def _mod(name):
    return importlib.import_module(f"app.services.{name}")


def _rot(mod, candidates):
    return asyncio.run(mod._rotated(list(candidates)))


@pytest.fixture
def zeroed(request, monkeypatch):
    """Each case starts from a known rotation index and restores it, because `_rr_index` is a
    process-wide global shared with anything else importing the factory."""
    mod = _mod(request.param) if hasattr(request, "param") else None
    return mod


# --------------------------------------------------------------------------- the sweep is real


def test_the_sweep_found_the_factories():
    """If the discovery ever returns nothing — a rename, a moved directory — every parametrised
    test below silently becomes zero test cases and this file passes while checking nothing."""
    assert ROTATING, "no rotating factories discovered; every rule below is inspecting nothing"
    for expected in ("image_factory", "music_factory", "video_factory"):
        assert expected in ROTATING, f"{expected} no longer round-robins — was that deliberate?"


def test_the_gpu_set_is_still_the_gpu_set():
    """Keeps the narrower sweep honest. A new GPU-dispatching factory must join the busy-aware rule
    rather than quietly sitting outside it."""
    assert set(GPU_DISPATCH) == {"image_factory", "music_factory", "video_factory",
                                 "voice_factory"}, (
        "the set of GPU-dispatching factories changed to %s — apply the busy-aware rule to the new "
        "one, then update this list" % GPU_DISPATCH)


# --------------------------------------------------------------------------- bug 1: the index reset


@pytest.mark.parametrize("name", ROTATING)
def test_a_single_candidate_call_does_not_reset_the_rotation(name, monkeypatch):
    """BUG 1, VERBATIM FROM THE MEMORY:

        NEVER `_rr_index = (_rr_index+1) % len(candidates)`. Forwarded/local_only calls pass
        `len==1`, so `% 1` resets the shared index to 0 → every request restarts at `_LOCAL` and
        peers (nas) are starved.

    A forwarded request arrives with the load-balanced header and runs `local_only`, which is a
    ONE-candidate call. Those are constant in normal operation, so under the bug the rotation is
    pinned at 0 forever and the peer never gets a turn."""
    mod = _mod(name)
    monkeypatch.setattr(mod, "_rr_index", 0)

    first = _rot(mod, ["a", "b", "c"])[0]
    _rot(mod, ["only-me"])                      # a forwarded / local_only call
    second = _rot(mod, ["a", "b", "c"])[0]

    assert first != second, (
        f"{name}: a single-candidate call reset the round-robin — every request restarts at the "
        f"same node and the peers are starved (both walks began at {first!r})")


@pytest.mark.parametrize("name", ROTATING)
def test_a_single_candidate_call_does_not_advance_the_rotation_either(name, monkeypatch):
    """The other half: a forwarded call is not a balancing decision, so it must not consume a turn.
    If it did, a node handling forwarded traffic would skip nodes in its own rotation."""
    mod = _mod(name)
    monkeypatch.setattr(mod, "_rr_index", 0)

    _rot(mod, ["only-me"])
    _rot(mod, ["only-me"])
    assert _rot(mod, ["a", "b", "c"])[0] == "a", \
        f"{name}: single-candidate calls consumed turns in the shared rotation"


@pytest.mark.parametrize("name", ROTATING)
def test_the_rotation_actually_walks(name, monkeypatch):
    mod = _mod(name)
    monkeypatch.setattr(mod, "_rr_index", 0)
    assert [_rot(mod, ["a", "b", "c"])[0] for _ in range(6)] == list("abcabc"), \
        f"{name}: consecutive requests do not start at successive nodes"


@pytest.mark.parametrize("name", ROTATING)
def test_every_node_leads_its_fair_share(name, monkeypatch):
    """The user-visible symptom of all of this is 'nas never gets any work'. This measures that
    directly, rather than trusting the mechanism that produces it."""
    mod = _mod(name)
    monkeypatch.setattr(mod, "_rr_index", 0)
    nodes = ["local", "peer-a", "peer-b"]
    leads = {n: 0 for n in nodes}
    for _ in range(300):
        leads[_rot(mod, nodes)[0]] += 1
    assert set(leads.values()) == {100}, f"{name}: uneven rotation {leads}"


@pytest.mark.parametrize("name", ROTATING)
def test_rotation_keeps_every_candidate_exactly_once(name, monkeypatch):
    """A rotation that drops a node removes it from the pool; one that duplicates a node makes a
    failed attempt retry the same box instead of failing over."""
    mod = _mod(name)
    monkeypatch.setattr(mod, "_rr_index", 0)
    nodes = ["local", "peer-a", "peer-b", "peer-c"]
    for _ in range(9):
        out = _rot(mod, nodes)
        assert sorted(out) == sorted(nodes), f"{name}: rotation changed the candidate set: {out}"


@pytest.mark.parametrize("name", ROTATING)
def test_no_candidates_is_an_empty_list_not_a_crash(name, monkeypatch):
    mod = _mod(name)
    monkeypatch.setattr(mod, "_rr_index", 0)
    assert _rot(mod, []) == []


@pytest.mark.parametrize("name", ROTATING)
def test_the_index_cannot_grow_without_bound(name, monkeypatch):
    """It is a process-lifetime counter on a long-running server. The modulus is what stops it."""
    mod = _mod(name)
    monkeypatch.setattr(mod, "_rr_index", 999_999)
    _rot(mod, ["a", "b"])
    assert 0 <= mod._rr_index < 1_000_000, f"{name}: _rr_index left at {mod._rr_index}"


# --------------------------------------------------------------------------- bug 2: self excluded


@pytest.mark.parametrize("name", ROTATING)
def test_peers_are_parsed_with_self_excluded(name):
    """BUG 2: this node's own IP is in `chat_server_urls` AND it is also the local candidate, so
    without `exclude_self` it is counted TWICE — two requests both land here and the peer is never
    reached. Worse for the GPU factories: a node that HTTPs its own /api/generate-* holds the GPU
    lock while waiting on a request queued behind that same lock."""
    src = _factory_sources()[name]
    calls = re.findall(r"parse_server_urls\(([^)]*)\)", src)
    assert calls, f"{name}: never parses a peer list at all"
    bare = [c for c in calls if "exclude_self=True" not in c]
    # MEASURED: a plain `"exclude_self=True" in src` does NOT catch this. image_factory parses a
    # peer list in two places, so deleting the kwarg from one leaves the other and the assertion
    # still passes over a node that is now its own peer. Every call site, or nothing.
    assert bare == [], (
        f"{name}: parse_server_urls without exclude_self — this node becomes its own peer, so it "
        f"is counted twice and the real peer is starved: {bare}")


@pytest.mark.parametrize("name", ROTATING)
def test_self_detection_is_the_shared_one_not_a_local_reimplementation(name):
    """The memory is explicit that there are two implementations and one is weaker:

        use `parse_server_urls(raw, exclude_self=True)` — it has robust local-IP detection via
        outbound-socket/`ip addr`; image_load_balancer's exclude_self is weaker.

    Deciding "is this me?" wrongly is bug 2 with extra steps, so every factory must ask the same
    code rather than each carrying its own idea of this node's address."""
    src = _factory_sources()[name]
    assert re.search(r"from app\.services\.load_balancer import [^\n]*parse_server_urls", src) \
        or re.search(r"load_balancer\.parse_server_urls", src), \
        f"{name}: does not use load_balancer.parse_server_urls — it has its own self-detection"


# --------------------------------------------------------------------------- bug 4: busy-aware


@pytest.mark.parametrize("name", GPU_DISPATCH)
def test_a_busy_local_gpu_defers_to_an_idle_peer(name):
    """BUG 4: after rotating, a node whose own GPU is occupied must hand the request to an idle peer
    instead of queueing behind its own in-progress job. Without it a request waits minutes on the
    flock while another machine sits idle — and nothing reports it, because the request does
    eventually succeed."""
    src = _factory_sources()[name]
    assert "gpu_busy" in src, \
        f"{name}: dispatches GPU work without consulting gpu_busy — a busy node still wins its turn"


@pytest.mark.parametrize("name", GPU_DISPATCH)
def test_a_busy_local_is_demoted_and_never_dropped(name):
    """It must move to the END, not out. Local is the last-resort fallback when every remote fails;
    dropping it turns a busy local GPU into "cannot generate at all" the moment a peer is down."""
    src = _factory_sources()[name]
    body = src[src.index("gpu_busy"):]
    # The rule is PARTITION, NOT DROP, and it is written two different ways on purpose:
    #   image/music/video  [c for c in candidates if c != _LOCAL] + [_LOCAL]
    #   voice              [c for c in order if c not in busy] + [c for c in order if c in busy]
    # Voice demotes every busy node in one pass, which is the same rule generalised. What both have
    # — and what a "drop the busy node" regression would lose — is the trailing `+ [`, i.e. the
    # demoted node still being in the list. So that is what is asserted, rather than either spelling.
    assert re.search(r"\[[^\]]*\bfor\b[^\]]*\bif\b[^\]]*\]\s*\+\s*\[", body), (
        f"{name}: the busy branch does not partition the candidates — a busy node must move to the "
        f"END, not out of the list, or a busy local GPU means 'cannot generate' the moment a peer "
        f"is down")


# --------------------------------------------------------------------------- bug 3: local competes


@pytest.mark.parametrize("name", GPU_DISPATCH)
def test_this_node_is_a_candidate_alongside_the_peers(name):
    """BUG 3: image was once "remote-first, local-fallback", which bypasses this GPU entirely and
    piles every job onto the peers. Local belongs IN the round-robin — it is rotated with everything
    else, not consulted after the others fail."""
    src = _factory_sources()[name]
    # The window is the DISPATCH FUNCTION's body up to the rotation — from the last `def` above the
    # call, not from the top of the file.
    #
    # Two ways to get this wrong, both measured here rather than reasoned about:
    #   * anchoring on `_rotated(` finds its DEFINITION, which sits above the candidate building in
    #     every factory — that reported voice_factory as skipping its own GPU when it does not;
    #   * slicing from the top of the FILE always matches, because `_LOCAL = "__local__"` is a
    #     module-level constant. Removing local from the candidate list left that test green.
    call = src.index("await _rotated(")
    start = max(src.rfind("\ndef ", 0, call), src.rfind("\nasync def ", 0, call))
    window = src[start:call]
    assert re.search(r"(_LOCAL|['\"]local['\"])", window), (
        f"{name}: the candidate list handed to the rotation contains no local entry — this node's "
        f"GPU is skipped and every job piles onto the peers")
