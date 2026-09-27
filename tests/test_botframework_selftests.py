"""The bot framework's own tests, run by the deploy gate.

Run: venv-unified/bin/python -m pytest tests/test_botframework_selftests.py

botframework/test_holdem.py, test_blackjack.py and test_pleroma_parity.py live beside the bots, and
the gate (scripts/deploy_regression_gate.py) and ./test.sh only discover tests/**/test_*.py -- so
none of them ran on any deploy. They could have been failing for months with nothing to say so.
This collects their test functions (by name, so a new one joins automatically) and runs the parity
script, which is a __main__ program, as the script it is.
"""
import importlib
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOTS = os.path.join(ROOT, "botframework")
MODULES = ("test_holdem", "test_blackjack")


def _cases():
    sys.path.insert(0, BOTS)
    try:
        out = []
        for name in MODULES:
            mod = importlib.import_module(name)
            out += [pytest.param(mod, fn, id=f"{name}::{fn}") for fn in sorted(dir(mod))
                    if fn.startswith("test_") and callable(getattr(mod, fn))]
        return out
    finally:
        sys.path.remove(BOTS)


@pytest.mark.parametrize("mod,fn", _cases())
def test_botframework_case(mod, fn, monkeypatch):
    monkeypatch.syspath_prepend(BOTS)
    getattr(mod, fn)()


def test_every_module_contributes_cases():
    """A module whose tests were all renamed away would otherwise pass here by running nothing."""
    names = {p.id.split("::")[0] for p in _cases()}
    assert names == set(MODULES), names


def test_pleroma_parity_script():
    r = subprocess.run([sys.executable, os.path.join(BOTS, "test_pleroma_parity.py")], cwd=BOTS,
                       capture_output=True, text=True, timeout=300,
                       env=dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", "")))
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
