"""The client's torrent calls must match the API's actual request shapes.

Every one of these was wrong on first write, and each failed in a way that told the user nothing:
the action endpoints take a torrent's POSITION (`num`), not its info hash, so pause/resume/remove
422'd; `/add` takes `torrent_url`, not `url`, so a .torrent link was silently ignored; and FastAPI's
422 `detail` is a list of objects, which the toast rendered as "[object Object]".

A static check on purpose. The behaviour lives in the browser against a live libtorrent session, and
the thing that actually broke was the CONTRACT between two files in this repo — which is exactly
what can be checked here.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "static" / "js" / "client" / "app.js").read_text()
API = (ROOT / "app" / "routers" / "torrent.py").read_text()


def _client_calls():
    """Every _torApi('/path', …) the client makes, with the body it sends."""
    return re.findall(r"_torApi\('(/[a-z]+)'\s*,\s*\{[^}]*?body:JSON\.stringify\(([^;]+?)\)\}\)", APP, re.S)


def test_action_endpoints_are_addressed_by_num_not_info_hash():
    assert "class TorrentActionRequest" in API and re.search(r"class TorrentActionRequest\(BaseModel\):\s*\n\s*num: int", API), \
        "the API's action model changed — this test is asserting the wrong contract"
    for path, body in _client_calls():
        if path in ("/pause", "/resume", "/remove"):
            assert "num:" in body, f"{path} is sent without `num`: {body.strip()[:90]}"
            assert "info_hash" not in body, (
                f"{path} is sent an info_hash; the endpoint takes a position and will 422")


def test_add_uses_the_fields_the_endpoint_declares():
    assert re.search(r"class AddTorrentRequest\(BaseModel\):\s*\n\s*magnet: str[^\n]*\n\s*torrent_url: str", API), \
        "the API's add model changed — this test is asserting the wrong contract"
    add = [b for p, b in _client_calls() if p == "/add"]
    assert add, "the client no longer calls /add"
    for body in add:
        stray = set(re.findall(r"\b(\w+)\s*:", body)) - {"magnet", "torrent_url"}
        assert not stray, f"/add is sent field(s) it does not declare: {sorted(stray)}"


def test_a_stale_position_is_never_reused_for_a_destructive_action():
    """`num` is an index into a list that reshuffles whenever a torrent is added or removed, and the
    view polls every two seconds. Remove-with-files against a stale index deletes someone else's
    download, so the hash must be resolved to a CURRENT number at the moment of the click."""
    assert "async function _torNum(hash)" in APP, "the hash→num resolver is gone"
    for path, body in _client_calls():
        if path in ("/pause", "/resume", "/remove"):
            assert "_torNum(" in body, (
                f"{path} uses a number that was not resolved at click time: {body.strip()[:90]}")


def test_validation_errors_are_flattened_for_the_toast():
    assert "Array.isArray(d)" in APP, (
        "a 422's `detail` is a list of objects; without flattening the toast says '[object Object]'")
