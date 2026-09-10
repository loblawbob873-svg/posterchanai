"""The process that OWNS settings.yml must be able to read the setting that decides its contents.

`_proxy_wanted` refuses to act on an unhydrated settings store, and that guard is right: the unit
runs a bare `python -m app.services.searxng_native`, where `all_settings()` is empty, and rewriting
the file from `get_bool`'s own defaults is how the admin toggle came to do nothing on a live node.

But it left the feature with nobody able to apply it. `apply_outgoing_proxy` runs from exactly one
place — building the WSGI app — and of the two processes that do that, only the in-app mount has
settings, while the UNIT is the copy `resolve_searxng_url` prefers because it is already warm. So
`searxng_proxy_engines` could be ON, settings.yml could carry a managed block with no `proxies:`
line, and nothing would ever close the gap. `scripts/check_websearch_rate.py` measured exactly that
on this node: "the toggle did not reach the file".
"""
import inspect

from app.services import searxng_native


def test_serve_hydrates_before_it_builds_the_wsgi_app():
    """Order matters: apply_outgoing_proxy runs inside wsgi_app(), so hydrating after it is the
    same as not hydrating at all."""
    src = inspect.getsource(searxng_native.serve)
    assert "_hydrate_settings_for_the_unit()" in src, src
    assert src.index("_hydrate_settings_for_the_unit()") < src.index("wsgi_app()"), src


def test_a_failed_hydrate_leaves_the_operators_file_alone():
    """Best effort by construction. If hydration cannot happen, is_hydrated() stays false,
    _proxy_wanted returns its refusal, and settings.yml is untouched — today's behaviour."""
    src = inspect.getsource(searxng_native._hydrate_settings_for_the_unit)
    assert "except Exception" in src, src
    assert "raise" not in src, "a node with no database must still serve searches"
    guard = inspect.getsource(searxng_native._proxy_wanted)
    assert "is_hydrated()" in guard and "return False" in guard, (
        "the refusal this relies on must stay: an unreadable store means leave the file alone")


def test_it_does_not_rehydrate_a_store_that_is_already_loaded():
    """The in-app mount shares this module and is hydrated already; a second pass would be a
    pointless database round trip on every import."""
    src = inspect.getsource(searxng_native._hydrate_settings_for_the_unit)
    assert "if settings_store.is_hydrated():" in src and "return" in src, src
