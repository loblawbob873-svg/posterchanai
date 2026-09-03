"""The taskbar Reconnect control must rebuild a missing/stale relay pool."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")


def test_reconnect_refetches_config_rebuilds_pool_and_waits_for_a_live_socket():
    body = APP.split("async function reconnectNetwork(){", 1)[1].split(
        "/* DRAIN THE PRIVATE QUEUES", 1
    )[0]
    assert "fetch('/client/config'" in body
    assert "cache:'no-store'" in body
    assert "connectRelays()" in body
    assert "Relay.wake()" in body
    assert "await Relay.ready(5000)" in body
    assert "reconnectNetwork," in APP.split("window.__PC = {", 1)[1]


def test_button_awaits_recovery_and_reports_when_it_did_not_reconnect():
    body = OS.split("const b = $('#os-net-again'", 1)[1].split(
        "const b = $('#os-net-relays'", 1
    )[0]
    assert "b.onclick = async" in body
    assert "await api.reconnectNetwork()" in body
    assert "b.disabled=true" in body
    assert "again.textContent='Try again'" in body

