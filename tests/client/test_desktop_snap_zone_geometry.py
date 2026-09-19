"""DESKTOP SNAP ZONES: corner quarters, and corners beat edges (backlog §3, item 6).

The mouse edge/corner snapping is pure geometry in os.js — `zones()` returns the tiling rectangles
for the work area, `zoneAt(x,y)` hit-tests a pointer to a zone, and `rectOf(z)` turns a zone into a
CSS rect with the edge gutter. Until now only source-greps and a live desktop gate touched it. This
RUNS the shipped functions under node against a known work area, so a mutation to the quarter math or
the corner-before-edge ordering fails here.

The cross-monitor completion of a snap still needs a real second monitor (the live gate
`scripts/check_installed_native_snap.py`); this pins the geometry that decides where a window lands.
"""
import json
import re
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


def _const(name):
    m = re.search(rf"const {name}\s*=\s*(\d+)", OS)
    assert m, f"{name} constant not found"
    return m.group(1)


def _run(body):
    script = (
        f"const EDGE={_const('EDGE')}, SNAP={_const('SNAP')};\n"
        "let WORK={width:2000,height:1000};\n"
        "let AREA={left:0,top:0,right:2000,bottom:1000};\n"
        "function snapWorkArea(){ return WORK; }\n"
        "function snapPointerArea(){ return AREA; }\n"
        + _fn("zones") + "\n" + _fn("zoneAt") + "\n" + _fn("rectOf") + "\n"
        "const out={};\n" + body + "\n"
        "process.stdout.write(JSON.stringify(out));\n")
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-600:]
    return json.loads(r.stdout)


def test_the_zones_are_exact_quarters_and_halves():
    assert NODE, "no node"
    out = _run("out.z = zones();")
    z = out["z"]
    # 2000x1000 work area → halves are 1000 wide, quarters are 1000x500.
    assert z["max"] == {"x": 0, "y": 0, "w": 2000, "h": 1000}
    assert z["left"] == {"x": 0, "y": 0, "w": 1000, "h": 1000}
    assert z["right"] == {"x": 1000, "y": 0, "w": 1000, "h": 1000}
    assert z["tl"] == {"x": 0, "y": 0, "w": 1000, "h": 500}
    assert z["tr"] == {"x": 1000, "y": 0, "w": 1000, "h": 500}
    assert z["bl"] == {"x": 0, "y": 500, "w": 1000, "h": 500}
    assert z["br"] == {"x": 1000, "y": 500, "w": 1000, "h": 500}


def test_a_corner_snaps_to_a_quarter_not_a_half():
    """The whole point of item 6's corners: a pointer in a corner must resolve to that QUARTER,
    never to the top-edge maximize or a side half. This is the corner-before-edge ordering."""
    assert NODE, "no node"
    out = _run(
        "out.tl = zoneAt(5,5);\n"
        "out.tr = zoneAt(1995,5);\n"
        "out.bl = zoneAt(5,995);\n"
        "out.br = zoneAt(1995,995);\n")
    assert out == {"tl": "tl", "tr": "tr", "bl": "bl", "br": "br"}, out


def test_edges_and_centre_resolve_correctly():
    assert NODE, "no node"
    out = _run(
        "out.top = zoneAt(1000,5);\n"       # top edge, away from corners → maximize
        "out.left = zoneAt(5,500);\n"       # left edge, mid height → left half
        "out.right = zoneAt(1995,500);\n"   # right edge, mid height → right half
        "out.centre = zoneAt(1000,500);\n") # nowhere near an edge → no snap
    assert out == {"top": "max", "left": "left", "right": "right", "centre": ""}, out


def test_rectof_applies_the_edge_gutter():
    assert NODE, "no node"
    out = _run("out.tl = rectOf('tl');")
    # tl quarter is 1000x500 at 0,0; SNAP=8 gutter on every side.
    assert out["tl"] == {"left": "8px", "top": "8px", "width": "984px", "height": "484px"}, out
