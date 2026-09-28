"""WebFinger lookups follow redirects — safely — so split-domain fediverse accounts can be found.

Run: venv-unified/bin/python -m pytest tests/test_activitypub_webfinger_redirects.py

RFC 7033 §4.2 has clients follow a WebFinger redirect, and Mastodon's LOCAL_DOMAIN/WEB_DOMAIN,
GoToSocial's account-domain and Akkoma all use one: `@user@example.com` lives at
social.example.com. `remote.webfinger` used the shared client, which follows nothing, so every such
account read as "does not know". (2026-09-28 AP review.) Each hop must still pass the SAME address
checks as the first — https, not blocked, not a private address — or a redirect is an SSRF.
"""
import asyncio

import httpx
import pytest

from app.services.activitypub import remote

JRD = {"subject": "acct:dana@example.com",
       "links": [{"rel": "self", "type": "application/activity+json", "href": "https://social.example.com/users/dana"}]}


def run(c):
    return asyncio.run(c)


@pytest.fixture
def net(monkeypatch):
    routes, checked = {}, []

    def handler(req):
        key = str(req.url)
        if key not in routes:
            return httpx.Response(404)
        code, where = routes[key]
        if code in (301, 302, 307, 308):
            return httpx.Response(code, headers={"location": where})
        return httpx.Response(200, json=where)

    monkeypatch.setattr(remote, "client", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                                                        follow_redirects=False))

    async def check(url):
        checked.append(url)
        if not url.startswith("https://") or "internal" in url:
            raise remote.FetchError("refused " + url)
    monkeypatch.setattr(remote, "_check", check)
    return routes, checked


WF = "https://example.com/.well-known/webfinger?resource=acct:dana@example.com"
WF2 = "https://social.example.com/.well-known/webfinger?resource=acct:dana@example.com"


def test_a_split_domain_account_is_found_through_the_redirect(net):
    routes, checked = net
    routes[WF] = (301, WF2)
    routes[WF2] = (200, JRD)
    assert run(remote.webfinger("dana@example.com")) == "https://social.example.com/users/dana"
    assert checked == [WF, WF2], "every hop must be checked"


def test_a_relative_redirect_is_resolved(net):
    routes, _ = net
    routes[WF] = (302, "/wf2?resource=acct:dana@example.com")
    routes["https://example.com/wf2?resource=acct:dana@example.com"] = (200, JRD)
    assert run(remote.webfinger("dana@example.com")).endswith("/users/dana")


@pytest.mark.parametrize("target", ["http://social.example.com/wf", "https://internal.example/wf"])
def test_a_redirect_that_fails_the_address_checks_is_refused(net, target):
    routes, _ = net
    routes[WF] = (307, target)
    routes[target] = (200, JRD)
    with pytest.raises(remote.FetchError):
        run(remote.webfinger("dana@example.com"))


def test_a_redirect_loop_gives_up(net):
    routes, checked = net
    routes[WF] = (301, WF2)
    routes[WF2] = (301, WF)
    with pytest.raises(remote.FetchError):
        run(remote.webfinger("dana@example.com"))
    assert len(checked) <= 4
