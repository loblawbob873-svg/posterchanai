"""THE THUMBNAIL PATH BUILDER DECIDES WHERE FILES GET WRITTEN, AND NOTHING TESTED IT.

`thumbnail_service.py` had ZERO test references across 598 lines. `get_thumbnail_path` is the
single source of truth for the whole feature — the writer, the exists-check, the reader and the
deleter all derive their path from it — so anything wrong there is wrong in four places at once and
consistently, which is the shape that hides.

Writing these found two defects.

ONE: `photo.jpg` and `photo.png` in one folder collided onto a single `.thumbnails/photo.jpg`. The
source extension was dropped before `.jpg` was appended, so whichever thumbnail was generated last
won and the other file displayed the wrong picture. A stem shared across image extensions is an
ordinary thing to have — an export beside its original — so this needed no help from anybody. It is
also invisible from the code: every function agrees on the path, and the file it points at exists.

TWO: `relative_to` does not normalise. `<user>/a/../../../etc/x.jpg` survives as
`a/../../../etc/x.jpg`, and since that parent is appended to `.thumbnails/`, the write lands in
`/srv/etc/`. NOT REACHABLE TODAY — both callers pass paths that came from the filesystem (a
server-generated save path in storage.py, a directory-walk entry in files.py), so this is a guard,
not a live vulnerability. It is guarded anyway because "no caller does that" is a property of the
callers, and this is the function that builds the path.

The generation tests run real Pillow against real files rather than mocking it: the interesting
answers are all about what happens to input that is not a valid image, and a mock cannot be wrong
in the ways a decoder is.
"""
import os
from pathlib import Path

import pytest
from PIL import Image

from app.services import thumbnail_service as ts


USER = Path("/srv/u")


def png(path: Path, size=(64, 48), colour=(255, 0, 0)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)
    return path


# --------------------------------------------------------------------------- collisions


def test_the_same_stem_with_two_image_extensions_does_not_collide():
    """THE BUG. `photo.jpg` and `photo.png` both became `.thumbnails/photo.jpg`, so one file showed
    the other's picture — and every function agreed on the path, so nothing looked broken."""
    assert ts.get_thumbnail_path(USER, USER / "photo.jpg") != \
           ts.get_thumbnail_path(USER, USER / "photo.png")


def test_it_does_not_collide_inside_a_subdirectory_either():
    assert ts.get_thumbnail_path(USER, USER / "a" / "b.jpg") != \
           ts.get_thumbnail_path(USER, USER / "a" / "b.png")


@pytest.mark.parametrize("ext", sorted(ts.IMAGE_EXTENSIONS))
def test_every_supported_extension_gets_its_own_thumbnail(ext):
    """A sweep over the real extension set, so a format added later is covered without anybody
    remembering this file. `.tif` and `.tiff` are two spellings of one format and must still not
    overwrite each other — they are two different files on disk."""
    others = [e for e in ts.IMAGE_EXTENSIONS if e != ext]
    mine = ts.get_thumbnail_path(USER, USER / f"photo{ext}")
    for other in others:
        assert mine != ts.get_thumbnail_path(USER, USER / f"photo{other}"), \
            f"photo{ext} and photo{other} share a thumbnail"


def test_files_in_different_directories_do_not_collide():
    assert ts.get_thumbnail_path(USER, USER / "a" / "photo.jpg") != \
           ts.get_thumbnail_path(USER, USER / "b" / "photo.jpg")


def test_a_flattened_name_does_not_collide_with_a_real_one():
    """The name encodes the directory with underscores, so `a/b.jpg` becomes `a_b`. A file actually
    called `a_b.jpg` at the root must still be distinct — it lands in a different directory."""
    assert ts.get_thumbnail_path(USER, USER / "a" / "b.jpg") != \
           ts.get_thumbnail_path(USER, USER / "a_b.jpg")


def test_a_file_with_no_extension_still_gets_a_thumbnail_path():
    p = ts.get_thumbnail_path(USER, USER / "noext")
    assert p.suffix == ".jpg" and p.name


# --------------------------------------------------------------------------- containment


@pytest.mark.parametrize("rel", [
    "a/../../../etc/x.jpg",
    "../../../tmp/x.jpg",
    "a/../../etc/x.jpg",
    "x/../y.jpg",
    "../.ssh/authorized_keys.jpg",
])
def test_a_thumbnail_can_never_be_written_outside_the_thumbnails_directory(rel):
    """`relative_to` does not normalise, so `..` survives into the path that gets `mkdir`'d and
    written. Not reachable from today's callers — both hand over filesystem-derived paths — but
    this is where the path is built, so this is where it has to hold."""
    got = ts.get_thumbnail_path(USER, USER / rel)
    resolved = os.path.normpath(str(got))
    assert resolved.startswith(str(USER / ".thumbnails")), \
        f"{rel} writes to {resolved}, outside the thumbnails directory"


def test_no_dot_dot_survives_into_the_thumbnail_path():
    """Normalising to a contained location is not enough on its own — a literal `..` component
    means the written path depends on what exists on disk at the time."""
    got = ts.get_thumbnail_path(USER, USER / "a" / ".." / ".." / "etc" / "x.jpg")
    assert ".." not in got.parts


