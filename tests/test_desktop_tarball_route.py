"""poster.place/desktop/PosterChan-linux-x64.tar.zst is the TARBALL, not the landing page.

os/gentoo.sh asks this URL first, so an install fetches the desktop from the host it already depends on. Measured
2026-10-27 in the from-scratch VM build: it answered 200 text/html (the /desktop page), because the route only knew
the AppImage/installer names and the rolling release carried the tarball under its versioned name alone. Every
install then depended on api.github.com to name the asset.

Both halves are checked: the route redirects that name (and the `linux-tar` alias) into the rolling release, and the
workflow puts a file of that name there -- after the immutable per-version release, which Portage pins.
"""
import asyncio
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_the_route_sends_the_installer_to_the_tarball():
    from app import main
    for asset in ("PosterChan-linux-x64.tar.zst", "linux-tar"):
        r = asyncio.run(main.desktop_asset(asset))
        loc = r.headers["location"]
        assert loc == main._DESKTOP_DL + "PosterChan-linux-x64.tar.zst", (asset, loc)
    # still an allowlist: anything else goes to the page, never off-site
    assert asyncio.run(main.desktop_asset("../../evil")).headers["location"] == "/desktop"


def test_the_rolling_release_carries_that_name_and_the_pinned_release_does_not():
    wf = yaml.safe_load((ROOT / ".github/workflows/desktop.yml").read_text())
    steps = wf["jobs"]["publish"]["steps"]
    names = [s.get("name", "") for s in steps]
    copy = next(i for i, s in enumerate(steps) if "cp " in s.get("run", "")
                and "artifacts/PosterChan-linux-x64.tar.zst" in s.get("run", ""))
    immutable = names.index("Publish the versioned tarball the Gentoo overlay pins")
    rolling = names.index("Publish rolling release")
    assert immutable < copy < rolling, (immutable, copy, rolling)
    assert steps[rolling]["with"]["files"].strip() == "artifacts/*"
    # The immutable release's pattern must not match the versionless copy.
    pinned = steps[immutable]["with"]["files"]
    pats = [p.strip() for p in pinned.splitlines() if p.strip()]
    rx = [re.compile("^" + re.escape(p).replace(r"\*", "[^/]*") + "$") for p in pats]
    assert not any(r.match("artifacts/PosterChan-linux-x64.tar.zst") for r in rx), pats
