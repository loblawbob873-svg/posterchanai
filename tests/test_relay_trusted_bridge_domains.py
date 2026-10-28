"""A TRUSTED BRIDGE: accounts a bridge's own domain vouches for (NIP-05) may publish though not in the web of trust.

Asked 2026-10-28 for glowers.club's Matrix bridge ("let's do the trusted bridge too"): each Matrix user posts with
their own Nostr key and a NIP-05 at the bridge's domain (q@glowers.club, verified live the same day). Seeding them
into the web of trust would also trust everyone they follow; this trusts exactly the accounts the bridge vouches for,
and only while it still does. The profile's claim is never proof by itself -- the domain must answer with this key.

Drives the SHIPPED `_on_event` (the fixtures of tests/test_grasp_pr_kinds_accepted.py: nobody is a member).
"""
from __future__ import annotations

import json

from app.services.nostr.event import build_event
from app.services.nostr_relay import server as srv
from tests.test_grasp_pr_kinds_accepted import deliver, relay

Q_SK = bytes.fromhex("33" * 32)
OTHER_SK = bytes.fromhex("44" * 32)


def _pk(sk):
    return build_event(sk, 1, "x", [])["pubkey"]


def _bridge(monkeypatch, answers, trusted=("glowers.club",)):
    """A relay whose trusted bridge domains are `trusted`; `answers[(domain, name)]` is what the domain says."""
    calls = []

    async def lookup(domain, name):
        calls.append((domain, name))
        return answers.get((domain, name))
    monkeypatch.setattr(srv, "_nip05_lookup", lookup)
    s = relay()
    s.cfg["trusted_bridge_domains"] = set(trusted)
    return s, calls


def _profile(sk, nip05):
    return build_event(sk, 0, json.dumps({"name": "q", "nip05": nip05}), [])


def _stored_profile(s, ev):
    async def query(filters, hard_cap=None):
        f = filters[0]
        return [ev] if f.get("kinds") == [0] and ev["pubkey"] in (f.get("authors") or []) else []
    s.store.query = query


def test_a_bridge_users_profile_and_posts_are_accepted_when_the_domain_vouches(monkeypatch):
    s, calls = _bridge(monkeypatch, {("glowers.club", "q"): _pk(Q_SK)})
    prof = _profile(Q_SK, "q@glowers.club")
    assert deliver(s, prof)[2] is True, s.sent[-1]
    _stored_profile(s, prof)
    ok = deliver(s, build_event(Q_SK, 1, "hello from matrix", []))
    assert ok[2] is True, ok
    assert calls == [("glowers.club", "q")], ("checked more than once, or not at all", calls)


def test_a_profile_claiming_the_domain_is_not_proof(monkeypatch):
    """Anybody can WRITE q@glowers.club into a profile; the domain answers with q's real key."""
    s, _ = _bridge(monkeypatch, {("glowers.club", "q"): _pk(Q_SK)})
    ok = deliver(s, _profile(OTHER_SK, "q@glowers.club"))
    assert ok[2] is False and "web of trust" in ok[3], ok


def test_a_domain_that_is_not_trusted_lets_nobody_in(monkeypatch):
    s, calls = _bridge(monkeypatch, {("elsewhere.example", "q"): _pk(Q_SK)})
    ok = deliver(s, _profile(Q_SK, "q@elsewhere.example"))
    assert ok[2] is False and calls == [], (ok, calls)


def test_a_post_with_no_profile_here_is_still_gated(monkeypatch):
    s, _ = _bridge(monkeypatch, {("glowers.club", "q"): _pk(Q_SK)})
    ok = deliver(s, build_event(Q_SK, 1, "no profile yet", []))
    assert ok[2] is False and "web of trust" in ok[3], ok


def test_an_unreachable_domain_is_not_a_yes(monkeypatch):
    s, _ = _bridge(monkeypatch, {})
    ok = deliver(s, _profile(Q_SK, "q@glowers.club"))
    assert ok[2] is False, ok


def test_with_no_trusted_domains_nothing_changes(monkeypatch):
    s, calls = _bridge(monkeypatch, {("glowers.club", "q"): _pk(Q_SK)}, trusted=())
    ok = deliver(s, _profile(Q_SK, "q@glowers.club"))
    assert ok[2] is False and calls == [], (ok, calls)


def test_taking_a_domain_off_the_list_stops_it_vouching_at_once(monkeypatch):
    """A day-long cached yes must not outlive the operator removing the bridge."""
    s, _ = _bridge(monkeypatch, {("glowers.club", "q"): _pk(Q_SK)})
    prof = _profile(Q_SK, "q@glowers.club")
    assert deliver(s, prof)[2] is True
    _stored_profile(s, prof)
    s.cfg["trusted_bridge_domains"] = set()
    ok = deliver(s, build_event(Q_SK, 1, "after removal", []))
    assert ok[2] is False and "web of trust" in ok[3], ok


def test_the_setting_reaches_the_relay_and_the_admin_list():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert "nostr_relay_trusted_bridge_domains" in (root / "app/schemas.py").read_text()
    from app.services import relay_lists
    assert relay_lists.LISTS["nostr_relay_trusted_bridge_domains"] == "domain"
    html = (root / "templates/admin/tabs/nostr_relay.html").read_text()
    assert 'id="nostr_relay_trusted_bridge_domains" name="nostr_relay_trusted_bridge_domains"' in html
