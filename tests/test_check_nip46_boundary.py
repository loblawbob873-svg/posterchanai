from pathlib import Path

import pytest

import asyncio

from scripts.check_nip46_signer import Bunker, CDPError, _cdp_result


SRC=(Path(__file__).resolve().parents[1]/'scripts/check_nip46_signer.py').read_text()


def test_oversize_scenario_requires_local_actionable_rejection_and_no_signer_call():
    assert "await bunker.wait_quiet()" in SRC
    assert "seen_before = len(bunker.seen)" in SRC
    assert '"65535" not in err' in SRC
    assert '"attachment" not in err.lower()' in SRC
    assert "len(bunker.seen) != seen_before" in SRC
    assert "a 100KB NIP-44 plaintext was accepted" in SRC


def test_quiescence_window_restarts_for_late_login_tail_under_load():
    async def exercise():
        bunker = Bunker("ws://unused.invalid")

        async def late_tail():
            for delay in (0.03, 0.07, 0.04, 0.12):
                await asyncio.sleep(delay)
                bunker.seen.append(("sign_event", "nip44"))
                bunker._activity_seq += 1
                bunker._activity.set()

        tail = asyncio.create_task(late_tail())
        started = asyncio.get_running_loop().time()
        assert await bunker.wait_quiet(quiet_for=0.15, timeout=2.0)
        elapsed = asyncio.get_running_loop().time() - started
        await tail
        assert len(bunker.seen) == 4
        assert elapsed >= 0.39, "returned before the last delayed event plus a complete quiet window"

    asyncio.run(exercise())


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
    assert "window.__PC_BOOTED === true" in SRC
    assert "typeof document.querySelector('#btn-amber')?.onclick==='function'" in SRC
    assert "typeof document.querySelector('#btn-amber-connect')?.onclick==='function'" in SRC


def test_login_completion_is_correlated_to_this_bunker_not_late_ui_paint():
    assert "who() === expectedPk" in SRC
    assert "$('#auth-gate').classList.contains('hidden') && who()" not in SRC
    assert "json.dumps(bunker.user_pk)" in SRC


def test_slow_fake_signer_deduplicates_identical_resends_like_a_real_signer():
    assert "self._event_ids = set()" in SRC
    assert 'if ev.get("id") in self._event_ids' in SRC
    assert 'self._event_ids.add(ev.get("id"))' in SRC
    assert "and not self._approval_delayed" in SRC
    assert "self._approval_delayed = True" in SRC
