"""Super+Down minimises a native application that is NOT hosted in a PosterChan frame.

Hosting native windows in frames is off by default, so Firefox and Telegram live only in the taskbar's
`nativeTasks`. The `pc:minimise-native:<id>` tick used to look only at hosted frames and silently did
nothing for them (measured on an isolated copy of the installed desktop). Runs the SHIPPED
`minimiseNativeById` from os.js under node.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

OS = (Path(__file__).parents[2] / "static/js/client/os.js").read_text(encoding="utf-8")


def _function(name):
    start = OS.index(f"function {name}(")
    brace = OS.index("{", start)
    depth = 0
    for pos in range(brace, len(OS)):
        if OS[pos] == "{":
            depth += 1
        elif OS[pos] == "}":
            depth -= 1
            if depth == 0:
                return OS[start:pos + 1]
    raise AssertionError(name)


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_unhosted_native_window_is_minimised_through_the_taskbar_path():
    script = f"""
const hidden=[], minimised=[];
let hosted=[], nativeTasks=[];
const nativeWins=()=>hosted;
const minimise=w=>minimised.push(w.native);
const pcWM={{hide:id=>{{hidden.push(id);return Promise.resolve({{result:'ok'}});}}}};
{_function('minimiseNativeById')}
const out={{}};
nativeTasks=[{{id:9,app:'firefox'}}];
out.unhosted=minimiseNativeById(9); out.hidden=hidden.slice();
hosted=[{{native:12}}];
out.hosted=minimiseNativeById(12); out.minimised=minimised.slice();
out.other=minimiseNativeById(44); out.hiddenAfter=hidden.slice();
process.stdout.write(JSON.stringify(out));
"""
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert got["unhosted"] == "task" and got["hidden"] == [9]
    assert got["hosted"] == "hosted" and got["minimised"] == [12]
    assert got["other"] is False and got["hiddenAfter"] == [9], "another output's window is left alone"


def test_the_tick_routes_through_the_helper():
    branch = OS[OS.index("/^pc:minimise-native:\\d+$/.test(p)"):]
    branch = branch[:branch.index("else if(")]
    assert "minimiseNativeById(Number(p.slice(19)))" in branch
