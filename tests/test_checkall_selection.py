"""Mistyped selectors must fail before a release gate can print a false green."""
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / 'scripts/checkall.py'


@pytest.mark.parametrize('selector', ['', ' , ', 'no_such_check_928471',
                                      'tests,no_such_check_928471'])
def test_every_explicit_selector_must_match(selector):
    result = subprocess.run([sys.executable, str(RUNNER), '--only', selector, '--list'],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2, result.stdout + result.stderr
    assert '--only' in result.stderr


def test_multiple_valid_selectors_are_still_supported():
    result = subprocess.run([sys.executable, str(RUNNER), '--only',
                             ' tests/client , gentoo_overlay ', '--list'],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'tests/client' in result.stdout
    assert 'check_gentoo_overlay' in result.stdout


def test_invalid_parallelism_fails_before_starting_suites():
    result = subprocess.run([sys.executable, str(RUNNER), '--jobs', '-1', '--list'],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert '--jobs' in result.stderr
