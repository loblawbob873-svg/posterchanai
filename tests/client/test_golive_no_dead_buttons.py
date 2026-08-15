"""A click that starts a network round trip must not look identical to a broken button.

THE REPORT, and it is one bug wearing three faces:

    "After streaming, I clicked Delete for the current stream, clicked Go Live, then 'Choose from
     drive' no longer works" … "ctrl+r in the windows app did not solve it either" … "announce and
     stream button also does nothing" … "eventually worked, after many retries".

Nothing was broken and nothing was stuck. Every one of those buttons was WAITING:

  * "Choose from your drive" ran `FilesIdx.pull()` (sign + POST /client/files-index + possibly a
    blob fetch) and then `fetch(<blossom>/list/<pubkey>)`, and only THEN called subModal(). Neither
    request had a deadline — a fetch has none of its own, and Chromium will sit on a stalled one for
    minutes — so for that whole time the button produced no sheet, no spinner and no error. The
    retry that "worked" was the one whose fetch came back.
  * "Announce and Stream" is disabled until a source radio is picked, and said so nowhere. It is
    also a relay round trip once pressed (sign → publish → the relay must have STORED it) with no
    busy state, so a slow relay meant clicking it again, and each click signed and published another
    kind-30311.
  * Go Live itself awaits `ensureAiSession()` + `/api/streams/ingest` before the sheet exists, and
    reported an UNREACHABLE server as "streaming isn't enabled on this server" — which sends you off
    to check settings that are fine.

So the rule, the same one as cache-first paint (see test_cache_first_paint.py) applied to controls:

    Open the surface BEFORE the first network await, bound every request a click waits on, and never
    let "I could not ask" render as "there is nothing".

These are source-shape assertions on purpose: the failure is a TIMING one that only appears on a
slow link, so a functional test on a fast loopback passes against the broken code — which is exactly
how this shipped.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APPJS = os.path.join(ROOT, "static", "js", "client", "app.js")


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
def picker():
    return _fn("async function _pickBlossomImage(onPick){")


@pytest.fixture(scope="module")
def golive():
    return _fn("async function _goLive(){")


# ---------------------------------------------------------------- the cover picker

def test_the_cover_sheet_opens_before_the_first_network_await(picker):
    """The whole bug: the sheet was built AFTER the drive was read."""
    opened = picker.index("subModal(")
    first_await = picker.index("await ")
    assert opened < first_await, (
        "_pickBlossomImage awaits the network before it opens its sheet — on a slow link that is a "
        "button that does nothing at all, for as long as the request takes"
    )


def test_reading_the_drive_is_time_bounded(picker):
    """A stalled fetch has no timeout of its own, so both legs need one."""
    assert "_fetchTimeout(server+'/list/'" in picker, \
        "the drive listing must go through _fetchTimeout — a bare fetch() can stall for minutes"
    # `ensure()`, not `pull()`: the four call sites that each latched "pull once" on the ATTEMPT
    # (so one failed pull hid every folder on the page until a reload) went through one gate that
    # latches on the RESULT — see tests/client/test_files_index_pull_retries.py. The rule this test
    # exists for is unchanged and still the point: whatever reads the drive here is raced.
    assert re.search(r"Promise\.race\(\s*\[\s*FilesIdx\.ensure\(\)", picker), \
        "the index read must be raced against a timeout; one that never answers held the picker shut"


def test_fetchtimeout_actually_aborts():
    """The helper has to abort the request, not merely resolve something else."""
    helper = _fn("function _fetchTimeout(url, opts, ms){")
    assert "AbortController" in helper and "abort()" in helper, \
        "_fetchTimeout must abort the underlying request"
    assert "clearTimeout" in helper, "…and must not leave its timer armed on the happy path"


def test_an_unreachable_drive_is_not_reported_as_an_empty_one(picker):
    """`[]` from a failed read and `[]` from an empty drive are different answers."""
    assert "let list=null" in picker, \
        "the listing must start as null so a failed read is distinguishable from an empty drive"
    unreachable = picker.index("if(!list){")
    empty = picker.index("if(!imgs.length){")
    assert unreachable < empty
    assert "Couldn’t reach your drive" in picker
    assert "Retry" in picker, "an unreachable drive must offer the retry, not send you off to upload"


def test_the_picker_survives_being_closed_while_it_loads(picker):
    """The sheet is up during the awaits now, so the user can close it mid-read."""
    assert picker.count("if(!alive()) return;") >= 2, \
        "every await must be followed by an is-this-sheet-still-open check"


def test_only_one_picker_at_a_time(picker):
    assert ".bp-modal" in picker.split("subModal(")[0], \
        "a second click must not stack a second cover sheet over a live one"


# ---------------------------------------------------------------- the Go Live sheet

def test_the_disabled_announce_button_says_why(golive):
    assert 'id="gl-need"' in golive, "the sheet needs the line that explains a disabled Announce button"
    assert re.search(r"\$\('#gl-need',root\).*classList\.toggle\('hidden', !off\)", golive, re.S), \
        "…and it must be toggled by the same condition that disables the button, or it lies"


def test_announcing_cannot_be_started_twice(golive):
    go = golive[golive.index("$('#gl-go',root).onclick"):]
    assert "if(_going) return;" in go, \
        "announcing is a relay round trip; without a guard each impatient click publishes another 30311"
    assert "Announcing…" in go, "…and it must SAY it is working, or it reads as a dead button"
    assert "revive()" in go, "a failed announce must give the button back"


def test_opening_the_sheet_is_bounded_and_names_the_real_failure(golive):
    assert "_glOpening" in golive, "a slow open must not stack two Go Live sheets"
    assert "_fetchTimeout('/api/streams/ingest'" in golive, \
        "the ingest request must have a deadline — it is what the sheet waits on"
    assert "couldn’t reach the server" in golive, \
        "an unreachable server must not be reported as 'streaming isn't enabled on this server'"
