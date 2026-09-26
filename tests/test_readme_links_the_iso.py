"""The README tells people where to download PosterChanOS — and it is the address the ISO is published to.

Asked 2026-09-25: "where is the ISO mention on the readme?" There was none. The README described the
OS at length and never said where to get it; the only mention of iso.poster.place was in
docs/RELEASE.md, which is the maintainer's publishing checklist. The URL is read from publish_iso.sh
rather than typed here, so moving the download moves (or breaks) this test instead of leaving the
README pointing at an old address.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _published_url():
    m = re.search(r'PUBLIC_URL="\$\{PC_ISO_PUBLIC_URL:-([^}"]+)\}"', (ROOT / "scripts/publish_iso.sh").read_text())
    assert m, "publish_iso.sh no longer declares its default public URL"
    return m.group(1)


def _posterchanos_section():
    text = (ROOT / "README.md").read_text()
    start = text.index("### PosterChanOS")
    end = text.index("\n### ", start + 1)
    return text[start:end]


def test_the_posterchanos_section_links_the_published_iso_and_its_checksum():
    url = _published_url()
    section = _posterchanos_section()
    assert f"({url})" in section, f"README's PosterChanOS section does not link {url}"
    assert f"({url}.sha256)" in section, "the checksum published beside the ISO is not linked"
