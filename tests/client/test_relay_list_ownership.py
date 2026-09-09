"""PosterChan SETS UP the relays and never removes them for the user.

Two halves of one rule, and both were broken in opposite directions:

  * we PUBLISHED kind 10002 out of this device's localStorage and never READ the user's own, so
    somebody arriving with a list from Amethyst or Damus saw our single default relay and the first
    Save that touched the relay controls replaced their list GLOBALLY — the replaceable-list wipe
    that has already cost a follows list once;
  * `_dropLegacyAutoRelays` DELETED a saved list on every `connectRelays()`, so a user who
    deliberately chose exactly the six relays an older build had suggested lost them at boot,
    re-entered them, and lost them again on the next launch, with no way to say "I meant it".

The runtime half runs the shipped functions against a stub relay and a stub localStorage, because
neither failure is visible in the source: one is a list that gets SHORTER, the other a publish that
goes out on a read nobody answered. Each rule in it was verified to fail on its own mutation.
"""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def test_the_relay_list_is_additive_and_the_publish_is_gated():
    result = subprocess.run(['node', str(ROOT / 'tests/client/relay_list_ownership_runtime.mjs')],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_kind_10002_write_needs_a_confirmed_read_of_the_existing_list():
    """The publish lives in the global Save handler, which is far too entangled to drive under node.

    It is the one place that REPLACES the user's relay list for every client they own, so what is
    asserted here is the gate itself: the write is reachable only from the branch that a COMPLETE
    read of their existing 10002 unlocks, and the refusal is spoken rather than silent.
    """
    i = APP.index("const relayChanged =")
    block = APP[i:APP.index("if($('input[name=media-mode]')", i)]
    publish = re.search(r"^\s*if\(_nip65Confirmed\) await publish\(10002,", block, re.M)
    assert publish, "the kind-10002 write is not gated on a confirmed read of the existing list"
    assert re.search(r"else toast\(", block), \
        "a refused publish must say so — a save that quietly did half of what it said is worse"
    # And the flag can only ever be set by an answer every relay EOSE'd.
    seed = APP[APP.index("  async function seedRelaysFromNip65(){"):]
    seed = seed[:seed.index("\n  }")]
    assert "if(!evs || evs.complete!==true) return false;" in seed
    assert seed.index("evs.complete!==true") < seed.index("_nip65Confirmed = true"), \
        "'no relay answered' must be rejected BEFORE the publish is unlocked"


def test_seeding_never_publishes():
    """Reading somebody's relay list must not write one back. A seed that republishes is the wipe
    wearing the fix's clothes: it would take a list assembled from a partial read and make it the
    newest global one."""
    seed = APP[APP.index("  async function seedRelaysFromNip65(){"):]
    seed = seed[:seed.index("\n  }")]
    assert "publish(" not in seed, "the seed path must never publish"
