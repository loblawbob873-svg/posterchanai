"""A view must not spend a network round trip showing nothing it could already have drawn.

THE RULE, and it is the general one behind three separate reports:

    If the client already holds what the view is ABOUT, paint that before the first network await.
    If it holds nothing, keep the spinner — an empty answer must never be dressed up as an answer.

The client is cache-first everywhere else (Store is a local relay), and every screen here paints into
one shared `#feed`. The failure shape is always the same: `feed.innerHTML='<div class="spinner">'`,
then a serial chain of awaits, then the real `feed.innerHTML=` at the very bottom. Nothing is logged,
nothing is broken, and the user sees a blank screen for as long as the radio takes.

Where it has actually bitten:

  * A POST OPENED FROM A NOTIFICATION. renderThread waited for `Relay.ready()`, fetched the event,
    walked the ancestor chain, fetched the root, and then ran up to FOUR rounds of reply expansion —
    each firing a REQ, waiting for EOSE, and repeating the whole query when the answer came back
    incomplete — before painting a single pixel. The event was in the Store the entire time: the
    notification was built from it. Reported as "it's like all the content is empty", then "takes
    forever to load but it does", which is what rules out a rendering failure and names it a wait.
  * A PROFILE. renderProfileView waited for the socket, refetched kind-0, then queried the author's
    notes with TWO retries and backoff on an empty result — all before the header. Your own profile,
    and anyone you had read that session, were fully in the Store.

The cold paths are deliberately unchanged in both: with nothing cached, painting early would mean a
header reading "anon" with no avatar, or a thread claiming a reply count it has not counted, and then
rewriting itself. That is worse than a spinner, so the split is on whether there is something real to
show — never on a timeout.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APPJS = os.path.join(ROOT, "static", "js", "client", "app.js")
CSS = os.path.join(ROOT, "static", "css", "client.css")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


APP = _read(APPJS)


def _fn(sig):
    """The body of a top-level function, from its signature to the next top-level declaration."""
    start = APP.index(sig)
    rest = APP[start + len(sig):]
    m = re.search(r"\n  (?:async )?function ", rest)
    assert m, f"could not find the end of {sig!r} — re-point this test"
    return APP[start: start + len(sig) + m.start()]


@pytest.fixture(scope="module")
def thread():
    return _fn("async function renderThread(id, hints){")


@pytest.fixture(scope="module")
def profile():
    return _fn("async function renderProfileView(pk){")


# ── A post ────────────────────────────────────────────────────────────────────────────────────────


def test_a_post_we_already_hold_is_painted_before_the_socket_is_waited_on(thread):
    """Above `await Relay.ready()`, not merely above the thread expansion. A notification tap on a
    COLD app is precisely "the event is in the Store and the relay has not finished connecting" —
    waiting for the socket first spends the whole connect on a spinner for a post the client could
    have drawn before the first packet left the phone."""
    assert "_paintThreadHead(feed, have)" in thread, \
        "renderThread no longer paints what the Store already holds — that IS the blank screen"
    paint = thread.index("_paintThreadHead(feed, have)")
    # The STATEMENTS, not the words — the comment above the paint quotes `await Relay.ready()` to
    # explain why it is there, and matching that made this test pass for the wrong reason.
    for later, why in (
        ("try{ await Relay.ready(); }catch(_){}", "the socket connect"),
        ("const { rootId, chain } = await _threadRoot(ev, hints);", "the ancestor walk"),
        ("for(let round=0; round<4", "the reply expansion rounds"),
    ):
        assert thread.index(later) > paint, f"{why} runs before the first paint — that is the blank screen"


def test_the_thread_paint_makes_no_claim_it_has_not_checked():
    """A wrong answer early is worse than a right one late. The reply COUNT, the ancestor chain and
    the missing-parent notice each need the network this paint exists not to wait for."""
    head = _fn("function _paintThreadHead(feed, ev){")
    assert "repl" not in head, "the early paint must not state a reply count it has not counted"
    assert "missingParent" not in head
    assert "thread-hl" in head and "noteHtml(ev)" in head


def test_a_chat_message_is_not_painted_as_a_thread(thread):
    """A kind-42 has no normal thread — renderThread resolves its channel and opens the room instead.
    Painting it as a thread head first would flash a post that is about to be replaced by a chat."""
    assert "have.kind!==42" in thread


def test_back_works_during_the_wait(thread):
    """A spinner you cannot leave is the other half of the complaint. One binder for both paints, so
    the early screen cannot end up with a dead button."""
    assert thread.count("_bindThreadBack(feed)") >= 1
    binder = _fn("function _bindThreadBack(feed){")
    assert "history.back()" in binder and "_startTimeline()" in binder
    assert APP.count("function _bindThreadBack(") == 1, \
        "two copies of the Back binding is how one of them goes stale"


def test_a_newer_thread_opened_mid_flight_still_wins(thread):
    """The early paint adds await boundaries that did not exist before it. Without a token re-check
    after the ancestor walk, opening B while A resolves repaints A's conversation over B."""
    after = thread[thread.index("await _threadRoot(ev, hints)"):]
    guard = after[: after.index("let root =")]
    assert "renderThread._tok!==id" in guard, \
        "the ancestor walk must re-check the token — it is an await like any other"


