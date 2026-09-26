"""scripts/publish_r2.py: the public ISO (iso.poster.place is an R2 custom domain) is replaced only by a
verified image, and a failure at any step leaves the published one exactly as it was.

The signer is checked against AWS's own published Signature V4 test vector, so a canonicalisation
mistake is a test failure here rather than a 403 from R2 in the middle of a release.
"""
import datetime as dt
import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("publish_r2", ROOT / "scripts" / "publish_r2.py")
r2mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r2mod)


def test_signature_matches_the_aws_get_vanilla_vector():
    out = r2mod.sign("GET", "https://example.amazonaws.com/", {"host": "example.amazonaws.com"},
                     hashlib.sha256(b"").hexdigest(), key_id="AKIDEXAMPLE",
                     secret="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
                     now=dt.datetime(2015, 8, 30, 12, 36, 0, tzinfo=dt.timezone.utc),
                     region="us-east-1", service="service")
    assert out["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31")


class Resp:
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}


class FakeR2:
    """Just enough S3 to run publish(): multipart upload, HEAD, server-side copy, DELETE."""

    bucket = "posterchan"

    def __init__(self, fail_part=None, truncate=False):
        self.objects, self.meta, self.uploads = {}, {}, {}
        self.fail_part, self.truncate = fail_part, truncate

    def req(self, method, key, query="", body=b"", headers=None, ok=(200,), timeout=0):
        headers = headers or {}
        q = dict(p.split("=", 1) if "=" in p else (p, "") for p in query.split("&") if p)
        if method == "POST" and "uploads" in q:
            uid = f"u{len(self.uploads) + 1}"
            self.uploads[uid] = {}
            return Resp(content=f"<InitiateMultipartUploadResult><UploadId>{uid}</UploadId>"
                                f"</InitiateMultipartUploadResult>".encode())
        if method == "PUT" and "partNumber" in q:
            n = int(q["partNumber"])
            if n == self.fail_part:
                raise r2mod.Refused(f"PUT part {n}: HTTP 500")
            self.uploads[q["uploadId"]][n] = body
            return Resp(headers={"etag": f'"{n}"'})
        if method == "POST" and "uploadId" in q:
            parts = self.uploads.pop(q["uploadId"])
            data = b"".join(parts[n] for n in sorted(parts))
            self.objects[key] = data[:-1] if self.truncate else data
            return Resp()
        if method == "DELETE" and "uploadId" in q:
            self.uploads.pop(q["uploadId"], None)
            return Resp(204)
        if method == "HEAD":
            if key not in self.objects:
                raise r2mod.Refused(f"HEAD {key}: HTTP 404")
            h = {"content-length": str(len(self.objects[key]))}
            h.update(self.meta.get(key, {}))
            return Resp(headers=h)
        if method == "PUT" and "x-amz-copy-source" in headers:
            src = headers["x-amz-copy-source"].split("/", 2)[2]
            self.objects[key] = self.objects[src]
            self.meta[key] = {"x-amz-meta-sha256": headers["x-amz-meta-sha256"]}
            return Resp()
        if method == "PUT":
            self.objects[key] = body
            return Resp()
        if method == "DELETE":
            self.objects.pop(key, None)
            return Resp(204)
        raise AssertionError(f"unexpected {method} {key}?{query}")


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setattr(r2mod, "PART", 1000)          # several parts from a small file
    p = tmp_path / "posterchan-live-20260925.iso"
    p.write_bytes(bytes(range(256)) * 20)             # 5120 bytes = 6 parts
    return p


OLD = b"the image people are downloading today"


def test_a_verified_image_replaces_the_published_one(iso):
    r2 = FakeR2()
    r2.objects["posterchanos.iso"] = OLD
    rec = r2mod.publish(r2, iso, log=lambda *_: None, sleep=lambda *_: None)
    digest = hashlib.sha256(iso.read_bytes()).hexdigest()
    assert r2.objects["posterchanos.iso"] == iso.read_bytes()
    assert r2.meta["posterchanos.iso"]["x-amz-meta-sha256"] == digest == rec["sha256"]
    assert r2.objects["posterchanos.iso.sha256"] == f"{digest}  posterchanos.iso\n".encode()
    assert not [k for k in r2.objects if k.startswith(".uploading/")], "the staging copy was left behind"
    assert not r2.uploads


def test_a_failed_part_leaves_the_published_image_and_aborts_the_upload(iso):
    r2 = FakeR2(fail_part=3)
    r2.objects["posterchanos.iso"] = OLD
    with pytest.raises(r2mod.Refused):
        r2mod.publish(r2, iso, log=lambda *_: None, sleep=lambda *_: None)
    assert r2.objects == {"posterchanos.iso": OLD}
    assert not r2.uploads, "an abandoned multipart upload keeps billing storage"


def test_a_staged_object_of_the_wrong_size_never_replaces_anything(iso):
    r2 = FakeR2(truncate=True)
    r2.objects["posterchanos.iso"] = OLD
    with pytest.raises(r2mod.Refused, match="bytes"):
        r2mod.publish(r2, iso, log=lambda *_: None, sleep=lambda *_: None)
    assert r2.objects == {"posterchanos.iso": OLD}


