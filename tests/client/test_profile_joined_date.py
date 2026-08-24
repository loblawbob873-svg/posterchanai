"""Profiles never invent an account-registration date from a partial relay cache."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()


def test_profile_does_not_present_partial_history_as_a_joined_date():
    body = APP[APP.index("async function renderProfileView(pk){"):
               APP.index("function editProfile", APP.index("async function renderProfileView(pk){"))]
    assert "_seenEvents.reduce" not in body
    assert "Joined Nostr" not in body
    assert "account-registration event" in body


def test_no_joined_date_markup_remains_in_profile_styles():
    assert ".prof-joined{" not in CSS
