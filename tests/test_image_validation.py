"""THE FILTER BETWEEN THE FILE LISTING AND JSON, WITH NO TESTS BEHIND IT.

`app/utils/image_validation.py` had ZERO test references. It sits between whatever a directory
listing or a proxied storage node produced and the JSON that goes to the browser, and its whole
purpose is that the layer above it cannot be trusted to have produced clean dictionaries.

Both halves fail in a way the user sees but no log explains:

  * `validate_and_clean_image_data` drops anything it cannot make sense of. Too strict and pictures
    vanish from the gallery with an entry in a log nobody is reading; too loose and a `None` path
    reaches the client as `undefined` and renders a broken tile.
  * `ensure_serializable_image` exists because a `Path`, a `bytes` or a stray object in one image
    makes `json.dumps` raise — which fails the WHOLE response, so one odd file empties the entire
    listing. That is why the strongest assertion here is a real `json.dumps` round trip rather than
    a type check.

IT MUTATES ITS INPUT. `validate_and_clean_image_data` writes the cleaned `path` and `name` back into
the dict it was handed and returns the same object. That is a genuine side effect on the caller's
data, it is not mentioned in the docstring, and it is pinned below so it stays a decision.
"""
import json
from pathlib import Path

import pytest

from app.utils.image_validation import (
    ensure_serializable_image,
    validate_and_clean_image_data,
    validate_and_filter_images,
)


# --------------------------------------------------------------------------- what gets dropped


@pytest.mark.parametrize("junk", [None, "a string", 123, [], (), 0.5, True])
def test_anything_that_is_not_a_dict_is_dropped(junk):
    """A proxied node returning a list of strings is the case this exists for — without the check
    the next line raises `str has no attribute get` and takes the whole listing with it."""
    assert validate_and_clean_image_data(junk) is None


@pytest.mark.parametrize("img", [
    {"name": "x"},                 # no path at all
    {"path": None},
    {"path": ""},
    {"path": "   "},               # whitespace only
    {"path": "undefined"},         # the JavaScript string, not the value
])
def test_an_image_without_a_usable_path_is_dropped(img):
    """`path` is what the client fetches. The literal string "undefined" is in here because that is
    what a JS caller sends when a variable was never set, and it would otherwise be treated as a
    filename and produce a 404 tile."""
    assert validate_and_clean_image_data(img) is None


def test_a_valid_image_survives():
    got = validate_and_clean_image_data({"path": "/a/b/photo.jpg", "name": "photo.jpg"})
    assert got == {"path": "/a/b/photo.jpg", "name": "photo.jpg"}


def test_the_path_is_trimmed():
    assert validate_and_clean_image_data({"path": "  /a/b.jpg  "})["path"] == "/a/b.jpg"


def test_a_non_string_path_is_coerced_rather_than_dropped():
    """A `Path` object from a local walk is legitimate; only an unusable path is a reason to drop
    the picture."""
    assert validate_and_clean_image_data({"path": Path("/a/b.jpg")})["path"] == "/a/b.jpg"


# --------------------------------------------------------------------------- the name


def test_a_missing_name_is_taken_from_the_path():
    """The name is what the gallery labels the tile with. Blank labels everywhere is a worse
    outcome than a slightly wrong one."""
    assert validate_and_clean_image_data({"path": "/a/b/c.jpg"})["name"] == "c.jpg"


@pytest.mark.parametrize("name", ["", "   ", "undefined", None])
def test_an_unusable_name_falls_back_to_the_path(name):
    assert validate_and_clean_image_data({"path": "/a/b/c.jpg", "name": name})["name"] == "c.jpg"


def test_a_path_with_no_directory_is_its_own_name():
    assert validate_and_clean_image_data({"path": "c.jpg"})["name"] == "c.jpg"


def test_a_good_name_is_kept_and_trimmed():
    got = validate_and_clean_image_data({"path": "/a/b/c.jpg", "name": "  Holiday.jpg  "})
    assert got["name"] == "Holiday.jpg"


def test_both_fields_come_out_as_strings():
    """The client indexes them directly. A non-string here becomes `[object Object]` in the DOM."""
    got = validate_and_clean_image_data({"path": Path("/a/b.jpg"), "name": 12345})
    assert isinstance(got["name"], str) and isinstance(got["path"], str)


def test_other_keys_are_preserved():
    """It cleans two fields; everything else the listing gathered — size, modified, type — has to
    survive, or the gallery loses its sorting."""
    got = validate_and_clean_image_data({"path": "/a.jpg", "size": 10, "type": "image/jpeg"})
    assert got["size"] == 10 and got["type"] == "image/jpeg"


def test_it_mutates_the_dict_it_was_given():
    """PINNED AS A DECISION, not endorsed. It writes the cleaned values back into the caller's dict
    and returns the same object — so a caller that kept a reference sees it change under them. That
    is fine while every caller discards the original, which is a property of the callers; it is
    recorded here so a future 'why is my dict different' has an answer."""
    original = {"path": "  /a/b.jpg  "}
    returned = validate_and_clean_image_data(original)
    assert returned is original
    assert original["path"] == "/a/b.jpg" and original["name"] == "b.jpg"


# --------------------------------------------------------------------------- filtering a list


def test_the_bad_ones_are_dropped_and_the_good_ones_kept():
    got = validate_and_filter_images([{"path": "/a.jpg"}, None, {"nope": 1}, {"path": "/b.jpg"}])
    assert [i["path"] for i in got] == ["/a.jpg", "/b.jpg"]


