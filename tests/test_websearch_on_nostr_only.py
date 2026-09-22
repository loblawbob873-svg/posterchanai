"""Web Search is shown on a nostr-only node. "SearXNG is part of the normal install" (2026-09-21):
install_nostr_only now installs it, so hiding the screen there hid a working feature. The AI parts
of the screen stay off on such a node (websearch.js aiOff), and a build with NO instance still hides
it (INSTANCE_VIEWS)."""
import re
from pathlib import Path

import jinja2

ROOT = Path(__file__).resolve().parent.parent


def _render(nostr_only):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")),
                             undefined=jinja2.ChainableUndefined)
    return env.get_template("client.html").render(nostr_only=nostr_only, ver="1", meta=None)


def test_the_sidebar_offers_web_search_on_a_nostr_only_node():
    assert 'data-view="websearch"' in _render(True)
    assert 'data-view="websearch"' in _render(False)


def test_the_phone_sheet_does_not_filter_it_out():
    app = (ROOT / "static/js/client/app.js").read_text()
    assert "PC_NOSTR_ONLY && v==='websearch'" not in app


def test_its_ai_features_stay_off_and_no_instance_still_hides_it():
    ws = (ROOT / "static/js/client/websearch.js").read_text()
    assert re.search(r"aiOff\s*=\s*\(\)\s*=>\s*!!window\.PC_NOSTR_ONLY", ws)
    app = (ROOT / "static/js/client/app.js").read_text()
    iv = app[app.index("const INSTANCE_VIEWS"):app.index("]);", app.index("const INSTANCE_VIEWS"))]
    assert "'websearch'" in iv
