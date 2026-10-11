"""Admin → Nostr Relay → Reports: every NIP-56 report filed by, or about, a member of this node.

Filing a report from the client publishes a kind 1984 to this node's relay; until now the only reader of
those was the opt-in report BOT, so on a node without that bot running an admin never saw a report at all.
The list reads the SAME source the bot reads (community_stats.reports: strict relay read, members only),
and a relay that could not be asked is a 503 -- never an empty list that reads as "nobody reported
anything".
"""
import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.services import community_stats

ROOT = Path(__file__).resolve().parents[1]
ALICE = "a1" * 32
SPAMMER = "c3" * 32
POST = "e5" * 32


def _report_event():
    return {"id": "f6" * 32, "pubkey": ALICE, "kind": 1984, "created_at": 1_760_000_000,
            "tags": [["e", POST, "spam"], ["p", SPAMMER, "spam"], ["client", "PosterChan AI"]],
            "content": "Selling fake followers <b>now</b>", "sig": ""}


@pytest.fixture
def app_client(monkeypatch):
    from app.routers.admin import router
    from app.auth import get_admin_user
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(community_stats, "members", lambda: {ALICE: "@alice@poster.test"})

    async def no_puppets(pks):
        return {}
    monkeypatch.setattr(community_stats, "_puppet_accts", no_puppets)
    state = {"answer": [_report_event()]}

    async def query(filters):
        if isinstance(state["answer"], Exception):
            raise state["answer"]
        return list(state["answer"])
    monkeypatch.setattr(community_stats, "_query", query)

    async def get(path, admin=True):
        app.dependency_overrides.clear()
        if admin:
            app.dependency_overrides[get_admin_user] = lambda: object()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            return await c.get(path)
    return get, state


def test_the_admin_list_shows_a_report_a_member_filed(app_client):
    get, _ = app_client
    r = asyncio.run(get("/api/admin/relay/reports"))
    assert r.status_code == 200, r.text
    (rep,) = r.json()["reports"]
    assert rep["reporter"] == ALICE and rep["reporter_handle"] == "@alice@poster.test"
    assert rep["reported"] == SPAMMER and rep["type"] == "spam" and rep["note"] == POST
    assert rep["reason"] == "Selling fake followers <b>now</b>" and rep["at"] == 1_760_000_000
    assert rep["note_ref"].startswith("nostr:note1")


def test_the_admin_list_is_a_503_when_the_relay_cannot_be_asked(app_client):
    from app.services.relay_reader import Unavailable
    get, state = app_client
    state["answer"] = Unavailable("relay down")
    r = asyncio.run(get("/api/admin/relay/reports"))
    assert r.status_code == 503, (r.status_code, r.text)
    assert "reports" not in r.json(), "an unreadable relay must never be shown as an empty list"


def test_the_admin_list_is_admin_only(app_client):
    get, _ = app_client
    assert asyncio.run(get("/api/admin/relay/reports", admin=False)).status_code in (401, 403)


def test_the_admin_tab_draws_the_report_escaped_with_a_link_to_the_post(app_client):
    get, _ = app_client
    rep = asyncio.run(get("/api/admin/relay/reports")).json()["reports"][0]
    js = ROOT / "static/js/admin-reports.js"
    out = subprocess.run(["node", "-e", f"const m=require({json.dumps(str(js))});"
                          f"process.stdout.write(m.rowHtml({json.dumps(rep)}))"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    html = out.stdout
    assert "@alice@poster.test" in html and "spam" in html
    assert "Selling fake followers &lt;b&gt;now&lt;/b&gt;" in html and "<b>now</b>" not in html
    assert 'href="/' + rep["note_ref"][len("nostr:"):] + '"' in html
    tab = (ROOT / "templates/admin/tabs/nostr_relay.html").read_text()
    assert 'id="rep_list"' in tab and 'id="rep_summary"' in tab
    assert "admin-reports.js" in (ROOT / "templates/admin.html").read_text()
