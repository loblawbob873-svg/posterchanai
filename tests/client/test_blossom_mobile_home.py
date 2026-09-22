from pathlib import Path
from tests.client_source import client_source

ROOT = Path(__file__).resolve().parents[2]


def test_mobile_does_not_repeat_synced_folders_as_giant_home_tiles():
    app = client_source()
    css = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
    assert 'class="fx-home-synced"' in app
    mobile = css[css.index("/* The horizontal source strip already contains synced folders") :]
    mobile = mobile[: mobile.index("/* ---- A stream")]
    assert ".fx-home-synced{display:none}" in mobile
    assert ".fx-home{grid-template-columns:1fr" in mobile
    assert ".fx-home-tile{min-height:48px" in mobile
