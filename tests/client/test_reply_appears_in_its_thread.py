"""A REPLY YOU JUST POSTED HAS TO APPEAR IN THE CONVERSATION YOU POSTED IT IN.

Reported: *"I have to navigate away from this conversation and come back to it for it to load
whatever reply I just posted"*, alongside *"viewing all the replies under the main post not working
on android? webui works"*. Two symptoms, two separate defects, and neither is Android-specific in
the code — which is why looking for a platform branch finds nothing.

**1. Nothing repainted the thread.** The composer's success tail ended in

        if(VIEW==='home'||VIEW==='global'||VIEW==='drafts') renderView(true);

and `thread` is not in that list. It cannot be: the thread is the one view that is not reached
through `renderView` at all — the route calls `renderThread(id)` — and `renderThread` holds no live
subscription, it is a one-shot query. So between those two facts there was no path by which a reply
you had just sent could reach the screen. It published, it went into the Store (publish() saves
optimistically), and the conversation in front of you did not change until you navigated away and
back, which re-ran the query. Exactly the report.

The decision now lives in `_repaintAfterPost`, a pure three-line function, because the bug was a
MISSING NAME IN A LIST — invisible inside a forty-line click handler, obvious next to a test.

**2. An incomplete reply query was rendered as the whole conversation.** The expansion fires
`#e` queries with a 6s budget and retries once when the answer did not EOSE. Both attempts can time
out on a phone radio, and the result was rendered anyway, under a confident "N replies" heading.
That is the same mistake the timeline and Trending were fixed for: a query no relay EOSE'd means
"the relays never spoke", never "there is nothing there". It looks like an Android-only bug for the
obvious reason — a desktop connection completes and a phone's does not — while the code is shared.

Both halves run the SHIPPED functions, lifted out of app.js rather than restated here.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "static/js/client/app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _src():
    return APP.read_text(encoding="utf-8")


def _lift(name, src=None):
    src = src if src is not None else _src()
    m = re.search(r"\n  (?:async )?function " + re.escape(name) + r"\(.*?\n  \}", src, re.S)
    assert m, f"{name} is gone from app.js"
    return m.group(0)


def _node(script):
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


# --------------------------------------------------------------------------- 1. the repaint


@pytest.mark.parametrize("view,expected", [
    ("thread", "thread"),          # THE BUG: this was None
    ("home", "view"),
    ("global", "view"),
    ("drafts", "view"),
    ("notifications", None),
    ("messages", None),
    ("profile", None),
    ("settings", None),
])
def test_a_post_repaints_the_view_it_was_made_from(view, expected):
    """`thread` returning None is the whole defect, and the other rows are what stops the fix from
    becoming "repaint everything" — a blanket re-render would blow away Messages mid-conversation
    and re-run the notification query on every post."""
    got = _node(_lift("_repaintAfterPost")
                + f"process.stdout.write(JSON.stringify(_repaintAfterPost({json.dumps(view)})));")
    assert got == expected


def test_the_composer_actually_uses_the_rule_and_repaints_the_open_thread():
    """The pure function is only worth anything if the click handler calls it. This pins the call
    site too, including that the thread branch re-renders the thread that is OPEN (`renderThread._tok`)
    rather than guessing an id."""
    src = _src()
    tail = src[src.index("if(r && r.ok) toast('posted');"):][:900]
    assert "_repaintAfterPost(VIEW)" in tail, "the composer no longer asks the rule"
    assert "renderThread._tok" in tail, "the thread branch does not re-render the open thread"
    assert "renderView(true)" in tail
    # And the old hardcoded list must not have grown back beside it.
    assert "VIEW==='home'||VIEW==='global'||VIEW==='drafts'" not in tail


def test_repainting_the_same_thread_keeps_the_readers_place():
    """A repaint that jumped to the top would be its own bug report. `renderThread` only resets the
    scroll when it is ARRIVING at a thread, which it decides by comparing the id it is about to
    render against the one it is already showing — so re-rendering `_tok` is the scroll-preserving
    case by construction."""
    src = _src()
    same = src[src.index("const _same = (renderThread._tok === id"):][:400]
    assert "VIEW === 'thread'" in same
    assert "_keepTop" in same, "the scroll-preserving branch is gone"


# --------------------------------------------------------------------------- 2. partial answers


def test_an_expansion_that_never_completed_is_not_reported_as_the_conversation():
    """The Android half. A `#e` query that times out returns a SHORT LIST rather than an error, so
    the count and the tree were both built from an answer nobody established. It now says so and
    offers the retry — and, importantly, does NOT claim 'No replies yet' when it simply could not
    ask, which is the same sentence being false for two completely different reasons."""
    src = _src()
    body = src[src.index("let expandedFully=true;"):src.index("_bindThreadBack(feed, id);")]
    assert "expandedFully=false" in body, "an incomplete retry no longer marks the expansion partial"
    assert "const partial = !expandedFully" in body
    assert "so far" in body, "the count is still presented as a settled number"
    assert "didn’t answer" in body, "nothing on screen says the relays were unreachable"
    assert "thread-retry" in body, "there is no way for the reader to ask again"
    # "No replies yet" is a claim. It may only be made when the query actually completed.
    empty = body[body.index("No replies yet") - 260:body.index("No replies yet") + 40]
    assert "partial" in empty, "'No replies yet' is still shown when the relays never answered"


def test_the_retry_button_is_bound():
    """A button that does nothing is worse than no button — and this one is rendered conditionally,
    so it is exactly the shape that gets drawn and never wired."""
    src = _src()
    bind = src[src.index("_bindThreadBack(feed, id);"):][:300]
    assert "thread-retry" in bind and "renderThread(id, hints)" in bind


def test_a_complete_expansion_says_nothing_extra():
    """The normal case must stay clean: no warning, no 'so far', on a thread that resolved."""
    src = _src()
    body = src[src.index("let expandedFully=true;"):src.index("_bindThreadBack(feed, id);")]
    # Both strings are inside a `partial ? … : …` choice rather than emitted unconditionally.
    for claim in ("so far", "didn’t answer"):
        idx = body.index(claim)
        assert "partial" in body[max(0, idx - 300):idx], f"{claim!r} is not gated on the partial flag"


def test_the_retry_is_still_attempted_before_anything_is_called_partial():
    """The flag must not fire on a first incomplete answer that the retry then satisfied — that
    would put a scary line on a thread which is, in the end, complete."""
    src = _src()
    body = src[src.index("let expandedFully=true;"):src.index("const all=[...merged.values()]")]
    retry = body[body.index("if(got && got.complete===false){"):]
    assert "const retry=await Relay.query" in retry
    assert retry.index("const retry=await Relay.query") < retry.index("expandedFully=false"), (
        "the expansion is marked partial before the retry has had its chance")
    assert "retry.complete===true" in retry
