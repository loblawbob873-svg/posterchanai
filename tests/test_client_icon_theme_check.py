from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_theme_audit_keeps_one_dom_instead_of_reloading_a_live_timeline():
    src = (ROOT / "scripts/check_client_icon_themes.py").read_text()
    loop = src[src.index("for theme in THEMES:"):src.index("# A theme that renders")]
    assert "document.documentElement.dataset.theme" in loop
    assert 'Page.navigate' not in loop
    assert "one frozen DOM" in loop
