"""A profile opened from a Social window must have an obvious, working route home."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


APP = (Path(__file__).resolve().parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = APP.index(f"  function {name}(")
    brace = APP.index("{", start)
    depth = 0
    for pos in range(brace, len(APP)):
        if APP[pos] == "{": depth += 1
        elif APP[pos] == "}":
            depth -= 1
            if depth == 0: return APP[start:pos + 1]
    raise AssertionError(name)


def _run(*, pushed: int, os_doc: bool = False) -> dict:
    script = f"""
      let click=null, went=0, switched='', closed=[];
      const button={{set onclick(fn){{click=fn;}}}};
      const $=()=>button, history={{back:()=>went++}};
      const window={{PCOS:{{}}}}, PCOS={{isOn:()=>{str(os_doc).lower()},closeDoc:id=>{{closed.push(id);return true;}}}};
      let _navPushed={pushed};
      const switchView=v=>switched=v, _startTimeline=()=> 'global';
      {_function('_bindProfileBack')}
      _bindProfileBack({{}},'alice'); click();
      process.stdout.write(JSON.stringify({{went,switched,closed}}));
    """
    done=subprocess.run(["node","-e",script],capture_output=True,text=True,timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_profile_back_restores_the_social_window_history():
    assert _run(pushed=1) == {"went": 1, "switched": "", "closed": []}


def test_profile_back_closes_a_shell_profile_document_over_social():
    assert _run(pushed=1, os_doc=True) == {"went": 0, "switched": "", "closed": ["prof:alice"]}


def test_cold_profile_back_stays_in_app_and_returns_to_configured_timeline():
    assert _run(pushed=0) == {"went": 0, "switched": "global", "closed": []}


def test_profile_back_is_present_during_cold_loading_and_after_paint():
    profile=APP[APP.index("async function renderProfileView(pk){"):APP.index("function editProfile", APP.index("async function renderProfileView(pk){"))]
    assert "_PROFILE_TOP+'<div class=\"spinner\"></div>'" in profile
    assert "feed.innerHTML=_PROFILE_TOP+`<div class=\"prof\">" in profile
    assert profile.count("_bindProfileBack(feed,pk)") >= 2


def test_avatar_navigation_and_back_are_bound_once_in_the_same_client():
    delegate=APP[APP.index("function bindFeedActions(){"):APP.index("function bindDmMediaActions", APP.index("function bindFeedActions(){"))]
    assert "const av=e.target.closest('.av')" in delegate
    assert "renderProfileView(n.dataset.pk)" in delegate
    assert "_bindProfileBack(feed,pk)" in APP
