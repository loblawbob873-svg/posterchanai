"""A file sent to somebody who does not have this app — decrypted in THEIR browser.

The client can already encrypt an attachment under a fresh random key, upload the ciphertext to
Blossom, and hand the key over in the URL FRAGMENT (`#pcenc1=…`, see app.js's uploadSharedEnc). That
was built for DMs, where both ends run this client and app.js understands the marker.

A text message is the case where they do not. The recipient has a phone, a browser, and no account
here, so `#pcenc1=` reaches software that has never heard of it and the link is inert. This serves
the one page that makes it work anywhere: it fetches the ciphertext and decrypts it with the key from
the fragment, in the recipient's own browser.

WHY THIS IS STILL PRIVATE, and it is worth being precise because the alternative was to give up and
send a plaintext blob. A fragment is never transmitted — not in the request line, not in Referer —
so this node serves the page and the ciphertext and has never seen the key. Plaintext was not merely
"less private": Blossom has no read authorization at all and `GET /list/<pubkey>` enumerates a
sender's blobs, so an unencrypted attachment is not just guessable, it is LISTABLE by anyone who
knows the sender's npub.

What it is NOT: the link is the whole secret. Anyone holding it can read the file, and SMS is not a
confidential channel. The page says so on the card rather than implying more than it can deliver.

NO AUTH, deliberately: the entire point is a stranger's phone. The security is the key, which is not
here.
"""
import os
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["sharelink"])

_TEMPLATES = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "..", "..", "templates"))

_SHA = re.compile(r"^[0-9a-f]{64}$")


@router.get("/f/{sha}")
async def shared_file(request: Request, sha: str):
    """The landing page for one shared encrypted blob.

    `/f/` and not `/file/` or `/share/`: this URL is typed into a text message beside whatever the
    sender wrote, and every character is one the person paid for and one more chance for a linkifier
    to clip it.
    """
    # An extension may ride along (the client keeps the blob's own, so image rules elsewhere match).
    sha = (sha or "").split(".")[0].lower()
    if not _SHA.match(sha):
        raise HTTPException(status_code=404, detail="Not found")
    # (request, name, context) — the modern Starlette signature. The old (name, {"request": …})
    # order still works and warns, and a deprecation that is merely tolerated becomes a broken node
    # on whichever upgrade finally removes it.
    return _TEMPLATES.TemplateResponse(
        request,
        "sharelink.html",
        {"blob_url": "/blossom/" + sha, "sha": sha},
        # The page holds no secret itself, but it is pointless to let a CDN or a proxy keep a copy
        # of a one-off transfer page, and `no-store` keeps the URL out of a shared cache index.
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
