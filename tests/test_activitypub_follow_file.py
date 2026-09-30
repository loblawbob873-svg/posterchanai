"""Export your fediverse follows as a file, and import one -- here or on any fediverse server.

Asked for: "add ability for users to export their Fediverse follows so they can import it back into
posterchan or a different fediverse server". The file is Mastodon's `following_accounts.csv` (what
Mastodon, Pleroma, Akkoma and GoToSocial all import). Two endpoints:

  * export-following maps the member's own follow list (sent by the client: it is their signed
    kind-3) to the fediverse accounts in it -- read-only, nothing fetched, nothing minted, in their
    list's order;
  * import-following takes a FILE's addresses as well as a public account; every address is only an
    address (its actor is fetched from its own server) and junk in the file is skipped, never sent.

The round trip is the promise: what one node exports, this node imports to the same people.
"""

from tests.test_activitypub import (  # noqa: F401  (fixtures)
    SimpleUser, _actor_doc, client, convert, ident, remote, world,
)
from app.models import FediPuppet

A1 = "https://mastodon.example/users/user1"
A2 = "https://pleroma.example/users/user2"


def _user(client):
    from app.routers.auth import get_current_user
    client.app.dependency_overrides[get_current_user] = lambda: SimpleUser(False)


def _seed_puppets(world):
    s = world["Session"]()
    s.add(FediPuppet(actor_uri=A1, acct="user1@mastodon.example", pubkey_hex="11" * 32, nip05_name="u1"))
    s.add(FediPuppet(actor_uri=A2, acct="user2@pleroma.example", pubkey_hex="22" * 32, nip05_name="u2"))
    s.commit()


def test_export_lists_only_the_fediverse_accounts_in_the_members_order(client, world):
    _seed_puppets(world)
    _user(client)
    try:
        native = "ab" * 32                      # a Nostr account: not a fediverse follow
        r = client.post("/api/activitypub/export-following",
                        json={"pubkeys": ["22" * 32, native, "not-a-key", "11" * 32, "22" * 32]})
    finally:
        client.app.dependency_overrides.clear()
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["following"] == 3
    assert [a["acct"] for a in got["accounts"]] == ["user2@pleroma.example", "user1@mastodon.example"]
    assert got["accounts"][0] == {"pubkey": "22" * 32, "acct": "user2@pleroma.example", "actor": A2}


def test_export_and_import_need_a_signed_in_member(client):
    assert client.post("/api/activitypub/export-following", json={"pubkeys": []}).status_code == 401
    assert client.post("/api/activitypub/import-following", json={"accounts": ["a@b.example"]}).status_code == 401


def test_a_file_imports_its_addresses_and_skips_everything_else(client, world, monkeypatch):
    world["actors"][A1] = _actor_doc(1)
    asked = []

    async def webfinger(handle):
        asked.append(handle)
        if handle == "user1@mastodon.example":
            return A1
        raise remote.FetchError("unknown")
    monkeypatch.setattr(remote, "webfinger", webfinger)
    _user(client)
    try:
        r = client.post("/api/activitypub/import-following", json={"accounts": [
            "Account address", "@user1@mastodon.example", "USER1@MASTODON.EXAMPLE", "not an account",
            "https://evil.example/users/x", "ghost@gone.example"]})
    finally:
        client.app.dependency_overrides.clear()
    assert r.status_code == 200, r.text
    got = r.json()
    want = ident.puppet_for(convert.account_from_actor(_actor_doc(1)))["pubkey_hex"]
    assert got["people"] == [{"pubkey": want, "acct": "user1@mastodon.example"}], got
    assert got["following"] == 2, "the header, the prose and the URL are not accounts; the duplicate is one"
    assert sorted(asked) == ["ghost@gone.example", "user1@mastodon.example"], asked


def test_what_this_node_exports_it_imports_to_the_same_people(client, world, monkeypatch):
    world["actors"][A1] = _actor_doc(1)

    async def webfinger(handle):
        if handle == "user1@mastodon.example":
            return A1
        raise remote.FetchError("unknown")
    monkeypatch.setattr(remote, "webfinger", webfinger)
    pk = ident.puppet_for(convert.account_from_actor(_actor_doc(1)))["pubkey_hex"]
    s = world["Session"]()
    s.add(FediPuppet(actor_uri=A1, acct="user1@mastodon.example", pubkey_hex=pk, nip05_name="u1"))
    s.commit()
    _user(client)
    try:
        out = client.post("/api/activitypub/export-following", json={"pubkeys": [pk]}).json()
        back = client.post("/api/activitypub/import-following",
                           json={"accounts": [a["acct"] for a in out["accounts"]]}).json()
    finally:
        client.app.dependency_overrides.clear()
    assert [p["pubkey"] for p in back["people"]] == [pk]


def test_an_empty_file_is_a_sentence_and_imports_share_the_rate_limit(client, monkeypatch):
    from app.routers import activitypub as routes
    calls = []

    async def fake(accts):
        calls.append(accts)
        return {"following": len(accts), "people": []}
    monkeypatch.setattr(routes, "_import_list", fake)
    _user(client)
    try:
        r = client.post("/api/activitypub/import-following", json={"accounts": ["Account address", "junk"]})
        assert r.status_code == 400 and "no fediverse accounts" in r.json()["detail"]
        assert client.post("/api/activitypub/import-following", json={"accounts": "x@y.example"}).status_code == 400
        assert client.post("/api/activitypub/import-following", json={"accounts": ["a@b.example"]}).status_code == 200
        assert client.post("/api/activitypub/import-following", json={"accounts": ["a@b.example"]}).status_code == 429
        assert client.post("/api/activitypub/import-following", json={"account": "me@old.example"}).status_code == 429
    finally:
        client.app.dependency_overrides.clear()
    assert calls == [["a@b.example"]]
