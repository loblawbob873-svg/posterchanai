"""/v1/chat/completions stops answering anonymous callers once the node can tell its peers apart
(security review #4), without breaking the two legitimate callers that send no user key.

Anybody could POST /v1/chat/completions (also /chat/completions, /api/chat/completions) with no
credentials: free GPU, the load-balanced nodes, the server fetching URLs from the prompt. The load
balancer's node-to-node calls carry no user key, and the bots' AI calls got through ONLY because anonymous
calls were allowed (a stale key 401s and the bot retried without one). So: the bots send their
manager-issued credential, a peer proves itself with lb_shared_secret, and anonymous calls are refused as
soon as the node HAS a secret -- before that, refusing would take load balancing down.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import openai_api
from app.services import community_stats
from app.utils import lb_auth


class _Req:
    def __init__(self, headers):
        self.headers = {k.lower(): v for k, v in headers.items()}


def _call(headers, secret, monkeypatch):
    monkeypatch.setattr(lb_auth, "shared_secret", lambda: secret)
    monkeypatch.setattr(community_stats, "bot_token", lambda: "bot-tok")
    return openai_api.verify_api_key(_Req(headers), authorization=headers.get("Authorization"),
                                     x_api_key=headers.get("X-API-Key"), db=None)


def test_anonymous_is_refused_once_the_node_has_a_peer_secret(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _call({}, "s3cret", monkeypatch)
    assert e.value.status_code == 401


def test_without_a_secret_nothing_changes_yet(monkeypatch):
    assert _call({}, "", monkeypatch) is None


def test_a_bot_with_its_credential_is_answered(monkeypatch):
    assert _call({"X-PC-Community-Token": "bot-tok"}, "s3cret", monkeypatch) is None
    with pytest.raises(HTTPException):
        _call({"X-PC-Community-Token": "wrong"}, "s3cret", monkeypatch)


def test_a_peer_node_with_the_secret_is_answered(monkeypatch):
    h = {lb_auth.FLAG_HEADER_NAME: "true", lb_auth.AUTH_HEADER_NAME: "s3cret"}
    assert _call(h, "s3cret", monkeypatch) is None


def test_the_bots_send_their_credential(monkeypatch):
    monkeypatch.setenv("POSTERCHANAI_COMMUNITY_TOKEN", "bot-tok")
    import importlib, sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "botframework"))
    client = importlib.import_module("ai.client")
    assert client._request_headers().get("X-PC-Community-Token") == "bot-tok"
    assert client._request_headers(drop_auth=True).get("X-PC-Community-Token") == "bot-tok"