def test_an_image_past_the_copy_limit_is_refused_before_uploading(iso, monkeypatch):
    monkeypatch.setattr(r2mod, "COPY_LIMIT", 100)
    r2 = FakeR2()
    r2.objects["posterchanos.iso"] = OLD
    with pytest.raises(r2mod.Refused, match="5 GiB"):
        r2mod.publish(r2, iso, log=lambda *_: None, sleep=lambda *_: None)
    assert r2.objects == {"posterchanos.iso": OLD} and not r2.uploads


def test_no_credentials_sends_nothing(iso, tmp_path, monkeypatch):
    monkeypatch.setattr(r2mod, "CONF", tmp_path / "nothing-here")
    monkeypatch.setattr(r2mod.urllib.request, "urlopen", lambda *a, **k: pytest.fail("contacted R2 without credentials"))
    assert r2mod.main(["publish_r2.py", str(iso)]) == 2


# ----------------------------------------------------------------- scripts/publish_iso.sh, RUN for real

def _publish_iso(tmp_path, iso, *, publisher_rc=0, sidecar=None, size=None, curl_fails=False, publisher=True):
    """The shipped publish_iso.sh beside a fake publish_r2.py, with `curl` stubbed to answer as the
    public URL would."""
    import os
    import shutil
    import subprocess
    scripts, bin_ = tmp_path / "scripts", tmp_path / "bin"
    scripts.mkdir()
    bin_.mkdir()
    shutil.copy(ROOT / "scripts" / "publish_iso.sh", scripts / "publish_iso.sh")
    calls = tmp_path / "published"
    if publisher:
        (scripts / "publish_r2.py").write_text(
            f"import sys; open({str(calls)!r}, 'w').write(sys.argv[1]); sys.exit({publisher_rc})\n")
    data = iso.read_bytes()
    sha = sidecar if sidecar is not None else hashlib.sha256(data).hexdigest()
    n = size if size is not None else len(data)
    (bin_ / "curl").write_text(
        "#!/bin/bash\n"
        + ("exit 22\n" if curl_fails else
           f'if [[ " $* " == *" -fsSI "* ]]; then printf "HTTP/2 200\\r\\ncontent-length: {n}\\r\\n\\r\\n";\n'
           f'else printf "%s  posterchanos.iso\\n" "{sha}"; fi\n'))
    (bin_ / "curl").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}")
    out = subprocess.run(["bash", str(scripts / "publish_iso.sh"), str(iso)], env=env,
                         capture_output=True, text=True, timeout=60)
    return out, (calls.read_text() if calls.exists() else None)


def test_publish_iso_publishes_to_r2_and_checks_what_the_internet_gets(tmp_path, iso):
    out, handed = _publish_iso(tmp_path, iso)
    assert out.returncode == 0, out.stderr
    assert handed == str(iso), "publish_r2.py was not handed the image"
    assert "Published https://iso.poster.place/posterchanos.iso" in out.stdout


def test_publish_iso_does_not_touch_router_lan():
    src = (ROOT / "scripts" / "publish_iso.sh").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    for gone in ("ssh ", "scp ", "router.lan", "/srv/iso", "devdrive"):
        assert gone not in code, f"publish_iso.sh still reaches for {gone!r}"


@pytest.mark.parametrize("rc", [1, 2])
def test_a_failed_r2_publish_is_reported_as_nothing_published(tmp_path, iso, rc):
    out, _ = _publish_iso(tmp_path, iso, publisher_rc=rc)
    assert out.returncode == rc
    assert "still serves the previous image" in out.stderr


@pytest.mark.parametrize("served", [{"sidecar": "0" * 64}, {"size": 7}])
def test_a_public_url_serving_another_image_is_a_failure(tmp_path, iso, served):
    out, _ = _publish_iso(tmp_path, iso, **served)
    assert out.returncode == 1 and "serves a different image" in out.stderr


def test_an_unreadable_public_url_says_the_upload_happened(tmp_path, iso):
    out, _ = _publish_iso(tmp_path, iso, curl_fails=True)
    assert out.returncode == 1
    assert "Published to R2, but" in out.stderr and "could not be read back" in out.stderr


def test_a_missing_r2_publisher_sends_nothing(tmp_path, iso):
    out, handed = _publish_iso(tmp_path, iso, publisher=False)
    assert out.returncode == 2 and handed is None and "publisher is missing" in out.stderr


def test_the_packaged_publisher_ships_its_r2_half():
    """PosterChanOS installs publish_iso.sh under libexec; it resolves publish_r2.py beside itself."""
    overlay = (ROOT / "scripts" / "publish_overlay.sh").read_text()
    from tests.overlay_paths import shell_ebuild
    ebuild = shell_ebuild().read_text()
    assert "scripts/publish_r2.py" in overlay
    assert 'doexe "${FILESDIR}/publish_r2.py"' in ebuild
