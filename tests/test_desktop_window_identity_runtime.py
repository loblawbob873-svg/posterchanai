"""Execute the shipped monitor-handoff identity policy, not a duplicate implementation."""

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
HTML = (ROOT / "templates/client.html").read_text(encoding="utf-8")


def _function(name: str, next_name: str) -> str:
    start = OS_JS.index(f"function {name}(")
    end = OS_JS.index(f"function {next_name}(", start)
    return OS_JS[start:end].strip()


def _node(script: str):
    run = subprocess.run(
        ["node", "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_shipped_classifier_covers_every_registered_app_at_runtime():
    views = sorted(set(re.findall(r'data-view=["\']([^"\']+)', HTML)))
    classify = _function("handoffDocumentKind", "reconstructHandoffWindow")
    result = _node(
        classify
        + "\nconst views="
        + json.dumps(views)
        + "; console.log(JSON.stringify(views.map(view=>[view,handoffDocumentKind(view)])));"
    )
    assert len(result) > 20
    assert result == [[view, "app"] for view in views]


def test_profile_and_post_runtime_never_enter_generic_open_app():
    classify = _function("handoffDocumentKind", "reconstructHandoffWindow")
    rebuild = _function("reconstructHandoffWindow", "selectedMessagesTab")
    pk = "ab" * 32
    event_id = "cd" * 32
    result = _node(
        f"""
        {classify}
        {rebuild}
        const calls=[];
        const wins=[];
        function PC(){{return {{
          openProfile(pk){{calls.push(['profile',pk]);wins.push({{view:'doc:prof:'+pk}});}},
          openThread(id){{calls.push(['post',id]);wins.push({{view:'doc:post:'+id}});}}
        }};}}
        function openApp(view){{calls.push(['openApp',view]);return {{view}};}}
        const profile=reconstructHandoffWindow({{view:'doc:prof:{pk}'}});
        const post=reconstructHandoffWindow({{view:'doc:post:{event_id}'}});
        const ordinary=reconstructHandoffWindow({{view:'calendar',title:'Calendar',icon:'i-calendar'}});
        console.log(JSON.stringify({{calls,profile:profile.view,post:post.view,ordinary:ordinary.view}}));
        """
    )
    assert result["profile"] == f"doc:prof:{pk}"
    assert result["post"] == f"doc:post:{event_id}"
    assert result["ordinary"] == "calendar"
    assert ["openApp", f"doc:prof:{pk}"] not in result["calls"]
    assert ["openApp", f"doc:post:{event_id}"] not in result["calls"]
    assert result["calls"].count(["openApp", "calendar"]) == 1


def test_unknown_document_runtime_fails_closed():
    classify = _function("handoffDocumentKind", "reconstructHandoffWindow")
    rebuild = _function("reconstructHandoffWindow", "selectedMessagesTab")
    result = _node(
        f"""
        {classify}
        {rebuild}
        const calls=[],wins=[];
        function PC(){{return {{}};}}
        function openApp(view){{calls.push(view);return {{view}};}}
        const value=reconstructHandoffWindow({{view:'doc:private-module:123'}});
        console.log(JSON.stringify({{value,calls}}));
        """
    )
    assert result == {"value": None, "calls": []}
