"""Parity harness for the Pleroma dedup (Phase 4).

Drives identical inputs through the legacy `pleroma` client and the new `pleroma_shim`
(which routes through app.services.pleroma_service), with the network mocked on each side,
then diffs the SEMANTIC request each would send to /api/v1/statuses (status text,
in_reply_to_id, visibility, content_type, media count). If these match, the shim is a
behavior-preserving replacement for the listener's use of pleroma.

Run:  PLEROMA_ENDPOINT=https://example.test PLEROMA_ACCESS_TOKEN=tok \
      ../venv/bin/python test_pleroma_parity.py
(from the botframework/ directory). Exits non-zero on any mismatch.
"""

import os
import sys

os.environ.setdefault("PLEROMA_ENDPOINT", "https://example.test")
os.environ.setdefault("PLEROMA_ACCESS_TOKEN", "tok")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))           # botframework/
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

STATUS = {
    "id": "STATUS123",
    "account": {"acct": "alice@remote.tld"},
    "mentions": [{"acct": "bot@local.tld"}],
    "in_reply_to_id": "PARENT99",
}
OWN = "bot@local.tld"


# ---- fake HTTP layers --------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient used by app.services.pleroma_service."""
    captured = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, files=None, json=None, data=None, params=None):
        _FakeAsyncClient.captured.append({"method": "POST", "url": url, "json": json, "files": files})
        if "/media" in url:
            return _Resp(200, {"id": "MEDIA_S"})
        return _Resp(200, {"mentions": []})

    async def get(self, url, headers=None, params=None):
        _FakeAsyncClient.captured.append({"method": "GET", "url": url, "params": params})
        if "verify_credentials" in url:
            return _Resp(200, {"acct": OWN, "id": "1"})
        if "notifications" in url:
            return _Resp(200, [{"type": "mention", "id": "n1"}])
        return _Resp(200, {"id": "X"})


legacy_captured = []


def _fake_requests_post(url, headers=None, files=None, data=None, timeout=None):
    legacy_captured.append({"method": "POST", "url": url, "data": data, "files": files})
    if "/media" in url:
        return _Resp(200, {"id": "MEDIA_L"})
    return _Resp(200, {"mentions": []})


def _fake_requests_get(url, headers=None, params=None, timeout=None):
    legacy_captured.append({"method": "GET", "url": url})
    if "verify_credentials" in url:
        return _Resp(200, {"acct": OWN, "id": "1"})
    if "notifications" in url:
        return _Resp(200, [{"type": "mention", "id": "n1"}])
    return _Resp(200, {"id": "X"})


# ---- payload extraction (normalize both encodings to one shape) --------------

def _status_post(captured, is_json):
    """Pull the single POST to /api/v1/statuses and normalize its fields."""
    posts = [c for c in captured if c["method"] == "POST" and c["url"].rstrip("/").endswith("/statuses")]
    assert len(posts) == 1, f"expected 1 status POST, got {len(posts)}"
    c = posts[0]
    if is_json:
        p = c["json"] or {}
        media = p.get("media_ids") or []
        return {
            "status": p.get("status"),
            "visibility": p.get("visibility"),
            "in_reply_to_id": p.get("in_reply_to_id"),
            "content_type": p.get("content_type"),
            "media_count": len(media),
        }
    # legacy: data is a list of (key, value) tuples
    data = c["data"] or []
    d = {}
    media_count = 0
    for k, v in data:
        if k == "media_ids[]":
            media_count += 1
        else:
            d[k] = v
    return {
        "status": d.get("status"),
        "visibility": d.get("visibility"),
        "in_reply_to_id": d.get("in_reply_to_id"),
        "content_type": d.get("content_type"),
        "media_count": media_count,
    }


def _diff(label, a, b):
    if a != b:
        print(f"  MISMATCH [{label}]")
        for key in sorted(set(a) | set(b)):
            if a.get(key) != b.get(key):
                print(f"    {key}: legacy={a.get(key)!r}  shim={b.get(key)!r}")
        return False
    print(f"  OK [{label}] {a}")
    return True


def main():
    import requests
    import httpx
    import pleroma
    import pleroma_shim

    ok = True

    # 0) interface parity — shim exposes everything the listener imports from pleroma
    needed = ["get_last_20_seconds_notifications", "get_status", "get_notifications",
              "get_own_account", "send_reply", "post_image_to_fediverse",
              "get_thread_history", "get_thread_images", "download_image_from_url"]
    missing = [n for n in needed if not hasattr(pleroma_shim, n)]
    if missing:
        print(f"  MISMATCH [interface] shim missing: {missing}")
        ok = False
    else:
        print(f"  OK [interface] shim exposes all {len(needed)} listener symbols")

    # 1) reply, text-only
    requests.get, requests.post = _fake_requests_get, _fake_requests_post
    httpx.AsyncClient = _FakeAsyncClient

    legacy_captured.clear(); _FakeAsyncClient.captured = []
    pleroma.send_reply(STATUS, "Hello there", own_acct=OWN)
    pleroma_shim.send_reply(STATUS, "Hello there", own_acct=OWN)
    ok &= _diff("reply text-only",
                _status_post(legacy_captured, is_json=False),
                _status_post(_FakeAsyncClient.captured, is_json=True))

    # 2) reply with an image
    legacy_captured.clear(); _FakeAsyncClient.captured = []
    pleroma.send_reply(STATUS, "pic", own_acct=OWN, image_bytes=b"PNGDATA")
    pleroma_shim.send_reply(STATUS, "pic", own_acct=OWN, image_bytes=b"PNGDATA")
    ok &= _diff("reply with image",
                _status_post(legacy_captured, is_json=False),
                _status_post(_FakeAsyncClient.captured, is_json=True))

    # 3) top-level post (content_type must be text/markdown on both)
    legacy_captured.clear(); _FakeAsyncClient.captured = []
    pleroma.post_image_to_fediverse("a post", image_bytes=b"PNGDATA")
    pleroma_shim.post_image_to_fediverse("a post", image_bytes=b"PNGDATA")
    ok &= _diff("post_image_to_fediverse",
                _status_post(legacy_captured, is_json=False),
                _status_post(_FakeAsyncClient.captured, is_json=True))

    # 4) read-path return shapes
    legacy_captured.clear(); _FakeAsyncClient.captured = []
    own_l, own_s = pleroma.get_own_account(), pleroma_shim.get_own_account()
    ok &= _diff("get_own_account", {"acct": own_l.get("acct")}, {"acct": own_s.get("acct")})
    notif_l, notif_s = pleroma.get_notifications(), pleroma_shim.get_notifications()
    ok &= _diff("get_notifications len/type",
                {"len": len(notif_l), "type": notif_l[0]["type"]},
                {"len": len(notif_s), "type": notif_s[0]["type"]})

    print("\nPARITY: " + ("PASS ✅" if ok else "FAIL ❌"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
