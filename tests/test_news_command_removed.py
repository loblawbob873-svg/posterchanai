"""The `news` / `dailynews` command and its "News sources" setting are gone; the News APP is not.

"remove 'News sources' from Settings and the code feature. we dont need that anymore" -- and, said
three times: "dont break news rss app though" / "news rss feature and app must remain". So this pins
both halves: what was removed answers or is absent, and the RSS reader's own endpoints and view stay.
"""
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _routes(router):
    return {r.path for r in router.routes}


def test_the_command_answers_that_it_moved_instead_of_reaching_the_llm():
    from app.services.command_service import CommandService
    svc = CommandService.__new__(CommandService)          # retired commands need no user, db or session
    for word in ("news", "dailynews"):
        assert word in CommandService.RETIRED_COMMANDS
        assert word not in CommandService.COMMANDS, f"`help` would still advertise {word}"
        res = asyncio.run(CommandService.execute_command(svc, word, ""))
        assert "News" in res["content"] and "removed" in res["content"], res


def test_the_news_router_keeps_only_the_apps_summarizer():
    from app.routers import news, rss
    paths = _routes(news.router)
    assert "/api/news/summarize" in paths, "the News app's summarize button needs this"
    assert not paths & {"/api/news/sources", "/api/news/all"} and not any("headlines" in p for p in paths), paths
    assert {"/api/rss/feed", "/api/rss/feeds"} <= _routes(rss.router), "the News app fetches its feeds here"


def test_the_summarizer_stays_behind_the_membership_gate():
    """It left the gate tests' route lists with /api/news/sources (an allowed call would fetch a real
    page), so the gate on what remains is pinned here."""
    from app.routers import news
    route = next(r for r in news.router.routes if r.path == "/api/news/summarize")
    names = {d.call.__name__ for d in route.dependant.dependencies}
    assert "get_instance_user" in names, names


def test_no_news_sources_setting_anywhere():
    from app import schemas
    from app.models import User
    for model in (schemas.SettingsResponse,):
        assert "news_sources" not in model.model_fields
    assert not any("news_sources" in getattr(m, "model_fields", {}) for m in vars(schemas).values() if isinstance(m, type))
    assert not hasattr(User, "news_sources")
    for f in ("static/js/client/settings.js", "templates/admin/tabs/tools.html", "static/js/admin.js"):
        text = (ROOT / f).read_text()
        assert "news_sources" not in text and "us-news-src" not in text, f


def test_the_news_app_is_still_wired():
    app_js = (ROOT / "static/js/client/app.js").read_text()
    assert "renderModuleView('news','news.js','PCNews','render')" in app_js
    assert 'data-view="news"' in (ROOT / "templates/client.html").read_text()
    assert (ROOT / "static/js/client/news.js").exists()
