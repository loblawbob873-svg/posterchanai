import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOSTFS = ROOT / "desktop" / "hostfs.js"
NODE = shutil.which("node") or shutil.which("nodejs")


@pytest.mark.skipif(not NODE, reason="node is unavailable")
def test_native_discard_restores_staged_and_worktree_changes(tmp_path):
    """Drive the exported desktop bridge against Git, not a source-string mock."""
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    changed = repo / "changed.js"
    changed.write_text("const staged = false;\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "changed.js"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    changed.write_text("const staged = true;\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "changed.js"], check=True)

    script = """
      const H=require(%s);
      (async()=>{
        const before=await H.gitStatus(%s);
        const diff=await H.gitDiff(%s,'changed.js');
        await H.gitAction(%s,'restore',['changed.js'],'');
        const after=await H.gitStatus(%s);
        process.stdout.write(JSON.stringify({before,diff:diff.diff,after}));
      })().catch(e=>{console.error(e&&e.stack||e);process.exit(1)});
    """ % tuple(json.dumps(str(x)) for x in (HOSTFS, repo, repo, repo, repo))
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    assert got["before"]["files"] == [{"xy": "M ", "path": "changed.js"}]
    assert "+const staged = true;" in got["diff"]
    assert got["after"]["files"] == []
    assert changed.read_text(encoding="utf-8") == "const staged = false;\n"


def test_native_and_server_discard_share_index_and_worktree_semantics():
    src = HOSTFS.read_text(encoding="utf-8")
    assert "['restore','--staged','--worktree','--',p]" in src
