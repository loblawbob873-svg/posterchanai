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
    assert "['restore','--staged','--worktree','--'].concat(restorePaths)" in src
    assert "renameSources.set(dest,_gitPath(records[++i]))" in src


@pytest.mark.skipif(not NODE, reason="node is unavailable")
def test_native_rename_row_uses_the_new_actionable_path_once(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "old.js").write_text("const renamed = true;\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "old.js"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    subprocess.run(["git", "-C", str(repo), "mv", "old.js", "new.js"], check=True)
    script = """
      const H=require(%s);
      H.gitStatus(%s).then(x=>process.stdout.write(JSON.stringify(x)))
        .catch(e=>{console.error(e&&e.stack||e);process.exit(1)});
    """ % (json.dumps(str(HOSTFS)), json.dumps(str(repo)))
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["files"] == [{"xy": "R ", "path": "new.js"}]


def _repo(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "kept.txt").write_text("kept\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "kept.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    return repo


def _discard(repo, target):
    script = """
      const H=require(%s);
      H.gitAction(%s,'restore',[%s],'')
        .then(r=>process.stdout.write(JSON.stringify({ok:true,r})))
        .catch(e=>process.stdout.write(JSON.stringify({ok:false,error:String(e&&e.message||e)})));
    """ % (json.dumps(str(HOSTFS)), json.dumps(str(repo)), json.dumps(target))
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(not NODE, reason="node is unavailable")
def test_discarding_an_untracked_file_removes_exactly_that_file(tmp_path):
    """It is a DELETE, not a restore — there is no stored copy to put back.

    Which is why the client's dialog says so, and why this half must take exactly the one path it
    was handed: everything else in the folder is somebody else's work.
    """
    repo = _repo(tmp_path)
    (repo / "scratch.txt").write_text("written and never committed\n", encoding="utf-8")
    assert _discard(repo, "scratch.txt")["ok"] is True
    assert not (repo / "scratch.txt").exists()
    assert (repo / "kept.txt").exists()


@pytest.mark.skipif(not NODE, reason="node is unavailable")
def test_the_native_bridge_refuses_a_directory_exactly_as_the_node_route_does(tmp_path):
    """`rmSync(..., {recursive:true})` would take a whole tree for one confirmed row.

    Porcelain with `--untracked-files=all` lists files, so a directory can only arrive here from a
    caller that has gone wrong — which is precisely the case a destructive branch must survive.
    The node route already answers 409 "Refusing to discard a directory"; two halves of one feature
    must not answer the same click differently.
    """
    repo = _repo(tmp_path)
    (repo / "notes").mkdir()
    (repo / "notes" / "a.txt").write_text("keep me\n", encoding="utf-8")
    got = _discard(repo, "notes")
    assert got["ok"] is False, got
    assert "directory" in got["error"].lower(), got
    assert (repo / "notes" / "a.txt").exists(), "a whole directory tree was deleted for one row"

    server = (ROOT / "app/routers/code.py").read_text(encoding="utf-8")
    assert "Refusing to discard a directory" in server
