#!/usr/bin/env python3
"""Ask the REAL model about a REAL link and check the answer came from the page.

    venv-unified/bin/python scripts/check_url_reading.py [--base http://127.0.0.1:3051]

`tests/test_url_reading.py` is offline: it asserts the render trigger, the message order and the
intent guard. None of that can prove the thing that was actually reported — that the answer was
about somebody else entirely. The only instrument for that is the model, so this drives the whole
path (fetch → render if the page said nothing → grounded message → generate) against THIS NODE'S
RUNNING SERVICE, and reads what comes back.

Needs the service up on `--base` and an API key in the database. Costs three generations.
Exit 0 = the answers came from the pages.

The reported failure, verbatim from the journal:

    [STREAM] First content chunk: "Jordan Peterson is definitely an asshole. Here's w"

with 4709 characters of a completely different Jordan's profile in the same prompt.
"""
import argparse
import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal                      # noqa: E402
from app.services.search_service import SearchService      # noqa: E402

PROFILE_URL = "https://poster.place/npub1p3za04z7mv86mkjzzhfkegxe4wsvwudct5m3wajt3gfg6hjy8exslltqmk"
PROFILE_Q = ("check Jordan's posts and tell me if he's an asshole or nice person and explain why: "
             + PROFILE_URL)
# Words that can only have come from the page itself, and ones that can only have come from the
# model's memory of a famous namesake.
FROM_THE_PAGE = ("crypto", "anarchist", "bitcoin", "monero", "meme", "nostr", "sats", "zap")
FROM_ITS_PRIORS = ("peterson", "michael jordan", "air jordan", "psychologist", "12 rules")

NEWS_URL = "https://www.cnn.com/"
NEWS_Q = f"Summarize this page: {NEWS_URL}"

fails = []


def check(ok: bool, label: str, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail else ""))
    if not ok:
        fails.append(label)


def api_key(db) -> str:
    from sqlalchemy import text
    row = db.execute(text("SELECT key FROM api_keys ORDER BY id DESC LIMIT 1")).fetchone()
    if not row:
        raise SystemExit("no API key in the database — make one in the UI first")
    return row[0]


async def generate(base: str, key: str, messages: list) -> str:
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(f"{base}/v1/chat/completions",
                         headers={"Authorization": f"Bearer {key}"},
                         json={"model": "default", "messages": messages, "max_tokens": 400})
        r.raise_for_status()
        return (r.json()["choices"][0]["message"]["content"] or "").strip()


async def read_and_ask(db, base, key, question: str, url: str) -> str:
    """Exactly what a chat surface does with a message that has a link in it."""
    urls = SearchService.extract_urls(question)
    if url not in urls:
        raise SystemExit(f"the link was not extracted from {question!r}")
    fetched = await asyncio.wait_for(SearchService(db).fetch_urls(urls, max_urls=3), timeout=15)
    ctx = ""
    for r in fetched:
        if r.get("content") and not r.get("error"):
            ctx += f"\n\n---\nContent from {r['url']}:\nTitle: {r['title']}\n\n{r['content']}\n---"
    print(f"    read {len(ctx)} chars"
          + (" (rendered in a browser)" if any(r.get("rendered") for r in fetched) else ""))
    if not ctx.strip():
        raise SystemExit(f"nothing was read from {url}")
    return await generate(base, key, [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": SearchService.build_grounded_message(question, ctx)},
    ])


async def intent_for(db, base, key, message: str) -> str:
    """The real classifier, on the real model — only the transport is swapped."""
    from app.services import intent_service as _is

    class _Chat:
        async def chat(self, messages, **kw):
            return await generate(base, key, messages)

    svc = _is.IntentService.__new__(_is.IntentService)
    svc.db, svc.user, svc.chat_service = db, None, _Chat()
    return ((await svc.detect_intent(message)) or {}).get("command", "")


async def main(base: str) -> int:
    db = SessionLocal()
    key = api_key(db)

    print("\n1. a profile link — the reported failure")
    answer = await read_and_ask(db, base, key, PROFILE_Q, PROFILE_URL)
    low = answer.lower()
    print(f"    answer: {' '.join(answer.split())[:400]}")
    hits = [w for w in FROM_THE_PAGE if w in low]
    strays = [w for w in FROM_ITS_PRIORS if w in low]
    check(bool(hits), "the answer is built from what the page said", f"matched: {hits}")
    check(not strays, "no famous namesake was substituted for the person at the link",
          f"found: {strays}" if strays else "")

    print("\n2. a link is read, not looked up")
    for q in (PROFILE_Q, NEWS_Q):
        cmd = await intent_for(db, base, key, q)
        verb = cmd.split()[0].lower() if cmd else ""
        check(verb not in ("search", "images", "news"),
              f"{q[:38]!r}… went to the page, not the search engine", f"intent: {cmd or 'none'}")

    print("\n3. an ordinary news page still summarizes")
    answer = await read_and_ask(db, base, key, NEWS_Q, NEWS_URL)
    print(f"    answer: {' '.join(answer.split())[:400]}")
    check(len(answer.strip()) > 80, "a real summary came back")
    check("could not" not in answer.lower()[:120], "it did not claim it could not read the page")

    print("\n" + ("ALL CHECKS PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("PC_BASE", "http://127.0.0.1:3051"))
    sys.exit(asyncio.run(main(ap.parse_args().base.rstrip("/"))))
