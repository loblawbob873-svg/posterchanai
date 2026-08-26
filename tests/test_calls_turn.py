from types import SimpleNamespace

from app.routers import calls


def test_admin_turn_public_ip_is_advertised_beside_domain(monkeypatch):
    """A Cloudflare/split-DNS media domain must not make its configured direct relay unreachable."""
    monkeypatch.setattr(calls.settings_store, "all_settings", lambda: {
        "calls_enabled": "true",
        "turn_enabled": "true",
        "turn_shared_secret": "test-secret",
        "turn_domain": "media.poster.place",
        "turn_public_ip": "203.0.113.42",
        "turn_port": "3478",
    })
    result = calls.turn_credentials(SimpleNamespace(id=7))
    assert result["iceServers"][0]["urls"] == [
        "stun:media.poster.place:3478", "stun:203.0.113.42:3478"]
    assert result["iceServers"][1]["urls"] == [
        "turn:media.poster.place:3478?transport=udp",
        "turn:media.poster.place:3478?transport=tcp",
        "turn:203.0.113.42:3478?transport=udp",
        "turn:203.0.113.42:3478?transport=tcp",
    ]


def test_turn_urls_are_deduplicated_when_domain_is_public_ip(monkeypatch):
    monkeypatch.setattr(calls.settings_store, "all_settings", lambda: {
        "calls_enabled": "true", "turn_enabled": "true", "turn_shared_secret": "x",
        "turn_domain": "203.0.113.42", "turn_public_ip": "203.0.113.42",
    })
    result = calls.turn_credentials(SimpleNamespace(id=1))
    assert result["iceServers"][0]["urls"] == ["stun:203.0.113.42:3478"]
    assert len(result["iceServers"][1]["urls"]) == 2
