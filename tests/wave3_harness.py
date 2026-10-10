"""A real relay for tests written against SQL accounts (#161 wave 3).

Accounts (`user_table`), Exodus wallet backups and instance applications are relay DocTables gated by a migration
marker. A test that seeds SQL rows and drives a route keeps working BEFORE the marker -- SQL answers -- but even
then the gate reads the marker from the relay and every write lands on the relay first, so such a test needs a
relay to talk to. This gives it the shipped RelayServer (tests/test_doc_table.py's `_Relay`) and an isolated
document namespace, and leaves every table in SQL mode:

    from tests.wave3_harness import pre_marker_relay, shared_relay  # noqa: F401  (fixtures)

    def test_x(pre_marker_relay, ...):
"""
import pytest

from tests.wave2_harness import _isolate, _reset, shared_relay  # noqa: F401


def use_relay(monkeypatch, port, factory=None) -> None:
    """Point this test at the relay on `port`, nothing migrated. `factory` replaces app.database.SessionLocal when
    given (the test's own database); otherwise the test's sessions are used as they are."""
    import app.database
    _isolate(monkeypatch, port, factory or app.database.SessionLocal, migrated=False)


@pytest.fixture
def pre_marker_relay(shared_relay, monkeypatch):     # noqa: F811
    try:
        yield shared_relay
    finally:
        _reset()
