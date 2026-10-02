"""Server Stats names fediverse content for what it is: "Fediverse posts", not a "mirror".

This server runs a native ActivityPub server; "mirror" read as a bridge. Only the LABEL changed --
the origin key stays `bridge`, the name the relay stores and the stats endpoint sends, so the row
still matches the data it describes.
"""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "static/js/client/stats.js").read_text(encoding="utf-8")


def test_the_fediverse_row_is_called_fediverse_posts_and_keeps_its_key():
    block = SRC[SRC.index("const ORIGIN_LBL = {"):]
    block = block[:block.index("};")]
    row = re.search(r"^\s*bridge:\s*\['([^']*)',\s*'([^']*)'\]", block, re.M)
    assert row, "the `bridge` origin lost its label (its key must not change: the server sends it)"
    assert "Fediverse posts" in row.group(1)
    assert "mirror" not in (row.group(1) + row.group(2)).lower()
