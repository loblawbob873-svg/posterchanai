"""The test suite leaves nothing behind in TMPDIR -- which is RAM on the machines it runs on.

Measured on 2026-09-23: 36,662 `.com.google.Chrome.*` files, 38,941 Chrome fetcher directories,
523 `pc-checkall-<pid>` directories and a `pytest-of-<user>` tree, ~8 GB in a tmpfs /tmp, and a
deploy killed for low memory. Headless Chrome, pytest and tempfile all write into TMPDIR, so every
way of running the suite now points TMPDIR at one directory of its own and deletes it at the end
(scripts/private_tmp.py): the deploy gate, ./test.sh (checkall.py) and a plain `pytest`.

The end-to-end check RUNS a real Chrome-driving test under a child pytest whose TMPDIR is an empty
directory, and asserts that directory is empty again afterwards.
"""
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
PT = runpy.run_path(str(ROOT / "scripts" / "private_tmp.py"))
CHROME = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
          or shutil.which("chromium") or "/opt/google/chrome/google-chrome")


def _clean_env(tmp):
    env = {k: v for k, v in os.environ.items() if k not in (PT["ENV_MARK"], "PYTEST_DEBUG_TEMPROOT")}
    env["TMPDIR"] = str(tmp)
    return env


def test_scoped_removes_everything_children_wrote(tmp_path):
    root = tmp_path / "sys-tmp"
    root.mkdir()
    code = (
        "import runpy,subprocess,sys,os\n"
        "pt=runpy.run_path(%r)\n"
        "with pt['scoped']('pct-t-') as d:\n"
        "    subprocess.run([sys.executable,'-c',\"import tempfile;open(tempfile.mkstemp()[1],'w').write('x');"
        "tempfile.mkdtemp()\"],env=pt['child_env'](dict(os.environ),d),check=True)\n"
        "    assert os.listdir(d), 'the child did not write into the private dir'\n"
        % str(ROOT / "scripts" / "private_tmp.py"))
    subprocess.run([sys.executable, "-c", code], env=_clean_env(root), check=True, timeout=60)
    assert os.listdir(root) == [], os.listdir(root)


def test_enter_is_removed_at_exit_and_nested_runs_share_one_dir(tmp_path):
    root = tmp_path / "sys-tmp"
    root.mkdir()
    code = (
        "import runpy,subprocess,sys,os,tempfile\n"
        "pt=runpy.run_path(%r)\n"
        "d=pt['enter']('pct-t-')\n"
        "assert tempfile.gettempdir()==d\n"
        "inner=subprocess.run([sys.executable,'-c',"
        "\"import runpy;print(runpy.run_path(%r)['enter']('pct-u-'))\"],capture_output=True,text=True,check=True)\n"
        "assert inner.stdout.strip()==d, (inner.stdout, d)\n"
        "open(os.path.join(d,'left-behind'),'w').write('x')\n"
        % (str(ROOT / "scripts" / "private_tmp.py"), str(ROOT / "scripts" / "private_tmp.py")))
    subprocess.run([sys.executable, "-c", code], env=_clean_env(root), check=True, timeout=60)
    assert os.listdir(root) == [], os.listdir(root)


def test_every_way_of_running_the_suite_uses_it():
    gate = (ROOT / "scripts" / "deploy_regression_gate.py").read_text(encoding="utf-8")
    assert "private_tmp['scoped']" in gate and "private_tmp['child_env']" in gate
    check = (ROOT / "scripts" / "checkall.py").read_text(encoding="utf-8")
    assert '["enter"]' in check and 'f"/tmp/pc-checkall-{' not in check
    conf = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "def pytest_configure" in conf and '["enter"]' in conf


@pytest.fixture
def short_root():
    """A TMPDIR as short as a real one. Chrome binds a Unix socket under TMPDIR and a socket path
    past 108 bytes cannot be bound, so a root nested inside pytest's own tree would test the path
    length instead of the cleanup."""
    d = tempfile.mkdtemp(prefix="pcx-", dir="/tmp" if os.path.isdir("/tmp") else None)
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _size(p):
    return sum(f.stat().st_size for f in Path(p).rglob("*") if f.is_file() and not f.is_symlink())


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome is not installed")
def test_a_real_chrome_test_run_leaves_only_pytests_own_small_bounded_tree(short_root):
    """Chrome's files and the run's private directory must be GONE. What pytest keeps of its own
    tmp_path tree is bounded by pyproject.toml's retention settings -- one run's skeleton, a few KB,
    replaced (not added to) by the next run."""
    root = short_root
    for run in range(2):
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                            "tests/client/test_notes_open_rendered_runtime.py"],
                           cwd=ROOT, env=_clean_env(root), capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    left = sorted(os.listdir(root))
    assert all(n.startswith("pytest-of-") for n in left), \
        "the test run left files that are not pytest's own tree: %r" % left
    runs = [d for n in left for d in os.listdir(root / n) if d.startswith("pytest-") and d != "pytest-current"]
    assert len(runs) <= 1, "pytest kept a tree per run instead of one: %r" % runs
    assert _size(root) < 1_000_000, "pytest's leftover tree is %d bytes" % _size(root)


def test_a_killed_runs_directory_is_removed_by_the_next_run(tmp_path):
    """A run SIGKILLed mid-test (the memory watchdog, a crash, a timeout) never reaches its own cleanup.
    The next run must remove what it left -- and must not touch a run that is still going."""
    root = tmp_path / "sys-tmp"
    root.mkdir()
    pt = str(ROOT / "scripts" / "private_tmp.py")
    hold = ("import runpy,sys,time\npt=runpy.run_path(%r)\nd=pt['enter']('pct-k-')\n"
            "open(d+'/chrome-junk','wb').write(b'x'*4096)\nprint(d,flush=True)\ntime.sleep(600)\n" % pt)
    killed = subprocess.Popen([sys.executable, "-c", hold], env=_clean_env(root), stdout=subprocess.PIPE, text=True)
    alive = subprocess.Popen([sys.executable, "-c", hold], env=_clean_env(root), stdout=subprocess.PIPE, text=True)
    try:
        dead_dir, live_dir = killed.stdout.readline().strip(), alive.stdout.readline().strip()
        killed.kill(); killed.wait(10)
        assert os.path.isdir(dead_dir), "a SIGKILLed run cannot clean up after itself -- that is the case"
        (root / "pct-unowned-new").mkdir()             # being set up this instant / an older build's run
        subprocess.run([sys.executable, "-c", "import runpy;runpy.run_path(%r)['scoped']('pct-n-').__enter__()" % pt],
                       env=_clean_env(root), check=True, timeout=60)
        assert not os.path.exists(dead_dir), "the killed run's directory was left in RAM"
        assert os.path.isdir(live_dir), "a run that is still going lost its temp directory"
        assert (root / "pct-unowned-new").is_dir()
    finally:
        alive.kill(); alive.wait(10)


def test_a_recycled_pid_does_not_keep_a_dead_runs_directory(tmp_path):
    pt = runpy.run_path(str(ROOT / "scripts" / "private_tmp.py"))
    d = tmp_path / "pct-old"
    d.mkdir()
    (d / ".owner").write_text(f"{os.getpid()} 1")      # our pid, but a start time that is not ours
    assert pt["sweep"](str(tmp_path)) == [str(d)]
    old = tmp_path / "pct-unowned-old"
    old.mkdir()
    os.utime(old, (0, 0))
    assert pt["sweep"](str(tmp_path)) == [str(old)]
