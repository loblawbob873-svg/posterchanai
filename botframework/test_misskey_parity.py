"""Parity harness for the Misskey dedup (Phase 4).

The Misskey shim swaps only the transport (misskey_post / upload_media_to_misskey), reusing
misskey.py's higher-level functions verbatim — so parity is structural. This harness confirms
the swap by driving identical inputs through legacy `misskey` and `misskey_shim` with the
network mocked on each side, then diffing the semantic /api/notes/create payload (text,
replyId, visibility, fileIds count) plus the read-path shapes.

Run:  MISSKEY_SERVER=https://misskey.test MISSKEY_ACCESS_TOKEN=tok \
      ../venv/bin/python test_misskey_parity.py
"""

import os
import sys

os.environ.setdefault("MISSKEY_SERVER", "https://misskey.test")
os.environ.setdefault("MISSKEY_ACCESS_TOKEN", "tok")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NOTE = {"id": "NOTE123", "user": {"username": "alice", "host": "remote.tld", "id": "U1"}}
OWN = "bot"


class _Resp:
    def __init__(self, status_code=200, json_data=None, text="{}"):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    captured = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, files=None, json=None, data=None, params=None):
        _FakeAsyncClient.captured.append({"url": url, "json": json, "files": files, "data": data})
        if "drive/files/create" in url:
            return _Resp(200, {"id": "FILE_S"})
        if url.endswith("/api/i"):
            return _Resp(200, {"username": OWN, "id": "1"})
        if "i/notifications" in url:
            return _Resp(200, [{"type": "mention", "id": "n1", "note": {"id": "m1"}}])
        if "notes/mentions" in url:
            return _Resp(200, [{"id": "m1"}])
        return _Resp(200, {"createdNote": {"id": "new"}})


legacy_captured = []


def _fake_requests_post(url, headers=None, files=None, data=None, json=None, timeout=None):
    legacy_captured.append({"url": url, "json": json, "files": files, "data": data})
    if "drive/files/create" in url:
        return _Resp(200, {"id": "FILE_L"})
    if url.endswith("/api/i"):
        return _Resp(200, {"username": OWN, "id": "1"})
    if "i/notifications" in url:
        return _Resp(200, [{"type": "mention", "id": "n1", "note": {"id": "m1"}}])
    if "notes/mentions" in url:
        return _Resp(200, [{"id": "m1"}])
    return _Resp(200, {"createdNote": {"id": "new"}})


def _note_create(captured):
    posts = [c for c in captured if c["url"].rstrip("/").endswith("/notes/create")]
    assert len(posts) == 1, f"expected 1 notes/create, got {len(posts)}"
    p = posts[0]["json"] or {}
    return {
        "text": p.get("text"),
        "replyId": p.get("replyId"),
        "visibility": p.get("visibility"),
        "files": len(p.get("fileIds") or []),
    }


def _diff(label, a, b):
    if a != b:
        print(f"  MISMATCH [{label}]")
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                print(f"    {k}: legacy={a.get(k)!r}  shim={b.get(k)!r}")
        return False
    print(f"  OK [{label}] {a}")
    return True


def main():
    import requests
    import httpx
    import misskey
    import misskey_shim

    ok = True

    needed = ["get_mentions", "get_note", "get_own_account", "send_reply",
              "post_image_to_fediverse", "get_thread_history", "get_thread_images",
              "download_image_from_url"]
    missing = [n for n in needed if not hasattr(misskey_shim, n)]
    if missing:
        print(f"  MISMATCH [interface] shim missing: {missing}"); ok = False
    else:
        print(f"  OK [interface] shim exposes all {len(needed)} listener symbols")

    requests.get, requests.post = _fake_requests_post, _fake_requests_post
    httpx.AsyncClient = _FakeAsyncClient

    # NOTE: misskey_shim monkeypatches misskey.misskey_post/upload at import; call legacy paths
    # by invoking misskey.* AFTER re-pointing them back to the originals so the A/B is honest.
    _orig_post = misskey.misskey_post
    _orig_upload = misskey.upload_media_to_misskey
    # misskey_shim import already replaced them; capture the shim versions:
    _shim_post = misskey_shim._post
    _shim_upload = misskey_shim._upload

    def legacy(fn, *a, **k):
        misskey.misskey_post = _make_legacy_post()
        misskey.upload_media_to_misskey = _make_legacy_upload()
        try:
            return fn(*a, **k)
        finally:
            misskey.misskey_post = _shim_post
            misskey.upload_media_to_misskey = _shim_upload

    # Rebuild the ORIGINAL transport (requests-based) for the legacy side, since import-time
    # monkeypatching replaced the module globals. We reconstruct equivalent originals here.
    def _make_legacy_post():
        def p(method, params=None):
            url = f"{misskey.misskey_server}/api/{method}"
            body = {"i": misskey.misskey_token}
            if params:
                body.update(params)
            r = requests.post(url, headers=misskey.misskey_headers, json=body, timeout=30)
            return r.json() if r.text else {}
        return p

    def _make_legacy_upload():
        def u(image_bytes, filename="image.png", mime="image/png"):
            url = f"{misskey.misskey_server}/api/drive/files/create"
            r = requests.post(url, data={"i": misskey.misskey_token},
                              files={"file": (filename, image_bytes, mime)}, timeout=60)
            return r.json().get("id")
        return u

    # 1) reply text-only
    legacy_captured.clear(); _FakeAsyncClient.captured = []
    legacy(misskey.send_reply, NOTE, "Hello", own_acct=OWN)
    misskey_shim.send_reply(NOTE, "Hello", own_acct=OWN)
    ok &= _diff("reply text-only", _note_create(legacy_captured), _note_create(_FakeAsyncClient.captured))

    # 2) reply with image
    legacy_captured.clear(); _FakeAsyncClient.captured = []
    legacy(misskey.send_reply, NOTE, "pic", own_acct=OWN, image_bytes=b"PNGDATA")
    misskey_shim.send_reply(NOTE, "pic", own_acct=OWN, image_bytes=b"PNGDATA")
    ok &= _diff("reply with image", _note_create(legacy_captured), _note_create(_FakeAsyncClient.captured))

    # 3) top-level post with image
    legacy_captured.clear(); _FakeAsyncClient.captured = []
    legacy(misskey.post_image_to_fediverse, "a post", image_bytes=b"PNGDATA")
    misskey_shim.post_image_to_fediverse("a post", image_bytes=b"PNGDATA")
    ok &= _diff("post_image_to_fediverse", _note_create(legacy_captured), _note_create(_FakeAsyncClient.captured))

    # 4) read path: own account
    legacy_captured.clear(); _FakeAsyncClient.captured = []
    own_l = legacy(misskey.get_own_account)
    own_s = misskey_shim.get_own_account()
    ok &= _diff("get_own_account", {"username": own_l.get("username")}, {"username": own_s.get("username")})

    print("\nPARITY: " + ("PASS ✅" if ok else "FAIL ❌"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
