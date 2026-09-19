"""PLAYING MEDIA SURVIVES A MONITOR MOVE AT THE SAME TIME/STATE (backlog §3, items 3-4).

When a PosterChan window is handed to another monitor it is rebuilt on the destination, and
`restoreHandoffUI` in os.js reapplies the captured state to the new DOM. The forms and scroll legs
were tested; the MEDIA leg (currentTime/volume/muted/playbackRate, and resuming playback when it was
not paused) was not, for a general window. This RUNS the shipped `restoreHandoffUI` under node with a
stubbed root and a fake <video>, so a mutation to the media restore fails here.

Matching is by IDENTITY (element id), not ordinal — a rebuilt window can lay its media out in a
different order, and restoring by position would drive the wrong element.
"""
import json
import shutil
import subprocess
from pathlib import Path

OS = (Path(__file__).parents[2] / "static/js/client/os.js").read_text(encoding="utf-8")
NODE = shutil.which("node") or shutil.which("nodejs")


def _fn(name):
    start = OS.index(f"function {name}(")
    brace = OS.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for pos in range(brace, len(OS)):
        c = OS[pos]
        if quote:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == quote:
                quote = None
            continue
        if c in "'\"`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return OS[start:pos + 1]
    raise AssertionError(f"unterminated {name}")


def _run(media_json):
    # Two fake <video> elements; the snap names the SECOND by id, so a by-position restore would
    # drive the first. Every observer/timer is a no-op, leaving only restoreHandoffUI's final
    # synchronous apply().
    script = f"""
function mk(id){{ return {{ id, currentTime:0, volume:1, muted:false, playbackRate:1, played:false,
  play(){{ this.played=true; return {{catch(){{}}}}; }} }}; }}
const vids=[mk('vidA'), mk('vidB')];
const root={{ querySelectorAll(sel){{ return (sel==='*')?[]:vids; }} }};
const wins=[{{}}]; const w=wins[0]; w.body={{}};
function _handoffRoot(){{ return root; }}
function _findHandoffField(){{ return null; }}
class MutationObserver{{ observe(){{}} disconnect(){{}} }}
const requestAnimationFrame=()=>0, setTimeout=()=>0, clearTimeout=()=>0;
const Event=function(){{}};
{_fn('restoreHandoffUI')}
restoreHandoffUI(w, {{ media: {media_json} }});
const out={{ a:vids[0], b:vids[1] }};
process.stdout.write(JSON.stringify(out));
"""
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-700:]
    return json.loads(r.stdout)


def test_media_state_is_restored_by_identity_and_resumes():
    assert NODE, "no node"
    out = _run('[{"id":"vidB","time":12.25,"volume":0.5,"muted":true,"rate":1.5,"paused":false}]')
    b, a = out["b"], out["a"]
    assert b["currentTime"] == 12.25, "playback position was not restored"
    assert b["volume"] == 0.5, "volume was not restored"
    assert b["muted"] is True, "mute state was not restored"
    assert b["playbackRate"] == 1.5, "playback rate was not restored"
    assert b["played"] is True, "playing media did not resume after the move"
    # By identity, not ordinal: the FIRST element (vidA) must be untouched.
    assert a["currentTime"] == 0 and a["played"] is False, "restore drove the wrong element (by index)"


def test_paused_media_is_not_forced_to_play():
    assert NODE, "no node"
    out = _run('[{"id":"vidB","time":3,"paused":true}]')
    assert out["b"]["currentTime"] == 3, "paused media lost its position"
    assert out["b"]["played"] is False, "media that was paused was force-played after the move"
