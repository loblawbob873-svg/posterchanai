"""What the preview says it WOULD do must equal what the sweep DOES.

Reported: "would upload shows new files, sync says in step on phone" — two surfaces, one folder,
opposite answers, and nothing to tell that apart from a broken sweep.

Two defects behind that shape, both mine, both from the batching:

  * the dry run was EXCLUDED from batching, so the preview ran the whole-folder path while the sweep
    ran the batched one. They happen to agree numerically today, so this is a structural fix rather
    than one these numbers expose — a preview whose job is to say what the sweep will do has to be
    the sweep, minus the writing, or the next divergence is silent too.
  * the batched report carried no `plan`. `summarise` reads `rep.plan` for the preview and `details`
    reads it for the card, so a batched sweep could only ever say "in step", whatever it found. That
    one IS exposed: drop the merge and the simulation reports "the preview said it would upload 0 and
    the sweep uploaded 5".

A dry run must also stay READ-ONLY — a preview that advances the agreement would make the sweep after
it find nothing to do, which is the same lie arriving a second later.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "preview_agrees_sim.js")


def _run(old, new, timeout=300):
    if shutil.which("node") is None:
        pytest.skip("no node")
    r = subprocess.run(["node", SIM, str(old), str(new)],
                       capture_output=True, text=True, timeout=timeout)
    out = r.stdout.strip()
    assert out, "the simulation printed nothing:\n" + r.stderr[-3000:]
    got = json.loads(out[out.index("{"):])
    assert not got["failures"], "\n".join(got["failures"])
    assert r.returncode == 0, r.stderr[-2000:]
    return got


@pytest.mark.parametrize("regime", ["agreedBase", "emptyBase"])
def test_the_preview_and_the_sweep_give_one_answer(regime):
    """Both regimes: a folder already in step that gains new photos, and one whose agreement is empty
    (a re-add, a reinstall, a device joining) — the second is the one that batches."""
    got = _run(2000, 5)
    r = got[regime]
    assert r["would"] == r["did"], "preview %s, sweep %s" % (r["would"], r["did"])
    assert r["did"] == 5
    assert r["previewHasPlan"] and r["sweepHasPlan"], \
        "a report with no plan can only ever say 'in step'"


def test_every_sweep_batches_not_just_the_first():
    """THE TRAP THIS CLOSED. Batching used to apply only when the agreement was empty, so an
    interrupted first sweep left a partial agreement and the NEXT sweep took the single-pass
    whole-folder path — precisely what batching exists to prevent. On a folder big enough to need it
    that is a loop: sweep, die, reload, start again, for ever. Reported as a tablet going "back to
    scanning during download".

    So both regimes batch now, and this asserts it rather than trusting the code to have meant it."""
    got = _run(2000, 5)
    assert got["emptyBase"]["batches"] > 1
    assert got["agreedBase"]["batches"] > 1, \
        "an incremental sweep of a large folder is back on the single-pass path"


def test_a_small_folder_agrees_too():
    got = _run(300, 2)
    assert got["agreedBase"]["would"] == got["agreedBase"]["did"] == 2
