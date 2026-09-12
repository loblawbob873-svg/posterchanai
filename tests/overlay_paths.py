"""Where the PosterChanOS overlay's ebuilds are — resolved by GLOB, never by a pinned filename.

A Gentoo package's filename IS its version, and a config change that ships in `files/` only reaches
an installed machine if the ebuild is revision-bumped (`emerge -uDN @world` keys on version, so an
edited `files/wayfire.ini` under an unchanged `1.0.0` is never reinstalled — measured: a machine
running this OS still had `active_color = \\#12121aff` long after the repo said otherwise).

So revbumps must be cheap. Ten test modules had the filename typed into them, which made every
revbump a ten-file rename and is exactly the kind of friction that stops one happening at all.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "os/overlay"
SHELL_PKG = OVERLAY / "app-misc/posterchanos-shell"


def _one(pkg: Path, name: str) -> Path:
    found = sorted(pkg.glob(f"{name}-*.ebuild"))
    if len(found) != 1:
        raise AssertionError(f"expected exactly one {name} ebuild in {pkg}, found {found}")
    return found[0]


def shell_ebuild() -> Path:
    """app-misc/posterchanos-shell, whatever revision it is on today."""
    return _one(SHELL_PKG, "posterchanos-shell")


def shell_ebuild_text() -> str:
    return shell_ebuild().read_text()


def wayfire_ebuild() -> Path:
    """gui-wm/wayfire, whatever revision carries today's patches."""
    return _one(OVERLAY / "gui-wm/wayfire", "wayfire")


def wayfire_version() -> str:
    """e.g. '0.10.1-r2' — what a dependency on the patched compositor must name."""
    return wayfire_ebuild().stem[len("wayfire-"):]
