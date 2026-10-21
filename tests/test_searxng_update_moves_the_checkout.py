"""`./install.sh --searxng` on an existing checkout really moves it to upstream's tip.

It used to run `git pull --ff-only` with the error discarded, which can NEVER succeed here: the clone
is --depth 1 and the branding step edits four tracked images. Both nodes sat on the same 2026-08-12
commit for two months while every run printed "(kept the checkout as-is)" and then "SearXNG is up".
These run the shipped `_searxng_update_checkout` under bash against a real local "upstream" repo,
cloned exactly the way the installer clones it.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "install" / "searxng.sh"
IMG = "searx/static/themes/simple/img"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo, name, text):
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


def _setup(tmp_path):
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q", "-b", "master")
    for f in ("searxng.png", "favicon.png", "192.png", "512.png"):
        (up / IMG).mkdir(parents=True, exist_ok=True)
        (up / IMG / f).write_text("upstream art")
    _commit(up, "searx/webapp.py", "v1")
    src = tmp_path / "searxng"
    # file:// so --depth is honoured, as it is for the real https clone
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{up}", str(src)], check=True)
    # what the branding step does after every install
    for f in ("searxng.png", "favicon.png", "192.png", "512.png"):
        (src / IMG / f).write_text("posterchan logo")
    new = _commit(up, "searx/engines/brave.py", "fixed")
    return up, src, new


def _run(up, src):
    return subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; _searxng_update_checkout "$1"', "_", str(src)],
        env={"PATH": "/usr/bin:/bin", "SEARXNG_REPO": f"file://{up}", "HOME": str(src.parent)},
        capture_output=True, text=True)


def test_a_branded_shallow_checkout_is_updated_to_upstream(tmp_path):
    up, src, new = _setup(tmp_path)
    r = _run(up, src)
    assert r.returncode == 0, r.stderr
    assert _git(src, "rev-parse", "HEAD") == new, r.stdout + r.stderr
    assert (src / "searx/engines/brave.py").read_text() == "fixed"
    assert "updated" in r.stdout


def test_somebody_elses_edit_is_kept_and_reported(tmp_path):
    up, src, new = _setup(tmp_path)
    (src / "searx/webapp.py").write_text("an operator's hotfix")
    old = _git(src, "rev-parse", "HEAD")
    r = _run(up, src)
    assert r.returncode == 0
    assert _git(src, "rev-parse", "HEAD") == old
    assert (src / "searx/webapp.py").read_text() == "an operator's hotfix"
    assert "NOT updated" in r.stdout + r.stderr and "searx/webapp.py" in r.stdout


def test_an_unreachable_upstream_is_said_out_loud(tmp_path):
    up, src, _ = _setup(tmp_path)
    r = _run(tmp_path / "nowhere", src)
    assert r.returncode == 0
    assert "Could not reach" in r.stdout + r.stderr or "could not reach" in r.stdout + r.stderr


def test_the_installer_calls_it():
    s = SCRIPT.read_text()
    assert '_searxng_update_checkout "$src_dir"' in s
    assert 'git -C "$src_dir" pull' not in s
