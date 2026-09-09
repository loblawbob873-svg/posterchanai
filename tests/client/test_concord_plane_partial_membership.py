"""One plane key this membership does not hold must not blank the whole room.

Vector's author-gated planes require AUTH as the DERIVED PLANE key, not the generic user key, so
`createPlaneAuth` throws "Concord plane key is not held by this membership" for any author whose
key a partially granted membership lacks. That call sat inside cordQuery's per-author loop with
nothing catching it, so a single un-held stream key aborted the entire history — every other
author and all six pagination pages — and the room rendered empty with nothing saying why.

The generic (non-plane) branch has always been tolerant: Promise.allSettled, throwing only when
EVERY job failed. These run the shipped cordQuery to prove the plane branch now matches that.
"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")


def _harness(tmp_path, held):
    """Run the real cordQuery with a reader whose plane keys are only `held`."""
    src = SRC[SRC.index("async function cordQuery("):]
    src = src[:src.index("\n  async function queryEnvelopeHistory")]
    helper = SRC[SRC.index("function cordPlaneAuth("):]
    helper = helper[:helper.index("\n  function cordPlaneSubscribe")]
    script = f"""
const HELD=new Set({held!r});
globalThis.window={{PosterCordReader:{{createPlaneAuth:(b,c,author)=>{{
  if(!HELD.has(author))throw new Error('Concord plane key is not held by this membership');
  return {{pubkey:author,sign:()=>({{}})}};}}}}}};
{helper}
{src}
const p={{relayQueryFrom:async(relays,filters)=>{{
  const a=filters[0].authors[0];return [{{id:'ev-'+a,created_at:1,pubkey:a}}];}}}};
const plane={{bundle:{{}},controls:[],current:()=>true}};
cordQuery(p,['wss://r.invalid'],[{{kinds:[1059],authors:['aa','bb','cc'],limit:10}}],{{plane}})
  .then(r=>console.log('OK '+JSON.stringify(r.map(e=>e.pubkey))+' unreadable='+JSON.stringify(cordQuery.lastUnreadablePlanes)))
  .catch(e=>console.log('THREW '+e.message));
"""
    f = tmp_path / "q.mjs"; f.write_text(script)
    out = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_authors_we_can_read_still_arrive_when_one_key_is_missing(tmp_path):
    got = _harness(tmp_path, ["aa", "cc"])
    assert got.startswith("OK "), got
    assert '"aa"' in got and '"cc"' in got, got
    assert '"bb"' not in got.split("unreadable=")[0], got


def test_the_unreadable_author_is_reported_not_hidden(tmp_path):
    assert 'unreadable=["bb"]' in _harness(tmp_path, ["aa", "cc"])


def test_a_membership_holding_nothing_still_raises(tmp_path):
    """Total failure must stay an error — the same contract the generic branch has."""
    assert _harness(tmp_path, []).startswith("THREW Concord plane key is not held")


def test_a_fully_granted_membership_is_unaffected(tmp_path):
    got = _harness(tmp_path, ["aa", "bb", "cc"])
    assert got.startswith("OK ") and 'unreadable=[]' in got, got
