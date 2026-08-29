"""Android foregrounding must reconcile Social even before its delayed battery pause fires."""
from pathlib import Path


APPJS = (Path(__file__).resolve().parents[1] / "static/js/client/app.js").read_text(encoding="utf-8")


def _resume_body():
    start = APPJS.index("_tlResume = ()=>{")
    return APPJS[start:APPJS.index("// Phase 2 (NIP-77)", start)]


def test_short_android_background_still_fetches_missed_posts():
    body = _resume_body()
    assert "const wasPaused=_tlPaused" in body
    assert "if(!_tlPaused) return" not in body, (
        "foreground reconciliation is still skipped until the 20-second pause timer fires"
    )
    assert "_hiddenAt" in body and "Relay.query(catchFilters)" in body


def test_foreground_catchup_waits_for_the_relay_and_preserves_scroll():
    body = _resume_body()
    assert "Relay.ready(8000).then(()=>Relay.query(catchFilters))" in body
    assert "_drawTimeline(true)" in body


def test_duplicate_native_foreground_signals_do_not_duplicate_queries():
    body = _resume_body()
    assert "_resumeCatchAt" in body and "< 4000" in body


def test_a_real_pause_still_reopens_live_delivery():
    body = _resume_body()
    assert "if(wasPaused && !subs[view]) fullSub()" in body


def test_visible_offline_recovery_runs_the_scroll_preserving_catchup():
    """An online event can happen without background/foreground when Wi-Fi drops in place."""
    assert "window.addEventListener('online', ()=>{ _tlForeground(); _resumeRelay(); });" in APPJS
    assert "if(e && e.persisted){ _tlForeground(); _resumeRelay(); }" in APPJS
    body = _resume_body()
    assert "_drawTimeline(true)" in body