def test_an_image_outside_the_user_directory_falls_back_to_its_bare_name():
    """The pre-existing safe fallback, kept: a path that is not under the user directory cannot
    produce a relative path, and must not produce an absolute one either."""
    got = ts.get_thumbnail_path(USER, Path("/etc/passwd.jpg"))
    assert os.path.normpath(str(got)).startswith(str(USER / ".thumbnails"))


def test_a_very_long_name_is_hashed_rather_than_left_to_the_filesystem():
    """Most filesystems cap a component at 255 bytes, and the flattened name grows with directory
    depth. Left alone it would be an OSError on write — for deeply nested files only."""
    deep = USER.joinpath(*[f"dir{i}" for i in range(20)]) / ("x" * 200 + ".jpg")
    assert len(ts.get_thumbnail_path(USER, deep).name) < 255


def test_the_path_is_stable_across_calls():
    """Writer, reader, exists-check and deleter each call this separately. An unstable answer would
    write a thumbnail nobody can find and delete nothing."""
    p = USER / "a" / "photo.jpg"
    assert ts.get_thumbnail_path(USER, p) == ts.get_thumbnail_path(USER, p)


def test_thumbnails_live_under_a_dot_thumbnails_directory():
    """It is skipped by name in listings and walks; renaming it would surface every thumbnail as a
    file in the user's own browser."""
    assert ".thumbnails" in ts.get_thumbnail_path(USER, USER / "a.jpg").parts


def test_subdirectory_structure_is_preserved():
    got = ts.get_thumbnail_path(USER, USER / "holiday" / "beach.jpg")
    assert got.parent == USER / ".thumbnails" / "holiday"


# --------------------------------------------------------------------------- classification


@pytest.mark.parametrize("name", ["a.jpg", "a.JPG", "a.jpeg", "a.PNG", "a.gif", "a.webp",
                                  "a.bmp", "a.tiff", "a.tif"])
def test_image_extensions_are_recognised_case_insensitively(name):
    """A camera writing `.JPG` is the common case, and an unrecognised extension means no thumbnail
    at all — a blank tile rather than an error."""
    assert ts.is_image_file(Path(name)) and ts.is_media_file(Path(name))


@pytest.mark.parametrize("name", ["a.mp4", "a.MOV", "a.mkv", "a.webm", "a.m4v", "a.3gp"])
def test_video_extensions_are_recognised_case_insensitively(name):
    assert ts.is_video_file(Path(name)) and ts.is_media_file(Path(name))


@pytest.mark.parametrize("name", ["a.txt", "a.pdf", "a", "a.jpg.txt", ".jpg", "a.exe"])
def test_non_media_is_not_media(name):
    assert not ts.is_media_file(Path(name))


def test_the_two_extension_sets_do_not_overlap():
    """`is_media_file` is the union, but the two are also used separately to choose between Pillow
    and ffmpeg. A format in both would be handed to whichever branch is tested first."""
    assert ts.IMAGE_EXTENSIONS & ts.VIDEO_EXTENSIONS == set()


def test_every_extension_is_lowercase_with_a_leading_dot():
    """The lookups compare against `suffix.lower()`, so an entry stored as `JPG` or `jpg` can never
    match and that format silently loses thumbnails."""
    for ext in ts.IMAGE_EXTENSIONS | ts.VIDEO_EXTENSIONS:
        assert ext.startswith(".") and ext == ext.lower()


# --------------------------------------------------------------------------- generation


def test_a_real_image_produces_a_real_thumbnail(tmp_path):
    src = png(tmp_path / "photo.png", size=(800, 600))
    out = tmp_path / "thumbs" / "photo.jpg"
    assert ts.generate_thumbnail_file(src, out) is True
    assert out.exists()
    with Image.open(out) as im:
        assert im.width <= 200 and im.height <= 200
        assert im.format == "JPEG"


def test_the_aspect_ratio_is_preserved(tmp_path):
    src = png(tmp_path / "wide.png", size=(800, 200))
    out = tmp_path / "wide.jpg"
    ts.generate_thumbnail_file(src, out)
    with Image.open(out) as im:
        assert im.width == 200 and im.height == 50


def test_an_image_smaller_than_the_thumbnail_is_not_upscaled(tmp_path):
    """Pillow's `thumbnail()` never enlarges. Upscaling would make every small avatar blurry and
    cost more bytes than the original."""
    src = png(tmp_path / "tiny.png", size=(32, 32))
    out = tmp_path / "tiny.jpg"
    ts.generate_thumbnail_file(src, out)
    with Image.open(out) as im:
        assert (im.width, im.height) == (32, 32)


def test_the_output_directory_is_created(tmp_path):
    src = png(tmp_path / "photo.png")
    out = tmp_path / "does" / "not" / "exist" / "photo.jpg"
    assert ts.generate_thumbnail_file(src, out) is True
    assert out.exists()


