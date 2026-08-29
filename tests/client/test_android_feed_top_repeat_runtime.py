"""Native double-HOME remains a repeatable scroll-to-top action."""

import json
from pathlib import Path
import subprocess


PHONE = (Path(__file__).parents[2] / "static/js/client/phoneshell.js").read_text(encoding="utf-8")


def _function(name):
    start = PHONE.index(f"function {name}(")
    if name == "consumeLaunchView":
        return PHONE[start:PHONE.index("\n\n  async function status", start)]
    brace = PHONE.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for pos in range(brace, len(PHONE)):
        char = PHONE[pos]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return PHONE[start:pos + 1]
    raise AssertionError(f"unterminated {name}")


def test_two_intentional_feed_top_actions_inside_carrier_window_both_land():
    script = f"""
let _launchQueue=Promise.resolve(),_lastLaunchView='',_lastLaunchAt=0,landed=[];
let PC={{capPlugin:()=>({{consumeLaunchView:async()=>({{view:''}})}})}};
const plug=()=>PC.capPlugin(),landView=v=>landed.push(v),window={{}},setTimeout=()=>{{}};
let now=1000;Date.now=()=>now;
{_function('consumeLaunchView')}
(async()=>{{
  await consumeLaunchView('__feed_top');now=1500;await consumeLaunchView('__feed_top');
  now=1600;await consumeLaunchView('notes');now=1700;await consumeLaunchView('notes');
  process.stdout.write(JSON.stringify(landed));
}})().catch(e=>{{console.error(e);process.exitCode=1}});
"""
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert got == ["__feed_top", "__feed_top", "notes"]


def test_feed_top_still_uses_active_or_configured_timeline_not_global():
    assert "PC.timelineTop()" in PHONE
    assert "PC.timelineTop('global')" not in PHONE
