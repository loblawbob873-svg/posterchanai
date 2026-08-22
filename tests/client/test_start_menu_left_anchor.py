from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_start_panel_opens_above_the_left_aligned_start_button():
    css = (ROOT / "static" / "css" / "client.css").read_text(encoding="utf-8")
    rule = css[css.index(".os-startmenu{"):css.index("}", css.index(".os-startmenu{"))]
    assert "inset-inline-start:10px" in rule
    assert "translateX(-50%)" not in rule
