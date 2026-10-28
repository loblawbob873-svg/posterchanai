"""The access log does not carry the Concord tunnel's member ticket (it travels in the websocket URL)."""
import logging

from app.services import access_log


def _line(path):
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("192.168.0.1:4536", "GET", path, "1.1", 101), None)
    access_log.RedactSecrets().filter(rec)
    return rec.getMessage()


def test_the_ticket_is_blanked_and_the_target_kept():
    line = _line("/ws/concord-relay?u=wss%3A%2F%2Fjskitty.com%2Fnostr&t=6bb8672448288977.1793217331.eb707c25")
    assert "6bb8672448288977" not in line and "eb707c25" not in line, line
    assert "u=wss%3A%2F%2Fjskitty.com%2Fnostr&t=…" in line, line


def test_a_path_without_secrets_is_untouched():
    assert "/client/nip05?domain=jskitty.com&name=jskitty" in _line("/client/nip05?domain=jskitty.com&name=jskitty")
    assert "/api/thing?at=5&st=2" in _line("/api/thing?at=5&st=2")


def test_the_app_installs_it():
    import app.main  # noqa: F401
    assert any(isinstance(f, access_log.RedactSecrets) for f in logging.getLogger("uvicorn.access").filters)