# ── A profile ─────────────────────────────────────────────────────────────────────────────────────


def test_a_cached_profile_skips_every_blocking_read(profile):
    """The socket wait, the kind-0 refetch and the notes query (which retries TWICE with backoff on an
    empty result) all move behind the paint when there is something cached to paint."""
    assert "const _cached = !!(Store.profile(pk) || Store.feed(e=>e.pubkey===pk).length);" in profile
    guard = profile.index("if(!_cached){")
    end = profile.index("const p=Store.profile(pk)||{}; const mine=pk===ME.pubkey;")
    cold = profile[guard:end]
    for blocking in ('feed.innerHTML=\'<div class="spinner">', "await Relay.ready()",
                     "kinds:[0],limit:1", "_loadNotes()"):
        assert blocking in cold, f"{blocking!r} must sit inside the cold-only branch"
    # …and nothing blocking is left outside it, before the paint. `_loadNotes` is DEFINED above the
    # branch and awaits inside its own body; what matters is that nothing CALLS it there.
    warm = profile[:guard]
    warm = warm.replace(profile[profile.index("const _loadNotes = async () => {"): profile.index("if(!_cached){")], "")
    assert "await " not in warm, "a warm profile open must reach its paint with no await at all"


def test_a_cached_profile_is_still_refreshed_afterwards(profile):
    """Cache-first is not cache-only: a rename or a new avatar has to land without a reload, and the
    notes list has to fill in. _patchProfileHeader exists for exactly this."""
    ref = profile[profile.index("if(_cached) (async()=>{"):]
    ref = ref[: ref.index("})();")]
    assert "_patchProfileHeader(pk)" in ref
    assert "_loadNotes()" in ref and "fillList(_prof.tab)" in ref
    # The scroll-back cursor was taken from what the CACHE held; paging from there would re-request
    # notes the refresh just brought in.
    assert "_prof.oldest=" in ref
    # It is a second async path through one view, and it must never patch a profile the user has left.
    assert ref.count("myGen!==_profGen") >= 2 and "_prof.pk!==pk" in ref


def test_the_cold_profile_path_is_unchanged(profile):
    """With nothing cached, an early paint means a header reading "anon" with no avatar that rewrites
    itself a second later. That is worse than a spinner, so the split is on having something real to
    show — never on a timeout."""
    cold = profile[profile.index("if(!_cached){"):profile.index("const p=Store.profile(pk)||{};")]
    assert "myGen!==_profGen" in cold, "the cold path keeps its generation guards"
    decision = profile[profile.index("const _cached ="): profile.index("const _loadNotes =")]
    assert "setTimeout" not in decision and "await" not in decision, \
        "the decision must not be a timer or a read — it is 'do we hold anything', asked once"


# ── The shared bit of chrome the loading label needs ──────────────────────────────────────────────


def test_the_inline_spinner_is_not_the_block_one():
    """`.spinner` carries `margin:40px auto`, which is what centres the block version. Reused inline
    it pushes the label a screen and a half below the post it belongs to."""
    rule = re.search(r"\n\.spinner-inline\{([^}]*)\}", _read(CSS))
    assert rule, ".spinner-inline is used by the thread's loading label but not defined"
    body = rule.group(1).replace(" ", "")
    assert "margin:0" in body and "display:inline-block" in body
