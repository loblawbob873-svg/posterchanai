"""Telegram cannot remain above the PosterChan application the user focused."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT=Path(__file__).resolve().parents[2]
OS=(ROOT/"static/js/client/os.js").read_text(encoding="utf-8")


def _plan(rect):
    program=f"""
      const N=require({json.dumps(str(ROOT/'static/js/client/osnative.js'))});
      process.stdout.write(JSON.stringify(N.domStackPlan([
        {{id:319,own:false,fullscreen:false,rect:{{x:500,y:300,width:900,height:900}}}},
        {{id:1005,own:true,fullscreen:false,rect:{{x:100,y:100,width:1200,height:1200}}}},
        {{id:88,own:false,fullscreen:true,rect:{{x:0,y:0,width:1920,height:1080}}}}
      ],{json.dumps(rect)})));
    """
    done=subprocess.run(["node","-e",program],capture_output=True,text=True,timeout=30)
    assert done.returncode==0,done.stderr
    return json.loads(done.stdout)


def test_dom_app_focus_parks_overlapping_telegram_but_not_our_real_window_or_fullscreen_game():
    assert _plan({"left":300,"top":200,"width":1000,"height":1000}) == {"hide":[319],"show":[]}


def test_non_overlapping_dom_app_does_not_park_telegram():
    assert _plan({"left":1500,"top":100,"width":300,"height":300}) == {"hide":[],"show":[319]}


def test_every_dom_app_shape_uses_the_same_central_stacking_path():
    focus=OS[OS.index("function focusWin(w, render)"):OS.index("function minimise",OS.index("function focusWin(w, render)"))]
    assert "else if(w.native == null) _stackDomAboveNative(w)" in focus
    for view in ("global","messages","concord","monero","office","drafts","settings","terminal"):
        assert view in OS


def test_real_posterchan_toplevel_uses_compositor_focus_after_releasing_dom_parking():
    branch=OS.split("const mine = nativeTasks.find",1)[1].split("let real = null",1)[0]
    assert "_releaseDomCoveredNative(mine.id)" in branch
    assert "_focusNativeDecorated(mine.id)" in branch
    assert branch.index("_releaseDomCoveredNative(mine.id)") < branch.index("_focusNativeDecorated(mine.id)")
    assert "++_domStackGen" in OS


def test_native_taskbar_restore_releases_dom_owned_parking_before_focus():
    handler=OS[OS.index("if(b.dataset.kind === 'native')"):OS.index("const w = wins.find",OS.index("if(b.dataset.kind === 'native')"))]
    assert "await _releaseDomCoveredNative(w.id)" in handler
    assert handler.index("_releaseDomCoveredNative(w.id)") < handler.index("_focusNativeDecorated(w.id)")
