"""HALF THE EXIF DATE FORMATS COULD NEVER PARSE, AND THE FEATURE FAILED SILENTLY.

`app/utils/exif_utils.py` had ZERO test references. Its job is to give a copied photo back the date
it was taken: read the EXIF timestamp, set the file's mtime. Without it every rsynced or downloaded
picture sorts by when it arrived rather than when it happened.

THE BUG. `_parse_exif_date` began with

    date_str = date_str.split('+')[0].split('-')[0].strip()

meaning to strip a timezone. The second split is the defect: **a hyphen is the date separator in
half the formats the function lists.** `"2024-01-15 14:23:45"` was truncated to `"2024"`, which
nothing in the format table can parse, so it returned None — and four of the eight format strings
(`%Y-%m-%d %H:%M:%S`, `%Y-%m-%d %H:%M:%S.%f`, `%Y-%m-%dT%H:%M:%S`, `%Y-%m-%d`) were unreachable
code. Two of them are named in the docstring as formats it handles.

Nothing reports it. `restore_exif_timestamp` gets None, logs at debug, returns False, and the photo
keeps the mtime of when it was downloaded. The feature simply does not work for any camera or tool
that writes dashes — which is every ISO-8601 producer, including the `Z`-suffixed form the docstring
also promises.

The parametrised table below is the whole point: it walks every format the docstring claims,
including the ones that were dead, so a future "simplify the timezone handling" cannot quietly
delete four of them again.
"""
import os
from datetime import datetime
from pathlib import Path

import pytest

from app.utils import exif_utils as ex


# --------------------------------------------------------------------------- date parsing


#: (input, expected). Every format the docstring advertises, plus the timezone shapes exiftool
#: actually emits. The dash-dated rows are the ones that returned None before.
DATES = [
    ("2024:01:15 14:23:45",          datetime(2024, 1, 15, 14, 23, 45)),
    ("2024-01-15 14:23:45",          datetime(2024, 1, 15, 14, 23, 45)),
    ("2024:01:15 14:23:45-08:00",    datetime(2024, 1, 15, 14, 23, 45)),
    ("2024:01:15 14:23:45+08:00",    datetime(2024, 1, 15, 14, 23, 45)),
    ("2024-01-15T14:23:45Z",         datetime(2024, 1, 15, 14, 23, 45)),
    ("2024-01-15T14:23:45+05:30",    datetime(2024, 1, 15, 14, 23, 45)),
    ("2024-01-15 14:23:45-0800",     datetime(2024, 1, 15, 14, 23, 45)),
    ("2024:01:15T14:23:45",          datetime(2024, 1, 15, 14, 23, 45)),
    ("2024:01:15",                   datetime(2024, 1, 15, 0, 0, 0)),
    ("2024-01-15",                   datetime(2024, 1, 15, 0, 0, 0)),
    ("  2024:01:15 14:23:45  ",      datetime(2024, 1, 15, 14, 23, 45)),
]


@pytest.mark.parametrize("raw,expected", DATES, ids=[d[0].strip() for d in DATES])
def test_every_advertised_date_format_parses(raw, expected):
    assert ex._parse_exif_date(raw) == expected


def test_a_dash_dated_timestamp_is_not_truncated_to_its_year():
    """THE BUG, named on its own so a regression reads as itself rather than as one row of a table.
    `split('-')[0]` turned this into "2024"."""
    got = ex._parse_exif_date("2024-01-15 14:23:45")
    assert got is not None, "dash-formatted EXIF dates parse to nothing — the feature is off"
    assert (got.year, got.month, got.day) == (2024, 1, 15)


def test_a_timezone_is_stripped_rather_than_applied():
    """Deliberate: the wall-clock time in the EXIF field is the time the photo was taken where it
    was taken. Applying the offset would shift every holiday photo by the trip's timezone."""
    assert ex._parse_exif_date("2024:01:15 14:23:45-08:00").hour == 14
    assert ex._parse_exif_date("2024:01:15 14:23:45+08:00").hour == 14


def test_subsecond_precision_is_accepted():
    assert ex._parse_exif_date("2024:01:15 14:23:45.123").second == 45


@pytest.mark.parametrize("bad", ["", None, "garbage", "not a date", "0000:00:00 00:00:00",
                                 "2024:13:45 99:99:99", ":::", "2024"])
def test_an_unparseable_date_is_none_rather_than_an_exception(bad):
    """`0000:00:00` is what a camera writes when its clock was never set, and it is common. This
    runs over whole directories in a thread pool — one raise would end the batch."""
    assert ex._parse_exif_date(bad) is None


def test_a_date_with_no_year_is_not_invented():
    assert ex._parse_exif_date("01:15 14:23:45") is None


# --------------------------------------------------------------------------- file classification


@pytest.mark.parametrize("name", ["a.jpg", "a.JPG", "a.jpeg", "a.png", "a.heic", "a.HEIF",
                                  "a.tiff", "a.webp"])
def test_image_extensions_are_recognised_case_insensitively(name):
    """`.heic` matters most: iPhone photos are the ones most likely to carry a real
    DateTimeOriginal and most likely to arrive with a copy date."""
    assert ex.is_image_file(Path(name)) and not ex.is_video_file(Path(name))


@pytest.mark.parametrize("name", ["a.mp4", "a.MOV", "a.mkv", "a.m4v", "a.3gp", "a.webm"])
def test_video_extensions_are_recognised_case_insensitively(name):
    assert ex.is_video_file(Path(name)) and not ex.is_image_file(Path(name))


