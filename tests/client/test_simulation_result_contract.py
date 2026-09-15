"""A newly added simulation scenario must fail pytest even without a named wrapper test."""
import importlib
import html
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def collect(module, wrapper):
    classes = {"test_sync_store_scale": "TestSyncStoreScale", "test_sync_tick": "TestSyncTick"}
    if wrapper in classes:
        cls = getattr(module, classes[wrapper])
        with patch.object(cls, "_rows", None):
            cls.setUpClass()
            return cls._rows
    if wrapper == "test_dm_draft_survives":
        with patch.object(module, "_sources", return_value=""):
            return module.results.__wrapped__()
    return module.results.__wrapped__()


def output(wrapper, rows):
    text = json.dumps(rows)
    return '<pre id="out">' + html.escape(text) + '</pre>' if wrapper == "test_dm_draft_survives" else text


@pytest.mark.parametrize("wrapper", ["test_sync_store_scale", "test_two_browser_sync", "test_sync_tick", "test_dm_draft_survives"])
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
    result = SimpleNamespace(stdout=output(wrapper, rows), stderr="simulation diagnostics", returncode=returncode)
    with patch.object(module.subprocess, "run", return_value=result):
        with pytest.raises(AssertionError):
            collect(module, wrapper)


@pytest.mark.parametrize("wrapper", ["test_sync_store_scale", "test_two_browser_sync", "test_sync_tick", "test_dm_draft_survives"])
def test_expected_negative_control_remains_a_success(wrapper):
    module = importlib.import_module("tests.client." + wrapper)
    row = {"name": "expected refusal control", "ok": True, "detail": {"accepted": False}}
    result = SimpleNamespace(stdout=output(wrapper, [row]), stderr="", returncode=0)
    with patch.object(module.subprocess, "run", return_value=result):
        assert collect(module, wrapper)[row["name"]] == row
