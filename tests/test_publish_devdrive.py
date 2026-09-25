"""The devdrive.cloud ISO publisher, run against a fake YetiShare API (httpx.MockTransport).

What it must never do: delete the previous image before the new one is verified, send a file the
plan cannot hold, or put a key anywhere but the form body.
"""
import hashlib
import importlib.util
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("publish_devdrive", ROOT / "scripts/publish_devdrive.py")
pd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pd)

K1, K2 = "a" * 64, "b" * 64


class Fake:
    def __init__(self, *, max_upload=0, truncate=0, bad_hash=False):
        self.calls, self.deleted, self.max_upload = [], [], max_upload
        self.truncate, self.bad_hash = truncate, bad_hash
        self.files = [{"id": "11", "filename": "posterchan-live-20260913.iso"},
                      {"id": "12", "filename": "notes.txt"}]
        self.uploaded = b""

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.rsplit("/v2/", 1)[1]
        self.calls.append(path)
        assert K1 not in str(req.url) and K2 not in str(req.url), "a key in the URL"
        body = req.read()
        form = {}
        if b"upload_file" not in body:
            form = dict(httpx.QueryParams(body.decode()))
        ok = lambda d: httpx.Response(200, json={"_status": "success", "data": d})
        if path == "authorize":
            assert form == {"key1": K1, "key2": K2}
            return ok({"access_token": "T", "account_id": "7"})
        if path != "authorize" and b"upload_file" not in body:
            assert form.get("access_token") == "T" and form.get("account_id") == "7"
        if path == "account/package":
            return ok({"max_upload_size": str(self.max_upload), "can_upload": "1"})
        if path == "folder/listing":
            if form.get("parent_folder_id"):
                return ok({"files": self.files})
            return ok({"folders": [{"id": "5", "folderName": "PosterChanOS"}]})
        if path == "file/upload":
            self.uploaded = body
            size = len(ISO_BYTES) - self.truncate
            h = "0" * 32 if self.bad_hash else hashlib.md5(ISO_BYTES).hexdigest()
            self.files.append({"id": "99", "filename": "posterchan-live-20260925.iso"})
            return ok({"file_id": "99", "size": size, "hash": h, "url": "https://devdrive.cloud/abc"})
        if path == "file/info":
            return ok({"id": form["file_id"], "fileSize": len(ISO_BYTES) - self.truncate})
        if path == "file/delete":
            self.deleted.append(form["file_id"])
            return ok({})
        if path == "disable_access_token":
            return ok({})
        return httpx.Response(404, json={"_status": "error", "response": "no such call"})


ISO_BYTES = b"PosterChanOS" * 1000


@pytest.fixture
def iso(tmp_path):
    p = tmp_path / "posterchan-live-20260925.iso"
    p.write_bytes(ISO_BYTES)
    return p


def run(fake, iso):
    with httpx.Client(transport=httpx.MockTransport(fake)) as c:
        return pd.publish(c, iso, K1, K2, log=lambda *_: None)


def test_uploads_verifies_then_retires_only_the_older_iso(iso):
    fake = Fake()
    rec = run(fake, iso)
    assert rec["file_id"] == "99" and rec["url"] == "https://devdrive.cloud/abc"
    assert rec["sha256"] == hashlib.sha256(ISO_BYTES).hexdigest()
    assert fake.deleted == ["11"], "only the previous posterchan-live ISO goes; never the new one or another file"
    assert fake.calls.index("file/upload") < fake.calls.index("file/info") < fake.calls.index("file/delete")
    assert fake.calls[-1] == "disable_access_token"


@pytest.mark.parametrize("kw", [{"truncate": 5}, {"bad_hash": True}])
def test_a_bad_upload_leaves_the_previous_image_alone(iso, kw):
    fake = Fake(**kw)
    with pytest.raises(pd.Refused):
        run(fake, iso)
    assert fake.deleted == [], "the old ISO was deleted before the new one was verified"
    assert fake.calls[-1] == "disable_access_token"


def test_a_file_the_plan_cannot_hold_is_never_sent(iso):
    fake = Fake(max_upload=len(ISO_BYTES) - 1)
    with pytest.raises(pd.Refused, match="max_upload_size"):
        run(fake, iso)
    assert "file/upload" not in fake.calls and fake.deleted == []


def test_missing_keys_mean_not_configured_and_nothing_sent(iso, tmp_path, monkeypatch):
    monkeypatch.setenv("PC_DEVDRIVE_KEY1_FILE", str(tmp_path / "nope1"))
    monkeypatch.setenv("PC_DEVDRIVE_KEY2_FILE", str(tmp_path / "nope2"))
    assert pd.main(["x", str(iso)]) == 2


def test_publish_iso_runs_it_only_after_the_router_publish_and_only_when_configured():
    sh = (ROOT / "scripts/publish_iso.sh").read_text()
    step = sh.index("publish_devdrive.py")
    assert step > sh.index('echo "Published $PUBLISH_HOST:$PUBLISH_PATH'), "devdrive must run after the verified router publish"
    assert "devdrive.key2" in sh[step - 600:step + 200]