def test_order_is_preserved():
    """The listing is already sorted by the caller. Reordering here would shuffle the gallery."""
    imgs = [{"path": f"/{c}.jpg"} for c in "cab"]
    assert [i["path"] for i in validate_and_filter_images(imgs)] == ["/c.jpg", "/a.jpg", "/b.jpg"]


@pytest.mark.parametrize("empty", [[], None, ()])
def test_nothing_in_is_nothing_out(empty):
    assert validate_and_filter_images(empty) == []


def test_a_list_of_entirely_bad_items_is_empty_not_an_exception():
    """This is the "all images were filtered out" case the source logs loudly. It has to be an
    empty gallery, not a failed request."""
    assert validate_and_filter_images([None, "x", {}], source="proxy") == []


# --------------------------------------------------------------------------- serialisable


def test_the_result_is_actually_json_serialisable():
    """THE POINT OF THE FUNCTION. One `Path` or `bytes` in one image raises inside `json.dumps` and
    fails the entire response — so a single odd file empties the whole listing. Asserting types
    field by field would miss whatever type nobody thought of; dumping it cannot."""
    img = {"path": Path("/a/b.jpg"), "name": b"caf\xc3\xa9", "size": "123",
           "modified": "1.5", "flag": True, "none": None, "weird": object()}
    assert json.loads(json.dumps(ensure_serializable_image(img)))


def test_a_path_becomes_a_string():
    assert ensure_serializable_image({"path": Path("/a/b.jpg")})["path"] == "/a/b.jpg"


def test_bytes_are_decoded():
    assert ensure_serializable_image({"name": b"caf\xc3\xa9"})["name"] == "café"


def test_undecodable_bytes_do_not_raise():
    """`errors='ignore'` — a filename in some other encoding must cost that filename, not the
    listing."""
    assert isinstance(ensure_serializable_image({"name": b"\xff\xfe bad"})["name"], str)


def test_the_thumbnail_is_dropped():
    """"Skip thumbnails - they're loaded on-demand". Left in, every listing carries its images'
    bytes inline."""
    assert "thumbnail" not in ensure_serializable_image({"path": "/a.jpg", "thumbnail": b"BIG"})


def test_numbers_keep_their_types():
    got = ensure_serializable_image({"size": 42, "modified": 1.5})
    assert got["size"] == 42 and isinstance(got["size"], int)
    assert got["modified"] == 1.5 and isinstance(got["modified"], float)


def test_a_boolean_stays_a_boolean():
    """`bool` is a subclass of `int`, so it is matched by the numeric branch before its own. It
    still comes out as a bool, which is what JSON needs — a `1` where the client expects `true`
    would be a truthy value with a different meaning in JavaScript."""
    assert ensure_serializable_image({"flag": True})["flag"] is True


def test_a_string_number_is_coerced():
    """Sizes arrive as strings from a proxied node. The client sorts on them numerically."""
    assert ensure_serializable_image({"size": "123"})["size"] == 123
    assert ensure_serializable_image({"modified": "1.5"})["modified"] == 1.5


def test_a_float_string_size_does_not_lose_the_field():
    """Converted via float first, on purpose — `int("123.0")` raises and would zero the size."""
    assert ensure_serializable_image({"size": "123.0"})["size"] == 123


@pytest.mark.parametrize("bad", ["abc", "", None, [], {}])
def test_an_uncoercible_size_falls_back_to_zero(bad):
    """Zero is wrong but sortable. An exception here fails the listing."""
    assert ensure_serializable_image({"size": bad})["size"] == 0


@pytest.mark.parametrize("bad", ["abc", "", None, [], {}])
def test_an_uncoercible_modified_falls_back_to_zero(bad):
    assert ensure_serializable_image({"modified": bad})["modified"] == 0.0


def test_every_field_the_client_reads_is_always_present():
    """The gallery indexes these directly. A missing key is an exception in the browser, on a
    listing that otherwise worked."""
    got = ensure_serializable_image({})
    assert got == {"name": "", "path": "", "size": 0, "modified": 0.0,
                   "modified_date": "", "type": "unknown"}


def test_none_values_become_empty_strings_not_the_string_none():
    """`str(None)` is `"None"`, which would render as a file called None.

    Note the asymmetry, measured rather than assumed: an ABSENT `type` defaults to "unknown", but an
    explicit `type: None` comes out as "" — the conversion loop turns it into "" before the
    missing-field default can apply, and "" is present so the default never fires. Both are falsy
    and the client treats them the same, so this records the behaviour rather than calling it a
    bug; it is the kind of difference that only matters if something ever starts branching on the
    literal string "unknown"."""
    got = ensure_serializable_image({"name": None, "path": None, "type": None})
    assert got["name"] == "" and got["path"] == "" and got["type"] == ""
    assert ensure_serializable_image({})["type"] == "unknown"


def test_an_unknown_object_becomes_a_string_rather_than_breaking_the_dump():
    got = ensure_serializable_image({"weird": object()})
    assert isinstance(got["weird"], str)


def test_the_two_halves_compose():
    """The real pipeline: validate, then serialise, then dump."""
    raw = [{"path": Path("/a/b.jpg"), "size": "10"}, None, {"path": "undefined"}]
    out = [ensure_serializable_image(i) for i in validate_and_filter_images(raw)]
    assert len(out) == 1
    assert json.loads(json.dumps(out))[0]["name"] == "b.jpg"
