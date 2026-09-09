"""A kind-30617 published by /api/git/announce must carry a `relays` tag.

    MUST reject git repository announcements that do not list the service in both `clone` and
    `relays` tags unless implementing `GRASP-05`.
                                                    -- grasp.git 01.md, commit f35b4f9a4ed2

ngit publishes the kind-30618 repo state to the relays named there and ABORTS the whole push if it
can reach none of them ("state event failed to reach any git server relay"), so a repo announced
without the tag is clonable and not pushable while the git side is perfectly healthy.

This is the shape that hides: there are TWO producers of a 30617 here, and the one that got it right
(the git host's own `create`) is not the one the web UI calls. So these tests pin the tag on the
route AND assert the two agree, rather than checking one in isolation.
"""
from __future__ import annotations


from app.services import git_http_service as ghttp
from app.services import settings_store


def test_the_relay_url_is_derived_from_the_public_base(monkeypatch):
    """`https://poster.place/git` -> `wss://poster.place/relay`: this node's relay is served at
    /relay on the same host that fronts /git."""
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d="": {"git_server_public_base": "https://poster.place/git"}.get(k, d))
    assert ghttp.announce_relay_url() == "wss://poster.place/relay"


def test_an_explicit_setting_wins(monkeypatch):
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d="": {"client_relay_url": "wss://elsewhere.example/git",
                                         "git_server_public_base": "https://poster.place/git"}.get(k, d))
    assert ghttp.announce_relay_url() == "wss://elsewhere.example/git"


def test_an_unusable_base_yields_no_tag_rather_than_a_broken_one(monkeypatch):
    """"" means the announcement simply carries no `relays` tag. A malformed URL in it would be
    worse than its absence: ngit would try to publish state to it and fail the push."""
    monkeypatch.setattr(settings_store, "get", lambda k, d="": {}.get(k, d))
    assert ghttp.announce_relay_url() == ""


def _announce_tags(src: str) -> str:
    """The a_tags block of app/routers/git.py:announce_repo."""
    i = src.index("a_tags = [[\"d\", repo_id]]")
    return src[i:src.index("ann = build_event(seckey, 30617", i)]


def test_the_announce_route_appends_a_relays_tag():
    """Read from the shipped source: the route signs with the operator key and publishes to a live
    relay, so the tag list is the honest unit here."""
    src = open("app/routers/git.py").read()
    block = _announce_tags(src)
    assert '["relays"' in block, block
    assert "announce_relay_url()" in block, "the tag must be resolved, not spelled out again"


def test_both_producers_of_a_30617_resolve_the_relay_the_same_way():
    """The bug was one of two producers of the same event being wrong. The git host reads
    `_CONFIG['relay_url']`, which `git_http_service._read_config` fills with the same expression
    `announce_relay_url` uses — this fails if either side grows its own copy."""
    host = open("git_host_main.py").read()
    assert '_CONFIG.get("relay_url"' in host and '["relays", relay]' in host
    cfg = open("app/services/git_http_service.py").read()
    # def + exactly two call sites: announce_relay_url and _read_config. A third would be a
    # producer that has grown its own copy of the resolution.
    assert cfg.count("_relay_from_public_base(") == 3, cfg.count("_relay_from_public_base(")


def test_the_clone_tag_was_not_disturbed():
    """`clone` is the other half of the same MUST and already worked. Do not let a fix to one
    reorder or drop the other."""
    block = _announce_tags(open("app/routers/git.py").read())
    assert '["clone", clone]' in block
    assert block.index('["clone", clone]') < block.index('["relays"')
