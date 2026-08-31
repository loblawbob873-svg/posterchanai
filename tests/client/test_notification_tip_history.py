"""Rare tip kinds paginate independently of the mixed notification window."""
from pathlib import Path


APP = (Path(__file__).resolve().parents[2] / "static/js/client/app.js").read_text()


def _notifications():
    return APP[APP.index("let _notifShown = 25"):APP.index("function notifGrouped(")]


def test_zap_tab_can_load_history_even_when_no_zaps_are_loaded():
    body = _notifications()
    assert "_notifFilter==='zaps' && !_notifZapDone" in body
    assert "Load older tips and zaps" in body
    assert "_notifFilter==='zaps' && _notifShown >= all.length-5" in body


def test_tip_history_queries_lightning_and_address_tip_records():
    body = _notifications()
    assert "kinds:[9735]" in body
    assert "'#t':['monerotip','bchtip']" in body
    assert "kinds:[1]" in body
    assert "_notifZapUntil=floor" in body


def test_empty_page_not_duplicate_page_marks_history_complete():
    body = _notifications()
    assert "if(!(older&&older.length)) _notifZapDone=true" in body
    assert "if(added===0)" not in body