@pytest.mark.parametrize("name", ["a.txt", "a.pdf", "a", "a.jpg.txt", "a.exe"])
def test_other_files_are_neither(name):
    assert not ex.is_image_file(Path(name)) and not ex.is_video_file(Path(name))


def test_the_two_extension_sets_do_not_overlap():
    """They select different EXIF tag lists — a format in both would take whichever branch is
    tested first and look for tags it does not have."""
    imgs = {e for e in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".heic",
                        ".heif", ".webp"]}
    vids = {e for e in [".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".wmv", ".flv",
                        ".webm", ".mpg", ".mpeg"]}
    for e in imgs:
        assert ex.is_image_file(Path("x" + e)) and not ex.is_video_file(Path("x" + e))
    for e in vids:
        assert ex.is_video_file(Path("x" + e)) and not ex.is_image_file(Path("x" + e))


# --------------------------------------------------------------------------- restoring the mtime


@pytest.fixture
def photo(tmp_path):
    p = tmp_path / "holiday.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")
    return p


def _fake_exiftool(monkeypatch, date_str, available=True, returncode=0):
    """Stands in for the exiftool subprocess so the test does not depend on it being installed."""
    class R:
        def __init__(self, out):
            self.returncode = returncode
            self.stdout = out

    def run(cmd, **kw):
        if "-ver" in cmd:
            return R("12.0" if available else "")
        return R(date_str)

    monkeypatch.setattr(ex.subprocess, "run", run)
    monkeypatch.setattr(ex, "_check_exiftool_available", lambda: available)


def test_a_photos_mtime_is_set_from_its_exif_date(monkeypatch, photo):
    _fake_exiftool(monkeypatch, "2020:06:01 09:00:00")
    assert ex.restore_exif_timestamp(photo) is True
    assert datetime.fromtimestamp(photo.stat().st_mtime).year == 2020


def test_a_dash_dated_photo_is_also_restored(monkeypatch, photo):
    """End to end through the bug: before the fix this returned False and the file kept its
    creation time, with a debug line as the only evidence."""
    _fake_exiftool(monkeypatch, "2020-06-01 09:00:00")
    assert ex.restore_exif_timestamp(photo) is True
    assert datetime.fromtimestamp(photo.stat().st_mtime).year == 2020


def test_a_correct_timestamp_is_left_alone_and_still_counts_as_processed(monkeypatch, photo):
    """"Return True even if already correct, to count as processed" — a batch that reported those
    as failures would look broken on its second run."""
    _fake_exiftool(monkeypatch, "2020:06:01 09:00:00")
    ex.restore_exif_timestamp(photo)
    before = photo.stat().st_mtime
    assert ex.restore_exif_timestamp(photo) is True
    assert photo.stat().st_mtime == pytest.approx(before)


def test_no_exif_date_leaves_the_file_untouched(monkeypatch, photo):
    """Most files have no usable date. Guessing one would be worse than leaving it: it would sort
    the picture confidently into the wrong place."""
    _fake_exiftool(monkeypatch, "")
    before = photo.stat().st_mtime
    assert ex.restore_exif_timestamp(photo) is False
    assert photo.stat().st_mtime == pytest.approx(before)


def test_an_unparseable_exif_date_leaves_the_file_untouched(monkeypatch, photo):
    _fake_exiftool(monkeypatch, "0000:00:00 00:00:00")
    before = photo.stat().st_mtime
    assert ex.restore_exif_timestamp(photo) is False
    assert photo.stat().st_mtime == pytest.approx(before)


def test_without_exiftool_it_declines_rather_than_failing(monkeypatch, photo):
    """exiftool is an optional system package. Its absence is a feature that is off, not an error
    on every file."""
    _fake_exiftool(monkeypatch, "2020:06:01 09:00:00", available=False)
    assert ex.restore_exif_timestamp(photo) is False


def test_a_missing_file_is_false(monkeypatch, tmp_path):
    _fake_exiftool(monkeypatch, "2020:06:01 09:00:00")
    assert ex.restore_exif_timestamp(tmp_path / "nope.jpg") is False


def test_a_directory_is_false(monkeypatch, tmp_path):
    _fake_exiftool(monkeypatch, "2020:06:01 09:00:00")
    assert ex.restore_exif_timestamp(tmp_path) is False


def test_a_non_media_file_is_skipped_without_running_exiftool(monkeypatch, tmp_path):
    """Neither branch matches, so it returns before spawning a process. Over a large directory that
    is the difference between a sweep and a subprocess per file."""
    doc = tmp_path / "notes.txt"
    doc.write_text("hello")
    calls = []
    monkeypatch.setattr(ex, "_check_exiftool_available", lambda: True)
    monkeypatch.setattr(ex.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    assert ex.restore_exif_timestamp(doc) is False
    assert calls == []


def test_a_crashing_exiftool_is_survivable(monkeypatch, photo):
    """It runs across whole directories; one file that makes exiftool die must not end the sweep."""
    monkeypatch.setattr(ex, "_check_exiftool_available", lambda: True)

    def boom(cmd, **kw):
        raise OSError("exiftool died")

    monkeypatch.setattr(ex.subprocess, "run", boom)
    assert ex.restore_exif_timestamp(photo) is False
