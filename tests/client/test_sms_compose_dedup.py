from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SMS = (ROOT / "static/js/client/sms.js").read_text(encoding="utf-8")


def _composer():
    start = SMS.index("let sending = false;")
    end = SMS.index("/* `.bubble[data-doc]`", start)
    return SMS[start:end]


def test_texts_composer_coalesces_repeated_send_actions():
    """A second Enter/click while the first carrier promise is pending must do nothing."""
    body = _composer()
    assert "if(sending) return;" in body
    assert body.index("sending = true;") < body.index("await send(")
    assert "finally{" in body
    assert body.rindex("sending = false;") > body.index("await send(")


def test_enter_does_not_submit_or_start_a_parallel_send():
    body = _composer()
    assert "if(e.key === 'Enter'){ e.preventDefault(); go(); }" in body


def test_a_failed_send_releases_the_guard_for_an_explicit_retry():
    body = _composer()
    finally_body = body.split("finally{", 1)[1]
    assert "sending = false;" in finally_body
    assert "btn.disabled = false;" in finally_body
