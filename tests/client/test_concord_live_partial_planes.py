"""New messages must arrive in a room you are only partly granted.

Reported as "I had to change Concord rooms and click on my room again to see new messages" — which
reads as a refresh bug and is actually an access one. `cordPlaneSubscribe` builds one live socket
per author, and `cordPlaneAuth` throws for any author whose plane key this membership does not
hold. That throw tore down EVERY subscription already made and propagated, so a partially granted
membership got no live stream at all: history loaded, then nothing new ever arrived. Switching
rooms rebuilds the subscription from scratch, which is why it briefly appeared to fix it.

This is the live half of the same rule cordQuery already follows for history.
"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")


def _run(held, authors=("aa", "bb", "cc")):
    body = SRC[SRC.index("  function cordPlaneAuth("):]
    body = body[:body.index("\n  async function cordQuery(")]
    script = f"""
const HELD=new Set({list(held)!r});
globalThis.window={{PosterCordReader:{{createPlaneAuth:(b,c,author)=>{{
  if(!HELD.has(author))throw new Error('Concord plane key is not held by this membership');
  return {{pubkey:author}};}}}}}};
{body}
const made=[];
const R={{subscribeFrom:(relays,filters,opts)=>{{
  const a=filters[0].authors[0];made.push({{author:a,live:!!opts.live}});
  const stop=()=>{{made.find(m=>m.author===a).stopped=true;}};
  stop.hasTargets=true;stop.ready=Promise.resolve(true);return stop;}}}};
const plane={{bundle:{{}},controls:[],current:()=>true}};
try{{
  const stop=cordPlaneSubscribe({{}},R,['wss://r'],[{{kinds:[1059],authors:{list(authors)!r}}}],
                                {{onEvent(){{}},timeout:0,live:true}},plane);
  console.log('OK '+JSON.stringify(made));
}}catch(e){{console.log('THREW '+e.message);}}
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_streams_we_can_read_stay_subscribed_when_one_key_is_missing():
    got = _run(["aa", "cc"])
    assert got.startswith("OK "), got
    assert '"author":"aa"' in got and '"author":"cc"' in got, got
    assert '"author":"bb"' not in got, got


def test_the_surviving_streams_are_still_live():
    """Without live the socket never redials and the room goes deaf on the first blip."""
    got = _run(["aa"])
    assert '"live":true' in got, got


def test_nothing_is_torn_down_by_one_failure():
    """The old code called stop() on every subscription already made before rethrowing."""
    assert '"stopped":true' not in _run(["aa", "cc"])


def test_a_membership_holding_nothing_still_raises():
    assert _run([]).startswith("THREW Concord plane key is not held")


def test_the_socket_budget_is_still_respected():
    """At most eight live sockets overall; with the default relay cap that is two per plane call.
    Tolerating a failure must not become a way to exceed it."""
    got = _run(["aa", "bb", "cc"])
    assert got.count('"author"') == 2, got


def test_the_budget_is_spent_on_authors_we_can_actually_read():
    """An un-held author used to abort everything; it must not instead silently consume a slot and
    leave a readable stream unsubscribed."""
    got = _run(["bb", "cc"], authors=("aa", "bb", "cc"))
    assert '"author":"bb"' in got and '"author":"cc"' in got, got
    assert '"author":"aa"' not in got, got
