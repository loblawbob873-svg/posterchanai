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


def test_the_page_promotes_posterfox_with_its_artwork_and_download():
    """PosterFox is PosterChanOS's default browser: the page has a section for it with its badge art,
    the art is one of the files the publisher uploads (a page naming an unpublished image shows a broken
    picture), and it links the portable download."""
    import re
    sec = re.search(r'<section class="band" id="posterfox".*?</section>', PAGE, re.S)
    assert sec, "no PosterFox section on the page"
    sec = sec.group(0)
    assert 'src="/site/posterfox.webp"' in sec and "alt=" in sec
    assert 'href="https://github.com/loblawbob873-svg/posterfox/releases"' in sec
    assert re.search(r"<h2[^>]*>.*PosterFox", sec, re.S)
    assert len(re.findall(r"<li>", sec)) >= 3, "the section lists what PosterFox does"
    assert '<h3><b>PosterFox</b> browser</h3>' in PAGE, "no PosterFox card in the features grid"
    published = {key: path for key, path, _ct, _cc in site.FILES}
    assert "site/posterfox.webp" in published and published["site/posterfox.webp"].is_file()
    from PIL import Image
    with Image.open(published["site/posterfox.webp"]) as im:
        assert im.format == "WEBP"


def test_every_site_image_the_page_uses_is_published():
    import re
    published = {key for key, _p, _ct, _cc in site.FILES}
    for src in re.findall(r'src="/(site/[^"]+)"', PAGE):
        assert src in published, f"the page shows {src} but the publisher never uploads it"


class FakeR2:
    def __init__(self):
        self.puts = []

    def req(self, method, key, **kw):
        if method == "PUT":
            self.puts.append(key)


def test_the_publisher_writes_only_the_page():
    r2 = FakeR2()
    site.publish(r2, log=lambda m: None)
    assert r2.puts == ["index.html", "site/posterchan-os-v2.webp", "site/posterchanos-desktop.webp", "site/posterfox.webp"]


@pytest.mark.parametrize("key", ["posterchanos.iso", "posterchanos.iso.sha256", "site/../posterchanos.iso", "other.html"])
def test_it_refuses_anything_that_is_not_the_page(key, tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"x")
    r2 = FakeR2()
    with pytest.raises(SystemExit):
        site.publish(r2, files=[(key, f, "text/plain", "no-cache")], log=lambda m: None)
    assert r2.puts == []
