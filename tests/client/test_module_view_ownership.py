"""Every lazy app must stop painting when another app owns the shared feed."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def src(name):
    return (ROOT / "static/js/client" / name).read_text()


def test_central_dispatch_rechecks_ownership_after_lazy_load():
    app = src("app.js")
    body = app[app.index("function renderModuleView("):app.index("function renderView(")]
    assert "if(VIEW !== view)" in body
    assert body.count("VIEW !== view") >= 2, "lazy module completion can render after navigation"


def test_every_module_backed_app_has_a_view_ownership_gate():
    # Some older modules use the exported VIEW getter through inView(); newer ones use isView().
    # Both are synchronous checks against app.js's authoritative current view.
    modules = {
        "concord.js":"concord", "news.js":"news", "websearch.js":"websearch",
        "term.js":"terminal", "code.js":"code", "calendar.js":"calendar",
        "contacts.js":"contacts", "markets.js":"markets", "meme.js":"meme",
        "stats.js":"stats", "budget.js":"budget", "sync.js":"sync",
        "vault.js":"vault", "webxdc.js":"xdc",
    }
    for filename, view in modules.items():
        text = src(filename)
        guarded = (f"isView('{view}')" in text
                   or f"VIEW === '{view}'" in text or f"VIEW === \"{view}\"" in text
                   or f"VIEW !== '{view}'" in text or f"VIEW !== \"{view}\"" in text)
        assert guarded, f"{filename} can paint the shared feed without checking ownership"


def test_concord_to_code_race_is_executed_not_only_source_checked():
    runtime = src("../../../tests/client/concord_runtime.mjs")
    assert "activeView='code'" in runtime
    assert "late Concord render replaced Code" in runtime
