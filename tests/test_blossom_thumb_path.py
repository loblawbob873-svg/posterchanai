"""Thumbnails must live on their own PATH, not on ?thumb=1.

Caches key on the path. Cloudflare and our own nginx both ignored the query string, so
`<sha>.mp4` and `<sha>.mp4?thumb=1` shared ONE cache entry — pinned for a year by the
`Cache-Control: immutable, max-age=31536000` these responses carry. Whichever URL was fetched
first won. Measured through the public edge, on a real blob:

    GET /<sha>.mp4?thumb=1  ->  200 video/mp4  1,612,155 bytes   cf-cache-status: HIT

The Files grid's <img> was handed a 1.6 MB MP4, could not decode it, and fell back to the 🎬 icon —
permanently, for that blob, at that edge. Image tiles never showed the bug because the same collision
hands them the FULL-SIZE image, which renders perfectly: hence "video thumbnails are broken, image
ones are fine", and hence the appearance of depending on the browser, or on Tor. It depends on
nothing but which of the two URLs that edge cached first.

Every check from inside the LAN passed throughout, because poster.place and media.poster.place both
resolve to the local nginx there — the edge is only in the path from outside.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "static", "js", "client", "app.js")
BLOSSOM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "app", "routers", "blossom.py")


def test_the_server_serves_a_thumbnail_path():
    with open(BLOSSOM) as fh:
        src = fh.read()
    assert '"/thumb/{sha256}"' in src, (
        "no /thumb/<sha> route — thumbnails would go back to ?thumb=1 on the blob's own URL, which "
        "shares a cache entry with the blob itself")
    # It must NOT be a route argument: FastAPI turns those into query parameters, which would put the
    # thumbnail back in the query string it was just taken out of.
    assert "async def get_blob(sha256: str, request: Request, db: Session = Depends(get_db)):" in src, \
        "get_blob grew an argument; a route argument is a public query parameter in FastAPI"
    assert "force_thumb: bool = False" in src and "async def _serve_blob" in src, \
        "the shared implementation should carry force_thumb, not the route"


def test_the_query_form_still_works():
    """Installed clients — a cached PWA, an older APK, an older desktop build — keep requesting
    ?thumb=1, and their thumbnails must not go dark on deploy."""
    with open(BLOSSOM) as fh:
        src = fh.read()
    assert 'request.query_params.get("thumb")' in src, "the old spelling must keep working"


def test_the_client_builds_a_path_not_a_query():
    with open(APP_JS) as fh:
        src = fh.read()
    m = re.search(r"function thumbUrl\(u\)\{[\s\S]*?\n  \}", src)
    assert m, "thumbUrl moved"
    body = m.group(0)
    assert "'/thumb/'" in body, "thumbUrl no longer builds a /thumb/ path"


def test_the_lightbox_undoes_both_spellings():
    """The lightbox turns a tile's URL back into the full-res one. It used to only drop ?thumb — with
    the path form that leaves you looking at the 320px JPEG full-screen."""
    with open(APP_JS) as fh:
        src = fh.read()
    i = src.index("function openLightbox(")
    norm = src[i:i + 700]
    assert "searchParams.delete('thumb')" in norm, "the query spelling must still be undone"
    assert "/thumb/" in norm, "the path spelling is not undone — the lightbox would show the thumbnail"
