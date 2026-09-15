"""A broken pytest import must block deployment, not masquerade as an unavailable browser."""
from argparse import Namespace
import json
import sys

import pytest

from scripts import checkall


@pytest.mark.parametrize("group", ["unit", "client"])
def test_real_collection_error_fails_release_report(tmp_path, monkeypatch, capsys, group):
    (tmp_path / "test_broken.py").write_text("import posterchan_missing_collection_dependency_92741\n")
    monkeypatch.setattr(checkall, "ROOT", tmp_path)
    monkeypatch.setattr(checkall, "PY", sys.executable)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    suite = dict(name="broken", group=group, secs=15,
                 argv=["-m", "pytest", "test_broken.py", "-q", "-p", "no:cacheprovider"])
    report = tmp_path / "report.json"
    args = Namespace(jobs=1, live=None, strict=False, brief=True, json=str(report))
    result = checkall._execute(args, [suite], [], tmp_path / "logs", None, lambda *a: None)
    rows = json.loads(report.read_text())
    assert rows[0]["code"] == 2, rows
    assert result == 1, "a pytest collection error allowed the release gate to pass"
    assert rows[0]["verdict"] == "FAIL", rows
    assert "result: FAIL" in capsys.readouterr().out


def test_unavailable_standalone_browser_check_remains_a_skip():
    assert checkall.verdict(dict(code=2, group="ui", out="SKIP Chrome unavailable")) == (
        "SKIP", "SKIP Chrome unavailable")