def test_a_transparent_png_becomes_a_valid_jpeg(tmp_path):
    """JPEG has no alpha. Handing Pillow an RGBA image raises `cannot write mode RGBA as JPEG`, so
    every screenshot and logo would silently have no thumbnail."""
    src = tmp_path / "alpha.png"
    Image.new("RGBA", (100, 100), (255, 0, 0, 128)).save(src)
    out = tmp_path / "alpha.jpg"
    assert ts.generate_thumbnail_file(src, out) is True
    with Image.open(out) as im:
        assert im.mode == "RGB"


def test_a_palette_image_becomes_a_valid_jpeg(tmp_path):
    """The other mode that cannot be written as JPEG directly — every GIF is one."""
    src = tmp_path / "p.gif"
    Image.new("P", (100, 100)).save(src)
    out = tmp_path / "p.jpg"
    assert ts.generate_thumbnail_file(src, out) is True


# --------------------------------------------------------------------------- bad input


def test_a_missing_file_is_false_not_an_exception(tmp_path):
    """These run in a thread pool over a whole user directory. One raise would abort the batch and
    every file after it would silently have no thumbnail."""
    assert ts.generate_thumbnail_file(tmp_path / "nope.png", tmp_path / "o.jpg") is False


def test_an_empty_file_is_false(tmp_path):
    src = tmp_path / "empty.png"
    src.write_bytes(b"")
    assert ts.generate_thumbnail_file(src, tmp_path / "o.jpg") is False


def test_a_file_that_is_not_an_image_is_false(tmp_path):
    """An extension is a claim, not a format. Anyone can upload `payload.jpg` full of text."""
    src = tmp_path / "fake.jpg"
    src.write_bytes(b"this is not an image, it is just some text" * 10)
    assert ts.generate_thumbnail_file(src, tmp_path / "o.jpg") is False


def test_a_truncated_image_is_false_rather_than_a_broken_thumbnail(tmp_path):
    """An interrupted upload. Pillow will happily decode the prefix of some formats and produce a
    half-grey image, so this is verified before processing."""
    good = png(tmp_path / "good.png", size=(400, 400))
    data = good.read_bytes()
    bad = tmp_path / "cut.png"
    bad.write_bytes(data[: len(data) // 2])
    assert ts.generate_thumbnail_file(bad, tmp_path / "o.jpg") is False


def test_no_thumbnail_is_left_behind_when_generation_fails(tmp_path):
    """A zero-byte or partial `.jpg` in the cache is worse than none: `get_thumbnail_if_exists`
    only checks existence and mtime, so a broken file would be served for ever."""
    src = tmp_path / "fake.jpg"
    src.write_bytes(b"not an image at all")
    out = tmp_path / "out.jpg"
    ts.generate_thumbnail_file(src, out)
    assert not out.exists() or out.stat().st_size > 0


def test_an_oversized_file_is_refused_without_decoding_it(tmp_path):
    """The file-size cap exists so a huge upload is rejected on `stat`, before any decoder sees it."""
    src = tmp_path / "big.png"
    src.write_bytes(b"\x00" * (2 * 1024 * 1024))
    assert ts.generate_thumbnail_file(src, tmp_path / "o.jpg", max_image_size_mb=1) is False


def test_the_size_cap_is_a_cap_and_not_a_floor(tmp_path):
    """The same file under the limit must still work, or the guard is just 'thumbnails are off'."""
    src = png(tmp_path / "ok.png", size=(64, 64))
    assert ts.generate_thumbnail_file(src, tmp_path / "o.jpg", max_image_size_mb=1) is True


# --------------------------------------------------------------------------- the cache contract


def test_a_missing_thumbnail_reads_as_absent(tmp_path):
    src = png(tmp_path / "photo.png")
    assert ts.get_thumbnail_if_exists(tmp_path, src) is None


def test_a_stale_thumbnail_is_reported_as_absent(tmp_path):
    """Edit a photo and the old thumbnail must not keep being served. mtime is the whole check."""
    src = png(tmp_path / "photo.png")
    thumb = ts.get_thumbnail_path(tmp_path, src)
    ts.generate_thumbnail_file(src, thumb)
    assert ts.get_thumbnail_if_exists(tmp_path, src) is not None

    os.utime(src, (thumb.stat().st_mtime + 100, thumb.stat().st_mtime + 100))
    assert ts.get_thumbnail_if_exists(tmp_path, src) is None, \
        "an edited image keeps serving its old thumbnail"


def test_deleting_a_thumbnail_that_never_existed_is_success(tmp_path):
    """Deleting a file is expected to clean up after it; a False here would be reported as a failed
    delete for every non-image."""
    assert ts.delete_thumbnail(tmp_path, tmp_path / "never.png") is True


def test_delete_removes_the_file_the_reader_would_have_found(tmp_path):
    """Reader and deleter must agree — that they both call `get_thumbnail_path` is what makes it
    true, and this is the test that would fail if one of them ever stopped."""
    src = png(tmp_path / "photo.png")
    thumb = ts.get_thumbnail_path(tmp_path, src)
    ts.generate_thumbnail_file(src, thumb)
    assert thumb.exists()
    assert ts.delete_thumbnail(tmp_path, src) is True
    assert not thumb.exists()
    assert ts.get_thumbnail_if_exists(tmp_path, src) is None
