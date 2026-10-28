"""A busy storage node is "try again", never a lost upload (2026-10-27).

Measured 12:10-12:11 that day: six uploads in a row died with httpx.ReadTimeout from the storage node (nas, its disks
saturated by a backup) and went back to the client as 500 "storage error". The proxy hop now raises Unavailable for a
store that did not answer or answered 502/503/504 -- which the upload route answers 503 + Retry-After -- and the client
resends a content-addressed blob (tests/client/upload_retries_a_busy_store_runtime.mjs, run below).
"""
import asyncio
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.services import blossom_service as bs
from app.services.relay_reader import Unavailable

ROOT = Path(__file__).resolve().parents[1]


def _with_store(monkeypatch, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(bs, "_client", lambda: client)
    monkeypatch.setattr(bs, "_proxy_headers", lambda: {})


@pytest.mark.parametrize("fail", [httpx.ReadTimeout("timed out"), httpx.ConnectError("refused"),
                                  httpx.RemoteProtocolError("closed")])
def test_a_store_that_does_not_answer_is_unavailable(monkeypatch, fail):
    def handler(request):
        raise fail
    _with_store(monkeypatch, handler)
    with pytest.raises(Unavailable):
        asyncio.run(bs._proxy_put("http://store.test:3051", "ab" * 32, b"x", "image/png"))


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_store_that_says_it_is_busy_is_unavailable(monkeypatch, status):
    _with_store(monkeypatch, lambda request: httpx.Response(status, text="busy"))
    with pytest.raises(Unavailable):
        asyncio.run(bs._proxy_put("http://store.test:3051", "ab" * 32, b"x", "image/png"))


def test_a_store_that_refuses_is_still_a_failure_and_a_good_one_stores(monkeypatch):
    _with_store(monkeypatch, lambda request: httpx.Response(400, text="bad path"))
    with pytest.raises(RuntimeError) as e:
        asyncio.run(bs._proxy_put("http://store.test:3051", "ab" * 32, b"x", "image/png"))
    assert not isinstance(e.value, Unavailable), "a real refusal must not be retried as if the store were busy"
    _with_store(monkeypatch, lambda request: httpx.Response(200, json={"path": "blossom/ab/x"}))
    assert asyncio.run(bs._proxy_put("http://store.test:3051", "ab" * 32, b"x", "image/png")) == "blossom/ab/x"


def test_the_upload_route_answers_unavailable_with_retry_after():
    src = (ROOT / "app/routers/blossom.py").read_text()
    up = src[src.index("        await blossom_service.save_blob(db, pubkey, data, mime"):][:400]
    assert "except Unavailable as e:\n        return _unavailable(e)" in up
    un = src[src.index("def _unavailable(e)"):][:500]
    assert "503" in un and 'r.headers["Retry-After"]' in un


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_client_resends_to_a_busy_store():
    r = subprocess.run(["node", str(ROOT / "tests/client/upload_retries_a_busy_store_runtime.mjs")], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr
