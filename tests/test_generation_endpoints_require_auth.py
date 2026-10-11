"""THE GPU GENERATION ENDPOINTS REFUSE A CALLER NOBODY PROVED.

/api/generate-music, /api/generate-voice, /api/generate-video and /api/effects/* each had an auth
dependency that tried a peer secret, an API key and a session -- and then, whatever happened, ended in
`return True`. So anyone on the internet could run GPU jobs on this node. (Found 2026-10-11 while adding
the bot's musicgeni; image_api.get_image_auth was always strict, which is how peer load-balancing was
proven to work without the fall-through: nas answers server1's strict /api/generate-image with 200.)

Each dependency is called directly: a stranger gets 401, the flag header WITHOUT the shared secret is a
stranger, a peer with the secret passes, a real API key passes, and an account store that could not be
read is a 503 (Unavailable), never "not logged in".
"""
import asyncio
from unittest import mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.routers import effects_api, music_api, video_api, voice_api
from app.services.relay_reader import Unavailable
from app.utils import lb_auth

DEPS = [(effects_api, "get_effects_auth"), (music_api, "get_music_auth"),
        (video_api, "get_video_auth"), (voice_api, "get_voice_auth")]
SECRET = "s3cret-for-the-test"


def _req(headers=None):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": raw, "query_string": b""})


def _call(mod, name, headers=None, x_api_key=None, authorization=None):
    return asyncio.run(getattr(mod, name)(_req(headers), authorization=authorization,
                                          x_api_key=x_api_key, db=None))


@pytest.fixture(autouse=True)
def _no_user(monkeypatch):
    for mod, _ in DEPS:
        monkeypatch.setattr(mod, "get_current_user_optional", lambda *a, **k: None)
    monkeypatch.setattr(lb_auth, "shared_secret", lambda: SECRET)


@pytest.mark.parametrize("mod,name", DEPS, ids=[n for _, n in DEPS])
def test_a_stranger_is_refused(mod, name):
    with pytest.raises(HTTPException) as e:
        _call(mod, name)
    assert e.value.status_code == 401


@pytest.mark.parametrize("mod,name", DEPS, ids=[n for _, n in DEPS])
def test_the_peer_flag_without_the_secret_is_a_stranger(mod, name):
    with pytest.raises(HTTPException):
        _call(mod, name, headers={lb_auth.FLAG_HEADER_NAME: "true"})


@pytest.mark.parametrize("mod,name", DEPS, ids=[n for _, n in DEPS])
def test_a_peer_node_with_the_secret_passes(mod, name):
    assert _call(mod, name, headers=lb_auth.headers()) is True


@pytest.mark.parametrize("mod,name", DEPS, ids=[n for _, n in DEPS])
def test_a_real_api_key_passes_and_a_made_up_one_does_not(mod, name):
    async def query(token):
        return ("row", 7) if token == "good-key" else (None, None)
    with mock.patch("app.utils.auth_utils.aquery_api_key", side_effect=query), \
         mock.patch("app.utils.auth_utils.get_user_from_api_key", return_value=object()):
        assert _call(mod, name, x_api_key="good-key") is True
        assert _call(mod, name, authorization="Bearer good-key") is True
        with pytest.raises(HTTPException):
            _call(mod, name, x_api_key="made-up")


@pytest.mark.parametrize("mod,name", DEPS, ids=[n for _, n in DEPS])
def test_a_signed_in_session_passes(mod, name, monkeypatch):
    monkeypatch.setattr(mod, "get_current_user_optional", lambda request, creds, db: object())
    assert _call(mod, name) is True


@pytest.mark.parametrize("mod,name", DEPS, ids=[n for _, n in DEPS])
def test_an_unreadable_account_store_is_503_not_401(mod, name, monkeypatch):
    def boom(*a, **k):
        raise Unavailable("relay down")
    monkeypatch.setattr(mod, "get_current_user_optional", boom)
    with pytest.raises(Unavailable):
        _call(mod, name)
