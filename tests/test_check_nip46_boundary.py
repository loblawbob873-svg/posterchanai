from pathlib import Path

import pytest

from scripts.check_nip46_signer import CDPError, _cdp_result


SRC=(Path(__file__).resolve().parents[1]/'scripts/check_nip46_signer.py').read_text()


def test_oversize_scenario_requires_local_actionable_rejection_and_no_signer_call():
    assert "stable = 0" in SRC
    assert "await asyncio.sleep(0.1)" in SRC
    assert "seen_before = len(bunker.seen)" in SRC
    assert '"65535" not in err' in SRC
    assert '"attachment" not in err.lower()' in SRC
    assert "len(bunker.seen) != seen_before" in SRC
    assert "a 100KB NIP-44 plaintext was accepted" in SRC


def test_cdp_errors_keep_the_protocol_cause_instead_of_becoming_none():
    with pytest.raises(CDPError, match=r"Runtime.evaluate failed \(-32000\).*context destroyed"):
        _cdp_result({"id": 7, "error": {"code": -32000,
                    "message": "Execution context destroyed"}}, "Runtime.evaluate")


def test_cdp_success_returns_even_an_empty_result_object():
    assert _cdp_result({"id": 8, "result": {}}, "Runtime.evaluate") == {}


def test_each_signer_case_gets_a_new_document_and_waits_for_the_fake_signer():
    assert "await bunker.wait_ready()" in SRC
    assert 'await call("Page.navigate", {"url": "about:blank"})' in SRC
    assert 'await call("Network.clearBrowserCookies")' in SRC
    assert "fake signer could not subscribe" in SRC
    assert "typeof document.querySelector('#btn-amber')?.onclick==='function'" in SRC
    assert "typeof document.querySelector('#btn-amber-connect')?.onclick==='function'" in SRC


def test_slow_fake_signer_deduplicates_identical_resends_like_a_real_signer():
    assert "self._event_ids = set()" in SRC
    assert 'if ev.get("id") in self._event_ids' in SRC
    assert 'self._event_ids.add(ev.get("id"))' in SRC
    assert "and not self._approval_delayed" in SRC
    assert "self._approval_delayed = True" in SRC
