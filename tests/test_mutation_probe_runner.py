"""Mutation diagnostics must survive outside the checkout and retain pytest's exit status."""
import pytest

from tests.test_the_suite_can_actually_fail import _run


@pytest.mark.parametrize("source, code, diagnostic", [
    ("def test_control():\n    assert True\n", 0, "1 passed"),
    ("def test_regression():\n    assert False, 'guard removed'\n", 1, "guard removed"),
    ("raise RuntimeError('collection broke')\n", 2, "collection broke"),
    ("# No tests: not evidence that a mutation was caught.\n", 5, "no tests ran"),
])
def test_probe_retains_status_and_diagnostics_without_a_local_venv(tmp_path, source, code, diagnostic):
    (tmp_path / "test_probe.py").write_text(source)
    result = _run("test_probe.py", tmp_path)
    assert result.returncode == code
    assert diagnostic in result.stdout + result.stderr


def test_missing_target_reports_usage_error(tmp_path):
    result = _run("test_missing.py", tmp_path)
    assert result.returncode == 4
    assert "test_missing.py" in result.stderr
