"""A failed source commit or production push must stop every later deployment action."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_deploy(tmp_path, failure):
    # Isolate fixed log files; execute all deployment control flow unchanged.
    source = (ROOT / "sync.sh").read_text().replace("/tmp/", str(tmp_path / "tmp") + "/")
    (tmp_path / "sync.sh").write_text(source)
    (tmp_path / "tmp").mkdir()
    (tmp_path / ".git").mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "calls.jsonl"
    stub = "#!" + sys.executable + "\n" + r'''
import json,os,sys
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
with open(os.environ['CALL_LOG'],'a') as output:
    output.write(json.dumps([name,*args])+'\n')
failure=os.environ['FAILURE']
if name=='git':
    if args[0]=='status':
        if failure=='status':sys.exit(128)
        if failure!='clean':print(' M tracked.py')
    elif args[0]=='commit':
        if failure in ('commit','clean'):sys.exit(1)
    elif args[:3]==['push','origin','master'] and failure=='origin':sys.exit(128)
    elif args[0]=='rev-parse':print('a'*40)
elif name=='ssh':
    if 'git rev-parse HEAD' in args[-1]:print('a'*40)
elif name=='systemctl':
    if args[0]=='is-enabled':sys.exit(1)
'''
    for tool in ["git", "ssh", "systemctl", "sudo", "sleep", "flock"]:
        path = tools / tool
        path.write_text(stub)
        path.chmod(0o755)
    for relative in ["venv-unified/bin/python", "scripts/refresh_apk.sh", "scripts/publish_overlay.sh"]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stub)
        path.chmod(0o755)
    result = subprocess.run(["bash", "sync.sh"], cwd=tmp_path, text=True, capture_output=True,
                            timeout=10, env={**os.environ, "PATH":str(tools)+os.pathsep+os.environ['PATH'],
                                             "SKIP_LINT":"1", "CALL_LOG":str(log), "FAILURE":failure})
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls


@pytest.mark.parametrize("failure", ["status", "commit", "origin"])
def test_failed_commit_or_production_push_cannot_publish_or_pull(tmp_path, failure):
    result, calls = run_deploy(tmp_path, failure)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not any(call[:3] == ["git", "push", "github"] for call in calls), calls
    assert not any(call[0] in ("ssh", "sudo", "refresh_apk.sh", "publish_overlay.sh") for call in calls), calls
    assert not (tmp_path / ".git/posterchanai-deployed").exists()
    if failure in ("status", "commit"):
        assert not any(call[:2] == ["git", "push"] for call in calls), calls
    else:
        assert ["git", "push", "origin", "master"] in calls
    assert "ABORT" in result.stdout


@pytest.mark.parametrize("state", ["clean", "changed"])
def test_clean_or_successfully_committed_tree_can_deploy(tmp_path, state):
    result, calls = run_deploy(tmp_path, state)
    assert result.returncode == 0, result.stdout + result.stderr
    production = calls.index(["git", "push", "origin", "master"])
    mirror = calls.index(["git", "push", "github", "master:main"])
    assert production < mirror
    assert any(call[0] == "ssh" for call in calls)
    commits = [call for call in calls if call[:2] == ["git", "commit"]]
    assert bool(commits) == (state == "changed"), calls
    assert (tmp_path / ".git/posterchanai-deployed").read_text().strip() == "a" * 40
