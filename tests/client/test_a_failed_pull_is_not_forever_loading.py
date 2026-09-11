""""CAN'T EVEN UPLOAD TO FILES NOW — STILL LOADING FOLDERS DESPITE LOADED."

`FilesIdx.pull()` set `_pullDone = true` as the LAST STATEMENT INSIDE its `try { … } catch(_){}`.
So any failure — an unreachable server, a blob that would not fetch, a decrypt that threw — was
swallowed by that empty catch and the flag stayed false with nothing left to set it. The upload
guard reads that flag:

    if(!music && (folder || _importsFolder) && !FilesIdx._pullDone){
      toast('One sec — still loading your folders. Try that again in a moment.'); return; }

Both halves of that sentence are untrue after a failed pull: nothing is loading, and trying again
never helps. Uploading to a folder is dead for the rest of the session.

THE FILE ALREADY STATED THE RULE IT BROKE, on the flags themselves:

    _pullDone = a pull attempt finished. _pullOk = the pull actually MATERIALISED the index (or
    proved the server has none). _pullBlocked = the server HAS an index we could not read — the one
    state in which writing anything would destroy it. The three are not the same thing.

A finished attempt is finished whether or not it worked, so `_pullDone` belongs in a `finally`.
Safety does not live there: a throw leaves `_pullOk` false and `_pullBlocked` false, so this cannot
make a dangerous write look safe — the "server has an index we could not read" state is still only
reached by actually reading a pointer.

Same family as the DM-inbox lookup fixed the same day: "could not ask" is not a fact about the
thing you were asking about.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _pull_tail():
    """The end of FilesIdx.pull(), where the flag is set."""
    at = APP.index("        this._pullDone=true;")
    return APP[at - 1400:at + 200]


def test_the_flag_is_set_however_the_pull_ends():
    tail = _pull_tail()
    assert "}finally{" in tail.replace(" ", ""), (
        "_pullDone is set inside the try again, so a failed pull leaves it false for ever and "
        "every upload to a folder answers 'still loading your folders'")
    after = tail[tail.index("}finally{"):]
    assert "_pullDone=true" in after.replace(" ", ""), "the flag is no longer set in the finally"


def test_the_catch_is_not_silent_about_why_it_exists():
    """An empty `catch(_){}` next to a latch is how this happened. The next person to read it has
    to see that swallowing the error is deliberate and that the flag is handled elsewhere."""
    tail = _pull_tail()
    body = tail[tail.index("}catch(_){"):tail.index("}finally{")]
    assert len(body) > 200, "the catch is silent again — nothing records why a throw is survivable"


def test_failing_a_pull_never_makes_an_unsafe_write_look_safe():
    """The reason `_pullDone` can move but the others cannot: writing is gated on `_pullOk` /
    `_pullBlocked`, and a throw leaves both false. Conflating _pullDone with 'we have the index' is
    what wiped a drive's folders once — that must stay impossible."""
    tail = _pull_tail()
    fin = tail[tail.index("}finally{"):]
    assert "_pullOk" not in fin, "_pullOk is being set in the finally — a failed pull would look like a good one"
    assert "_pullBlocked" not in fin, "_pullBlocked is being cleared in the finally"
    save = APP[APP.index("    async _saveOnce(){"):]
    save = save[:save.index("\n    },")]
    # The save gate is POSITIVE CONFIRMATION (`_pullOk`), not the absence of an error flag — its own
    # comment says so, having been written after a drive lost its folders. A throw leaves _pullOk
    # false, so moving _pullDone into a finally cannot let an unconfirmed save through.
    assert "_pullOk" in save, (
        "the SAVE path no longer requires confirmation of what is on the server — that is the state "
        "that once wiped a drive's folders while all its blobs sat untouched")
    assert "_pullDone" not in save, (
        "the save path now consults _pullDone, which is exactly the conflation the flag comment "
        "warns about: a finished ATTEMPT is not a read index")


def test_the_upload_guard_still_reads_the_attempt_flag():
    """If the guard moved to a different flag this whole file is measuring the wrong thing."""
    # The CODE line, not the comment that quotes it — this file now contains the sentence too.
    line = [l for l in APP.splitlines()
            if "still loading your folders" in l and "toast(" in l and "*" not in l.strip()[:2]]
    assert line, "the upload guard's message changed — re-point this test"
    assert "_pullDone" in line[0], (
        "the upload guard no longer reads _pullDone, so this file no longer describes the bug")


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_a_throwing_pull_still_finishes():
    """RUN the shape. A `try` whose last statement is the latch versus one with a `finally`: the
    difference is invisible on the happy path and total on the failing one, which is exactly why it
    survived in the code for so long."""
    script = r"""
      const mk = (useFinally) => ({
        _pullDone: false,
        async pull(shouldThrow) {
          try {
            if (shouldThrow) throw new Error('relay unreachable');
            if (!useFinally) this._pullDone = true;
          } catch (_) {
          } finally { if (useFinally) this._pullDone = true; }
        },
      });
      const out = {};
      const old = mk(false), now = mk(true);
      await old.pull(true); out.oldAfterFailure = old._pullDone;
      await now.pull(true); out.newAfterFailure = now._pullDone;
      const ok = mk(true); await ok.pull(false); out.newAfterSuccess = ok._pullDone;
      console.log(JSON.stringify(out));
    """
    done = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-800:]
    got = json.loads(done.stdout.strip().splitlines()[-1])
    assert got["oldAfterFailure"] is False, "the old shape is not the bug this file describes"
    assert got["newAfterFailure"] is True, "a failed pull still never finishes"
    assert got["newAfterSuccess"] is True
