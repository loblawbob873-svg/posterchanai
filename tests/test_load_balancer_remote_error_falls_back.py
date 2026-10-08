"""A remote model server that answers with an ERROR falls back -- it is not handed to the person as their answer.

Found in the 2026-10-08 cleanup pass (pyflakes: `error_detected` assigned, never used). chat_stream raised
NoHealthyServersError on an `{"error": ...}` chunk -- inside the per-chunk try, whose `except Exception` caught it,
logged a warning and yielded the error chunk to the client. The caller only falls back (another server, or this
node's own model) on NoHealthyServersError, so a remote error became the reply. Runs the real chat_stream against
a fake remote server.
"""
import asyncio
import json

import httpx
import pytest

from app.services import load_balancer as lb


def _sse(*objs):
    return "".join(f"data: {json.dumps(o) if not isinstance(o, str) else o}\n\n" for o in objs)


def _stream(monkeypatch, body):
    async def healthy(servers):
        return "http://remote.test"
    monkeypatch.setattr(lb, "get_healthy_server", healthy)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body))
    real = httpx.AsyncClient
    monkeypatch.setattr(lb.httpx, "AsyncClient", lambda **kw: real(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}))
    bal = lb.LoadBalancer(["http://remote.test"])

    SENT.clear()

    async def go():
        async for ch in bal.chat_stream([{"role": "user", "content": "hi"}]):
            SENT.append(ch)
        return list(SENT)
    return asyncio.run(go())


SENT = []          # what reached the client, kept even when the stream then raises


def _delta(text):
    return {"choices": [{"delta": {"content": text}}]}


def test_an_error_before_any_content_falls_back_instead_of_being_the_answer(monkeypatch):
    body = _sse({"choices": [{"delta": {"role": "assistant"}}]}, {"error": {"message": "model failed to load"}}, "[DONE]")
    with pytest.raises(lb.NoHealthyServersError) as e:
        _stream(monkeypatch, body)
    assert "model failed to load" in str(e.value)
    # The person must not have been sent the error before the fallback answers them.
    assert not any("model failed to load" in c for c in SENT), SENT


def test_an_error_after_content_keeps_the_answer_the_person_already_has(monkeypatch):
    body = _sse(_delta("Hello"), _delta(" there"), {"error": {"message": "late hiccup"}}, "[DONE]")
    out = _stream(monkeypatch, body)
    text = "".join(out)
    assert "Hello" in text and " there" in text


def test_a_normal_stream_is_untouched(monkeypatch):
    out = _stream(monkeypatch, _sse(_delta("Hi"), _delta("!"), "[DONE]"))
    assert [json.loads(o[6:].strip())["choices"][0]["delta"]["content"] for o in out if "[DONE]" not in o] == ["Hi", "!"]


def test_a_tool_call_with_no_text_is_still_an_answer(monkeypatch):
    call = {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "f", "arguments": "{}"}}]}}]}
    out = _stream(monkeypatch, _sse(call, "[DONE]"))
    assert any("tool_calls" in o for o in out)
