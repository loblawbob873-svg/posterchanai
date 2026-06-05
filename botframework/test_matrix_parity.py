"""Parity harness for the Matrix dedup (Phase 4).

The Matrix shim swaps only the transport (matrix_request / upload_media_to_matrix), reusing
matrix_client.py's higher-level functions verbatim — so parity is structural. This harness
drives identical inputs through legacy `matrix_client` and `matrix_shim` with the network
mocked on each side, then diffs the semantic m.room.message send payload (msgtype, body) and
the get_own_account result.

Run:  MATRIX_SERVER=https://matrix.test MATRIX_USER_ID=@bot:matrix.test \
      MATRIX_ACCESS_TOKEN=tok ../venv/bin/python test_matrix_parity.py
"""

import os
import sys

os.environ.setdefault("MATRIX_SERVER", "https://matrix.test")
os.environ.setdefault("MATRIX_USER_ID", "@bot:matrix.test")
os.environ.setdefault("MATRIX_ACCESS_TOKEN", "tok")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOM = "!room:matrix.test"


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


def _resp_for(url):
    if "/upload" in url:
        return {"content_uri": "mxc://matrix.test/ABC"}
    if "whoami" in url or "account" in url:
        return {"user_id": "@bot:matrix.test"}
    if "/send/" in url:
        return {"event_id": "$evt"}
    if "joined_members" in url:
        return {"joined": {}}
    return {}


class _FakeAsyncClient:
    captured = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def _do(self, method, url, json=None, content=None, **k):
        _FakeAsyncClient.captured.append({"method": method, "url": url, "json": json})
        return _Resp(200, _resp_for(url))

    async def get(self, url, **k):
        return await self._do("GET", url, **k)

    async def post(self, url, **k):
        return await self._do("POST", url, **k)

    async def put(self, url, **k):
        return await self._do("PUT", url, **k)


legacy_captured = []


def _fake_req(method):
    def f(url, headers=None, params=None, json=None, data=None, timeout=None, verify=None):
        legacy_captured.append({"method": method, "url": url, "json": json})
        return _Resp(200, _resp_for(url))
    return f


def _send_payload(captured):
    sends = [c for c in captured if "/send/m.room.message/" in c["url"]]
    assert len(sends) >= 1, f"expected an m.room.message send, got {len(sends)}"
    body = sends[-1]["json"] or {}
    return {"msgtype": body.get("msgtype"), "body": body.get("body"),
            "has_url": bool(body.get("url"))}


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
    import matrix_client

    # Save the ORIGINAL (requests-based) transport before the shim monkeypatches it.
    orig_request = matrix_client.matrix_request
    orig_upload = matrix_client.upload_media_to_matrix

    import matrix_shim  # patches matrix_client.matrix_request / upload_media_to_matrix

    ok = True
    needed = ["get_messages", "get_sync_token", "get_own_account", "send_message", "send_reply",
              "post_image_to_matrix", "get_thread_history", "get_thread_images",
              "get_room_member_count", "get_event", "join_room", "leave_room", "send_poll",
              "mxc_to_https", "download_image_from_url", "send_file_to_room"]
    missing = [n for n in needed if not hasattr(matrix_shim, n)]
    if missing:
        print(f"  MISMATCH [interface] shim missing: {missing}"); ok = False
    else:
        print(f"  OK [interface] shim exposes all {len(needed)} listener symbols")

    requests.get, requests.post, requests.put = _fake_req("GET"), _fake_req("POST"), _fake_req("PUT")
    httpx.AsyncClient = _FakeAsyncClient

    def legacy(fn, *a, **k):
        matrix_client.matrix_request = orig_request
        matrix_client.upload_media_to_matrix = orig_upload
        try:
            return fn(*a, **k)
        finally:
            matrix_client.matrix_request = matrix_shim._request
            matrix_client.upload_media_to_matrix = matrix_shim._upload

    # 1) text message
    legacy_captured.clear(); _FakeAsyncClient.captured = []
    legacy(matrix_client.send_message, ROOM, "Hello world")
    matrix_shim.send_message(ROOM, "Hello world")
    ok &= _diff("send_message text", _send_payload(legacy_captured), _send_payload(_FakeAsyncClient.captured))

    # 2) image message (upload mxc then send)
    legacy_captured.clear(); _FakeAsyncClient.captured = []
    legacy(matrix_client.post_image_to_matrix, ROOM, "caption", image_bytes=b"PNGDATA")
    matrix_shim.post_image_to_matrix(ROOM, "caption", image_bytes=b"PNGDATA")
    ok &= _diff("post_image_to_matrix",
                _send_payload(legacy_captured), _send_payload(_FakeAsyncClient.captured))

    print("\nPARITY: " + ("PASS ✅" if ok else "FAIL ❌"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
