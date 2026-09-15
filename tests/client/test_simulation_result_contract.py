"""A newly added simulation scenario must fail pytest even without a named wrapper test."""
import importlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def collect(module, wrapper):
    if wrapper == "test_sync_store_scale":
        with patch.object(module.TestSyncStoreScale, "_rows", None):
            module.TestSyncStoreScale.setUpClass()
            return module.TestSyncStoreScale._rows
    return module.results.__wrapped__()


@pytest.mark.parametrize("wrapper", ["test_sync_store_scale", "test_two_browser_sync"])
@pytest.mark.parametrize("problem", ["extra_failed_row", "missing_ok", "duplicate_name", "empty", "process_failure"])
def test_simulation_wrappers_reject_incomplete_or_failed_results(wrapper, problem):
    module = importlib.import_module("tests.client." + wrapper)
    # Negative controls report success when the expected refusal occurs. Their detail may be false.
    rows = [{"name": "expected refusal control", "ok": True, "detail": {"accepted": False}}]
    returncode = 0
    if problem == "extra_failed_row":
        rows.append({"name": "brand new scenario with no Python method", "ok": False, "detail": "sentinel failure"})
    elif problem == "missing_ok":
        rows.append({"name": "malformed scenario"})
    elif problem == "duplicate_name":
        rows += [{"name": "duplicate", "ok": True}, {"name": "duplicate", "ok": True}]
    elif problem == "empty":
        rows = []
    else:
        returncode = 1
    result = SimpleNamespace(stdout=json.dumps(rows), stderr="simulation diagnostics", returncode=returncode)
    with patch.object(module.subprocess, "run", return_value=result):
        with pytest.raises(AssertionError):
            collect(module, wrapper)


@pytest.mark.parametrize("wrapper", ["test_sync_store_scale", "test_two_browser_sync"])
def test_expected_negative_control_remains_a_success(wrapper):
    module = importlib.import_module("tests.client." + wrapper)
    row = {"name": "expected refusal control", "ok": True, "detail": {"accepted": False}}
    result = SimpleNamespace(stdout=json.dumps([row]), stderr="", returncode=0)
    with patch.object(module.subprocess, "run", return_value=result):
        assert collect(module, wrapper)[row["name"]] == row
