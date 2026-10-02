"""The iso.poster.place splash page: it downloads the ISO, reads the live checksum, and its publisher
can never write to the ISO's own keys (the page shares the R2 bucket with the download)."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "os/iso-site/index.html").read_text()
spec = importlib.util.spec_from_file_location("publish_iso_site", ROOT / "scripts/publish_iso_site.py")
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


def test_the_page_downloads_the_iso_and_reads_its_live_checksum():
    assert 'id="dl" href="/posterchanos.iso"' in PAGE
    assert "fetch('/posterchanos.iso.sha256'" in PAGE and "method:'HEAD'" in PAGE
    for _key, path, _ct, _cc in site.FILES:
        assert path.is_file(), f"{path} is missing"


class FakeR2:
    def __init__(self):
        self.puts = []

    def req(self, method, key, **kw):
        if method == "PUT":
            self.puts.append(key)


def test_the_publisher_writes_only_the_page():
    r2 = FakeR2()
    site.publish(r2, log=lambda m: None)
    assert r2.puts == ["index.html", "site/posterchan-os.webp", "site/posterchanos-desktop.webp"]


@pytest.mark.parametrize("key", ["posterchanos.iso", "posterchanos.iso.sha256", "site/../posterchanos.iso", "other.html"])
def test_it_refuses_anything_that_is_not_the_page(key, tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"x")
    r2 = FakeR2()
    with pytest.raises(SystemExit):
        site.publish(r2, files=[(key, f, "text/plain", "no-cache")], log=lambda m: None)
    assert r2.puts == []
