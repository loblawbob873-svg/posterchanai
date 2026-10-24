"""An Android 8 phone is given the APK it can install.

Reported 2026-10-24: one person "can't install the apk" (ARMv7-A, armeabi-v7a). The APK carries armeabi-v7a, but
the file /apk serves is signed with APK Signature Scheme v3 ONLY -- it carries the signing-key rotation lineage,
which Android 8/8.1 cannot verify (docs/ZAPSTORE_SIGNING_RECOVERY.md) -- so an 8.x phone refuses it as an invalid
package. CI already builds a second artifact for exactly that (posterchan-android8-reinstall.apk, v1+v2+v3), and
nothing on poster.place ever handed it out.

The shipped route, real requests: an Android 8 User-Agent asking /apk is redirected to /apk/android8, which serves
the Android 8 bytes; Android 9+, desktops and the in-app updater of a newer phone get the main APK, unchanged; the
redirect may not be cached for everybody. And the shipped mirror script (stub curl) keeps that file current without
ever letting it stop the main APK refreshing.
"""
import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]

UA8 = "Mozilla/5.0 (Linux; Android 8.1.0; SM-J260G Build/OPM1.171019.026; wv) AppleWebKit/537.36 Chrome/113 Mobile"
UA80 = "Mozilla/5.0 (Linux; Android 8; Moto E) AppleWebKit/537.36 Chrome/99 Mobile"
UA9 = "Mozilla/5.0 (Linux; Android 9; Nokia 2.2) AppleWebKit/537.36 Chrome/120 Mobile"
UA14 = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/130 Mobile"
UA_DESKTOP = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130"


def _apk(path, build, marker):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("assets/public/index.html", "<script>window.__PC_APP_BUILD__ = %d;</script>" % build)
        z.writestr("marker", marker)
    return path


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app.main as m
    main = _apk(tmp_path / "posterchan.apk", 2600, "MAIN")
    eight = _apk(tmp_path / "posterchan-android8-reinstall.apk", 2600, "EIGHT")
    monkeypatch.setattr(m, "_APK_LOCAL_PATH", str(main))
    monkeypatch.setattr(m, "_APK_ANDROID8_PATH", str(eight))
    return TestClient(m.app)


def _marker(body):
    import io
    return zipfile.ZipFile(io.BytesIO(body)).read("marker").decode()


def test_an_android_8_phone_is_sent_to_the_apk_it_can_install(client):
    for ua in (UA8, UA80):
        r = client.get("/apk", headers={"user-agent": ua}, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/apk/android8", (ua, r.status_code, r.headers)
        assert "no-store" in r.headers.get("cache-control", ""), "a CDN must not hand this redirect to everybody"
        got = client.get("/apk", headers={"user-agent": ua})
        assert got.status_code == 200 and _marker(got.content) == "EIGHT", ua
        assert got.headers["content-type"] == "application/vnd.android.package-archive"


def test_every_other_device_still_gets_the_main_apk(client):
    for ua in (UA9, UA14, UA_DESKTOP, "", "Mozilla/5.0 (Linux; Android 10; K) Chrome/130 Mobile"):
        r = client.get("/apk", headers={"user-agent": ua}, follow_redirects=False)
        assert r.status_code == 200 and _marker(r.content) == "MAIN", (ua, r.status_code)


UA7 = "Mozilla/5.0 (Linux; Android 7.1.2; Redmi 4X) AppleWebKit/537.36 Chrome/90 Mobile"
UA6 = "Mozilla/5.0 (Linux; Android 6.0; Lenovo K33) AppleWebKit/537.36 Chrome/80 Mobile"


def test_a_phone_too_old_for_the_app_is_told_so_instead_of_handed_a_file_that_cannot_install(client):
    for ua, n in ((UA7, 7), (UA6, 6)):
        r = client.get("/apk", headers={"user-agent": ua}, follow_redirects=False)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html"), (ua, r.headers)
        assert "Android 8.0 or newer" in r.text and ("Android %d" % n) in r.text and 'href="/client"' in r.text
        assert "no-store" in r.headers.get("cache-control", "")
        forced = client.get("/apk?force=1", headers={"user-agent": ua})
        assert _marker(forced.content) == "MAIN", "the page's own 'download anyway' link must still download"


def test_android8_falls_back_to_the_release_when_the_mirror_has_none(client, monkeypatch, tmp_path):
    import app.main as m
    monkeypatch.setattr(m, "_APK_ANDROID8_PATH", str(tmp_path / "absent.apk"))
    r = client.get("/apk/android8", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].endswith("/apk-latest/posterchan-android8-reinstall.apk")


def _refresh(tmp_path, fail8):
    """Run the shipped scripts/refresh_apk.sh with a stub curl that 'downloads' from a fake release."""
    rel = tmp_path / "release"
    rel.mkdir()
    _apk(rel / "posterchan.apk", 2601, "MAIN")
    _apk(rel / "posterchan-android8-reinstall.apk", 2601, "EIGHT")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "curl").write_text(
        '#!/bin/bash\nout=""; url=""\nwhile [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2;; '
        '--retry|--max-time) shift 2;; -*) shift;; *) url="$1"; shift;; esac; done\n'
        'name=$(basename "$url")\n'
        + ('[ "$name" = posterchan-android8-reinstall.apk ] && exit 22\n' if fail8 else '')
        + 'cp "%s/$name" "$out"\n' % rel)
    (bin_ / "curl").chmod(0o755)
    dest = tmp_path / "dest"
    env = dict(os.environ, PATH="%s:%s" % (bin_, os.environ["PATH"]), POSTERCHAN_APK_DIR=str(dest))
    r = subprocess.run(["bash", str(ROOT / "scripts/refresh_apk.sh")], env=env, capture_output=True, text=True, timeout=60)
    return r, dest


def test_the_mirror_keeps_the_android8_apk_current(tmp_path):
    r, dest = _refresh(tmp_path, fail8=False)
    assert r.returncode == 0, r.stderr
    assert zipfile.ZipFile(dest / "posterchan-android8-reinstall.apk").read("marker") == b"EIGHT"
    assert zipfile.ZipFile(dest / "posterchan.apk").read("marker") == b"MAIN"


def test_a_missing_android8_asset_never_stops_the_main_apk_refreshing(tmp_path):
    r, dest = _refresh(tmp_path, fail8=True)
    assert r.returncode == 0, r.stderr
    assert zipfile.ZipFile(dest / "posterchan.apk").read("marker") == b"MAIN"
    assert not (dest / "posterchan-android8-reinstall.apk").exists()
    assert not [p for p in dest.iterdir() if ".apk." in p.name], "a temp file was left behind"
