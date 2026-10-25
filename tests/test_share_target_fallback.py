"""The Web Share Target fallback (`POST /client/share`) forwards a share's TEXT and stores nothing.

It is unauthenticated and parsed the whole multipart body with no limit, and Starlette spools every file part to
/tmp -- RAM on a node -- before the route ever looks at it. A share's files are the service worker's job; this route
only ever needed title/text/url. Found by the 2026-10-24 route-coverage scan (no test had called it).
"""
from fastapi.testclient import TestClient


def _client():
    from app.main import app
    return TestClient(app)


def test_a_text_share_is_forwarded_to_the_composer():
    r = _client().post("/client/share", data={"title": "Look", "text": "at this", "url": "https://example.com/a"},
                       follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/client?") and "title=Look" in loc and "text=at+this" in loc and "example.com" in loc


def test_a_file_in_the_share_is_never_spooled_and_still_redirects(monkeypatch):
    import starlette.datastructures as sd
    made = []
    real = sd.UploadFile.__init__

    def counting(self, *a, **k):
        made.append(1)
        real(self, *a, **k)
    monkeypatch.setattr(sd.UploadFile, "__init__", counting)
    r = _client().post("/client/share", data={"text": "hi"},
                       files={"media": ("big.bin", b"\0" * (4 * 1024 * 1024), "application/octet-stream")},
                       follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert made == [], "the share fallback accepted (and spooled) a file part"


def test_an_oversized_text_field_is_refused_without_storing_it():
    r = _client().post("/client/share", data={"text": "x" * (2 * 1024 * 1024)}, follow_redirects=False)
    assert r.status_code == 303 and "x" * 1000 not in r.headers["location"], r.headers.get("location", "")[:100]
