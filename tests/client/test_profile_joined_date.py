"""Profiles show the earliest honest Nostr activity date without breaking narrow screens."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()


def test_profile_uses_earliest_known_signed_event_and_labels_the_limit():
    body = APP[APP.index("async function renderProfileView(pk){"):
               APP.index("function editProfile", APP.index("async function renderProfileView(pk){"))]
    assert "_seenEvents.reduce" in body
    assert "Joined Nostr" in body
    assert "earliest signed event currently available" in body


def test_joined_date_is_a_compact_wrapping_safe_chip():
    assert ".prof-joined{display:inline-flex;align-items:center" in CSS
    assert "border-radius:999px" in CSS
