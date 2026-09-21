#!/usr/bin/env python3
"""Pack the PosterChan Cyberpunk Firefox theme into an .xpi — standard library only.

    python3 os/firefox-theme/build.py            → os/firefox-theme/dist/posterchan-cyberpunk-<ver>.xpi
    python3 os/firefox-theme/build.py --signed X  → exit 0 if X carries a Mozilla signature, else 1

THE .xpi THIS BUILDS IS UNSIGNED, AND RELEASE FIREFOX WILL NOT INSTALL IT. Measured on the laptop
(Firefox 156, firefox-bin): the unsigned theme dropped into a fresh profile's extensions/ and into a
copy of Firefox's distribution/extensions/ was discarded without a word — no entry in
extensions.json at all — while a SIGNED theme placed the same way installed at once. Release builds
ignore `xpinstall.signatures.required`; themes are a signed type. So what PosterChanOS ships is the
copy addons.mozilla.org signed (unlisted — `.github/workflows/firefox-theme.yml`), and
scripts/publish_overlay.sh refuses to put anything without a signature into the overlay.

Deterministic: fixed member order and timestamps, so the same sources give the same bytes and a
rebuild does not look like a new version.
"""
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ["manifest.json", "images/logo-banner.png", "images/icon-48.png", "images/icon-96.png",
         "images/icon-128.png"]
EPOCH = (2026, 1, 1, 0, 0, 0)


def version():
    return json.load(open(os.path.join(HERE, "manifest.json")))["version"]


def xpi_name(ver=None):
    return "posterchan-cyberpunk-%s.xpi" % (ver or version())


def build(out_dir=None):
    out_dir = out_dir or os.path.join(HERE, "dist")
    os.makedirs(out_dir, exist_ok=True)
    m = json.load(open(os.path.join(HERE, "manifest.json")))
    # Every file the manifest names must be packed, or Firefox shows a theme with holes in it.
    named = set(m.get("icons", {}).values()) | set(m["theme"].get("images", {}).values())
    missing = sorted(named - set(FILES))
    if missing:
        raise SystemExit("manifest names files the package does not ship: " + ", ".join(missing))
    out = os.path.join(out_dir, xpi_name(m["version"]))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in FILES:
            info = zipfile.ZipInfo(rel, EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(os.path.join(HERE, rel), "rb") as fh:
                z.writestr(info, fh.read())
    return out


def is_signed(path):
    """A Mozilla-signed add-on carries its signature in META-INF (PKCS#7 and/or COSE)."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
    except (OSError, zipfile.BadZipFile):
        return False
    return "META-INF/mozilla.rsa" in names or "META-INF/cose.sig" in names


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--signed":
        sys.exit(0 if is_signed(sys.argv[2]) else 1)
    if len(sys.argv) == 2 and sys.argv[1] == "--name":
        print(xpi_name())
        sys.exit(0)
    print(build(sys.argv[1] if len(sys.argv) > 1 else None))
