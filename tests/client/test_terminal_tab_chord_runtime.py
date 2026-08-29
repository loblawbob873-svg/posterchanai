"""Ctrl/Command+Page keys switch tabs from both terminal keyboard surfaces."""

import json
from pathlib import Path
import subprocess


TERM = (Path(__file__).parents[2] / "static/js/client/term.js").read_text(encoding="utf-8")


def _function(name):
    start = TERM.index(f"function {name}(")
    brace = TERM.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for pos in range(brace, len(TERM)):
        char = TERM[pos]
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
                return TERM[start:pos + 1]
    raise AssertionError(f"unterminated {name}")


def test_tab_chord_runtime_routes_directions_and_leaves_plain_page_keys_alone():
    script = f"""
let steps=[],prevented=0;
const _cycleTab=x=>steps.push(x);
{_function('_tabChord')}
const ev=(key,mods={{}})=>Object.assign({{type:'keydown',key,preventDefault(){{prevented++}}}},mods);
const results=[_tabChord(ev('PageUp',{{ctrlKey:true}})),
  _tabChord(ev('PageDown',{{metaKey:true}})),_tabChord(ev('PageUp')),
  _tabChord(ev('ArrowUp',{{ctrlKey:true}}))];
process.stdout.write(JSON.stringify({{steps,prevented,results}}));
"""
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert got == {"steps": [-1, 1], "prevented": 2,
                   "results": [True, True, False, False]}


def test_xterm_and_mobile_catcher_share_the_tab_chord_owner():
    assert TERM.count("if(_tabChord(ev))") == 2
    catcher = TERM[TERM.index("c.onkeydown = (ev)") : TERM.index("window.addEventListener", TERM.index("c.onkeydown = (ev)"))]
    assert catcher.index("if(_tabChord(ev))") < catcher.index("if(ev.key === 'Enter')")
