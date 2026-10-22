"""Remote test shards (scripts/deploy_regression_gate.py): idle cores elsewhere may share the full suite, and
every doubt resolves to running here -- the remote can save time, never change a verdict."""
import runpy
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
G = runpy.run_path(str(ROOT / "scripts/deploy_regression_gate.py"))


def _no_ssh(monkeypatch, answers):
    calls = []

    def fake(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0 if answers else 255, b"", b"")
    monkeypatch.setattr(G["subprocess"], "run", fake)
    return calls


def test_nothing_configured_means_everything_runs_here(monkeypatch, tmp_path):
    monkeypatch.delenv("PC_GATE_REMOTE", raising=False)
    G["remote_target"].__globals__["GATE_REMOTE_FILE"] = tmp_path / "absent"
    calls = _no_ssh(monkeypatch, True)
    assert G["remote_target"]() is None and calls == [], "no config must not even try a host"


def test_off_means_off(monkeypatch):
    monkeypatch.setenv("PC_GATE_REMOTE", "off")
    assert G["remote_target"]() is None


def test_an_unreachable_remote_means_everything_runs_here(monkeypatch):
    monkeypatch.setenv("PC_GATE_REMOTE", "pctest@nowhere.invalid 6")
    _no_ssh(monkeypatch, False)
    assert G["remote_target"]() is None


def test_a_reachable_remote_and_its_job_count(monkeypatch):
    monkeypatch.setenv("PC_GATE_REMOTE", "pctest@nas.lan 5")
    _no_ssh(monkeypatch, True)
    assert G["remote_target"]() == ("pctest@nas.lan", 5)
    monkeypatch.setenv("PC_GATE_REMOTE", "pctest@nas.lan")
    assert G["remote_target"]() == ("pctest@nas.lan", 6)


def test_tests_needing_torch_or_local_checkouts_stay_here(tmp_path):
    (tmp_path / "a.py").write_text("import os\nimport torch\n")
    (tmp_path / "b.py").write_text("from searx import search\n")
    (tmp_path / "c.py").write_text("x = 'import torch'  # prose\nimport json\n")
    assert G["local_only"](tmp_path, "a.py") and G["local_only"](tmp_path, "b.py")
    assert not G["local_only"](tmp_path, "c.py")
    (tmp_path / "d.py").write_text('G = runpy.run_path("scripts/deploy_regression_gate.py")\n')
    assert G["local_only"](tmp_path, "d.py"), "the gate's own tests read this checkout's git state"


def test_the_remote_verdict_is_never_final():
    """Failures re-run here (the retry-alone pass), skips run here, and a shard that did not finish runs here."""
    src = (ROOT / "scripts/deploy_regression_gate.py").read_text()
    body = src[src.index("def _run_shards("):src.index("def run_gate(")]
    assert "again_skipped" in body and "redo.extend(shards[index])" in body
    assert "failed > REMOTE_MAX_FAILURES" in body, "a broken remote environment would be retried one test at a time"
    assert body.index("if remote:") < body.index("retry = [b for b in bad")
    assert "PC_GATE_MANAGED_PROCESSES') != '1' else None" in src, "a gate inside a shard must not fan out again"
