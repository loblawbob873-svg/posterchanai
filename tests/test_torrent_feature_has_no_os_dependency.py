"""The torrent feature needs no OS package (the owner, 2026-10-23: "we should not have any os-level deps for torrent
feature since we made it all python and native").

libtorrent is the PyPI wheel -- self-contained, matched to the venv's Python. A distro build (python3-libtorrent,
libtorrent-rasterbar) tracks the SYSTEM Python, so it stops importing when that moves past the venv's; nas hit
exactly that. So: the requirements carry the wheel, no install instruction names an OS libtorrent, and the engine
never reaches into the system site-packages for one.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OS_PACKAGES = re.compile(r"python3?-libtorrent|libtorrent-rasterbar")


def test_the_wheel_is_in_both_requirement_sets_at_a_current_version():
    for name in ("requirements.txt", "requirements-nostr.txt"):
        pins = re.findall(r"^libtorrent>=([\d.]+)", (ROOT / name).read_text(), re.M)
        assert pins, f"{name} does not install libtorrent"
        assert tuple(int(x) for x in pins[0].split(".")) >= (2, 1, 1), (name, pins)


def test_no_install_path_asks_for_an_os_libtorrent():
    offenders = []
    candidates = [ROOT / "setup.sh", ROOT / "install.sh", *sorted((ROOT / "os/overlay").rglob("*.ebuild"))]
    for path in candidates:
        if not path.exists():
            continue
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            code = line.split("#", 1)[0] if path.suffix != ".sh" or not line.lstrip().startswith("echo") else line
            if OS_PACKAGES.search(code):
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not offenders, offenders


def test_the_engine_never_imports_libtorrent_from_the_system_site_packages():
    src = (ROOT / "app/services/libtorrent_service.py").read_text()
    assert "sys.path.insert" not in src and "/usr/lib/python3" not in src, \
        "the engine adds the system site-packages to sys.path again"
    assert not OS_PACKAGES.search(src), "the engine tells people to install an OS libtorrent"
