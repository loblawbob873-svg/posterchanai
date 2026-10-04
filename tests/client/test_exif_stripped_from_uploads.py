"""Camera metadata never leaves the device in an uploaded image ("make sure we remove exif data on all
blossom uploads").

A phone photo carries where it was taken (GPS), when and on what. Uploads only lost it when they happened to
be re-encoded (big photos), so every small one went out with its location. static/js/client/exifstrip.js
strips it LOSSLESSLY -- the pixels are copied through untouched. These run the SHIPPED stripper under node
against real images Pillow writes with real metadata, then decode the result with Pillow:

  gone        no EXIF, no GPS, no XMP/IPTC/comments, no PNG text chunks, no WebP EXIF/XMP
  lossless    the stripped JPEG/PNG/WebP decodes to exactly the same pixels
  trailer     data after the JPEG's end (a second preview image with its own EXIF, a vendor trailer) goes
  rotation    a rotated photo's Orientation is REPORTED, because removing EXIF removes it (the caller
              re-draws such a photo upright instead of shipping it sideways)
  untouched   an image with nothing to remove comes back as the very same bytes
"""
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image, PngImagePlugin  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
STRIPPER = ROOT / "static/js/client/exifstrip.js"
NODE = shutil.which("node")
GPS_LAT = 51.4778          # Greenwich: the coordinate that must not survive


def _exif(orientation=1):
    ex = Image.Exif()
    ex[0x0112] = orientation                       # Orientation
    ex[0x010F] = "PhoneMaker"                      # Make
    ex[0x0110] = "PhoneModel X"                    # Model
    ex[0x9003] = "2026:10:04 12:34:56"              # DateTimeOriginal (in the Exif IFD via get_ifd below)
    gps = ex.get_ifd(0x8825)
    gps[1] = "N"; gps[2] = (51.0, 28.0, 40.08)      # GPSLatitudeRef / GPSLatitude
    gps[3] = "W"; gps[4] = (0.0, 0.0, 5.3)
    return ex


def _img(mode="RGB"):
    im = Image.new(mode, (64, 48), (200, 30, 90) if mode == "RGB" else (200, 30, 90, 255))
    for x in range(64):
        for y in range(48):
            im.putpixel((x, y), ((x * 4) % 256, (y * 5) % 256, (x * y) % 256) + ((255,) if mode == "RGBA" else ()))
    return im


def jpeg(orientation=1, comment=True, xmp=True):
    buf = io.BytesIO()
    kw = {"exif": _exif(orientation).tobytes(), "quality": 92}
    if comment:
        kw["comment"] = b"taken at home"
    if xmp:
        kw["xmp"] = b"<x:xmpmeta><rdf:Description exif:GPSLatitude='51,28.668N'/></x:xmpmeta>"
    _img().save(buf, "JPEG", **kw)
    return buf.getvalue()


def png():
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "taken at home")
    info.add_itxt("XML:com.adobe.xmp", "<x:xmpmeta>GPSLatitude 51,28.668N</x:xmpmeta>")
    buf = io.BytesIO()
    _img("RGBA").save(buf, "PNG", pnginfo=info, exif=_exif().tobytes())
    return buf.getvalue()


def webp():
    buf = io.BytesIO()
    _img().save(buf, "WEBP", lossless=True, exif=_exif().tobytes(), xmp=b"<x:xmpmeta>GPS</x:xmpmeta>")
    return buf.getvalue()


RUN = r"""
const s = require(process.argv[1]); const fs = require('fs');
const r = s.strip(new Uint8Array(fs.readFileSync(0)));
process.stdout.write(JSON.stringify({format: r.format, changed: r.changed, orientation: r.orientation,
  hex: r.bytes ? Buffer.from(r.bytes).toString('hex') : null}));
"""


def strip(data: bytes) -> dict:
    p = subprocess.run([NODE, "-e", RUN, str(STRIPPER)], input=data, capture_output=True, timeout=30)
    assert p.returncode == 0, p.stderr.decode()
    out = json.loads(p.stdout)
    out["bytes"] = bytes.fromhex(out.pop("hex")) if out["hex"] is not None else None
    return out


def pixels(data):
    with Image.open(io.BytesIO(data)) as im:
        return im.convert("RGBA").tobytes(), im.size


def no_metadata(data):
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        exif = im.getexif()
        return (not dict(exif) and not exif.get_ifd(0x8825) and not im.info.get("exif")
                and not im.info.get("xmp") and not im.info.get("comment") and "Comment" not in im.info
                and "XML:com.adobe.xmp" not in im.info)


pytestmark = pytest.mark.skipif(not NODE, reason="node required")


def test_a_jpeg_loses_exif_gps_xmp_and_comment_and_keeps_every_pixel():
    src = jpeg()
    with Image.open(io.BytesIO(src)) as im:
        assert im.getexif().get_ifd(0x8825), "the fixture must carry a GPS block"
    r = strip(src)
    assert r["format"] == "jpeg" and r["changed"] and r["orientation"] == 1
    assert no_metadata(r["bytes"]), "metadata survived"
    assert b"Exif" not in r["bytes"] and b"PhoneModel" not in r["bytes"] and b"taken at home" not in r["bytes"]
    assert pixels(r["bytes"]) == pixels(src), "stripping changed the picture"


def test_a_rotated_photo_reports_its_orientation():
    r = strip(jpeg(orientation=6))
    assert r["orientation"] == 6, "a sideways photo must be re-drawn upright by the caller, so it must be told"
    assert no_metadata(r["bytes"])


def test_whatever_is_appended_after_the_jpeg_ends_is_cut_off():
    """Phones append a second (preview) JPEG with its OWN EXIF after the first image's end, and vendors
    append trailers -- both can carry the location the first image's EXIF no longer does."""
    src = jpeg() + jpeg() + b"SEFHtrailer-with-location"
    r = strip(src)
    assert b"Exif" not in r["bytes"] and b"SEFH" not in r["bytes"] and r["bytes"].count(b"\xff\xd8") == 1
    assert pixels(r["bytes"]) == pixels(jpeg())


def test_a_png_loses_exif_and_text_chunks_and_keeps_alpha_and_pixels():
    src = png()
    assert not no_metadata(src) and b"eXIf" in src and b"iTXt" in src, "the fixture must carry metadata"
    r = strip(src)
    assert r["format"] == "png" and r["changed"]
    for chunk in (b"eXIf", b"tEXt", b"iTXt", b"zTXt"):
        assert chunk not in r["bytes"], chunk
    assert no_metadata(r["bytes"]) and pixels(r["bytes"]) == pixels(src)


def test_a_webp_loses_exif_and_xmp_and_still_decodes_the_same():
    src = webp()
    assert not no_metadata(src) and b"EXIF" in src, "the fixture must carry metadata"
    r = strip(src)
    assert r["format"] == "webp" and r["changed"]
    assert b"EXIF" not in r["bytes"] and b"XMP " not in r["bytes"]
    assert no_metadata(r["bytes"]) and pixels(r["bytes"]) == pixels(src)


def test_an_image_with_nothing_to_remove_is_the_same_bytes():
    buf = io.BytesIO()
    _img().save(buf, "PNG")
    r = strip(buf.getvalue())
    assert r["changed"] is False and r["bytes"] == buf.getvalue()


def test_what_it_cannot_parse_it_says_so_instead_of_guessing():
    for data in (b"GIF89a....", b"not an image at all", jpeg()[:200]):
        r = strip(data)
        assert r["bytes"] is None, "an unparseable or truncated file must not be 'cleaned' into garbage"
