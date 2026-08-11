"""Notes, Calendar and Contacts: what they cost a phone that is not looking at them.

Run: venv-unified/bin/python -m pytest tests/test_private_sync_battery.py

Asked for as "notes, calendar, contacts should all be battery efficient syncing". Measured rather
than assumed, and two of the three were already free — Calendar and Contacts hold no timer and no
subscription at all; they load when their screen is rendered and do nothing otherwise.

Notes (and the password vault, which is the same code shape) had two real costs:

  1. A LIVE RELAY SUBSCRIPTION THAT WAS NEVER CLOSED. Both open one so a note written on another
     device appears without a reload, and both exposed an `unmount` to close it — which nothing ever
     called, because renderView replaces #feed and tells no view it is gone. One visit to Notes left
     that subscription open for the rest of the session, repainting a screen nobody was looking at.
     Exactly the cost the timeline's pause exists to avoid.
  2. A 45-SECOND TIMER THAT PARSED localStorage. For the life of the page, on every device, to
     discover an empty array — and the real drain is now the relay reaching 'ok', which fires on a
     cold start, a reconnect and a resume.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
C = {n: (ROOT / "static" / "js" / "client" / f"{n}.js").read_text(encoding="utf-8")
     for n in ("notes", "vault", "calendar", "contacts", "app")}


def test_calendar_and_contacts_cost_nothing_when_closed():
    """They hold no timer and no live subscription: they load on render and stop."""
    for n in ("calendar", "contacts"):
        assert "setInterval" not in C[n], f"{n}.js has grown a timer"
        assert "Relay().subscribe" not in C[n] and "Relay.subscribe" not in C[n], (
            f"{n}.js has grown a live subscription with no lifecycle to close it")


def test_leaving_notes_or_the_vault_closes_the_subscription():
    for n in ("notes", "vault"):
        assert "sleep(){" in C[n], f"{n}.js exposes no way to drop its subscription"
        i = C[n].index("sleep(){")
        assert "unwatch()" in C[n][i:i + 120]
    i = C["app"].index("PCNotes.sleep")
    body = C["app"][max(0, i - 900):i + 300]
    assert "VIEW!=='notes'" in body and "VIEW!=='vault'" in body, "nothing calls them"


def test_sleeping_is_not_unmounting():
    """`unmount` clears the selection too — losing your place because you glanced at the timeline
    would be a worse bug than the one being fixed."""
    for n in ("notes", "vault"):
        i = C[n].index("sleep(){")
        assert "_sel" not in C[n][i:i + 120], f"{n}'s sleep throws away the selected item"


def test_the_desktop_is_exempt():
    """A view there lives in its own WINDOW and is still on screen when another is focused; os.js
    owns that lifetime. The same exemption the terminal has."""
    i = C["app"].index("PCNotes.sleep")
    assert "PCOS && PCOS.isOn()" in C["app"][max(0, i - 700):i]


def test_the_backstop_timer_costs_nothing_when_there_is_nothing_queued():
    """It read localStorage and JSON.parsed the queue every 45 seconds, for the life of the page, to
    discover an empty array."""
    for n in ("notes", "vault"):
        assert "_pend > 0 && navigator.onLine" in C[n], f"{n} still parses storage on a timer"
        assert "}, 300000);" in C[n], f"{n}'s backstop still runs every 45 seconds"
        # …and the count has to be MAINTAINED, or the guard is a permanent off switch.
        assert "_pend = (list && list.length) || 0;" in C[n], f"{n} never updates the count"
        assert "_pend = l.length" in C[n]
        assert "try{ pending(); }catch(_){}" in C[n], (
            f"{n} never seeds the count, so `_pend > 0` is false for ever and the backstop is dead")


def test_the_real_drain_is_the_relay_coming_back():
    """Which fires on a cold start, a reconnect and a resume — the three ways a phone comes back."""
    assert "_flushPrivateQueues()" in C["app"]
    i = C["app"].index("if(s === 'ok'){")
    assert "_flushPrivateQueues()" in C["app"][i:i + 200]
