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


# ---- FLIPPING THE SWITCH USED TO CHANGE A SETTING AND NOTHING ELSE -----------------------------
#
# `apply_outgoing_proxy` runs in exactly one place — building the WSGI app — so it ran ONCE, at
# start. Turn "Route engine requests through this node's proxy" on afterwards and settings.yml was
# never rewritten: the setting said one thing, the file said another, and the engines carried on
# going wherever the last start decided. MEASURED on this node by check_websearch_rate:
# `searxng_proxy_engines=True  settings.yml proxies=False`, every search fast and DIRECT, with
# nothing anywhere saying the toggle had not landed.


def test_the_admin_save_re_applies_the_toggle():
    from pathlib import Path
    admin = (Path(__file__).resolve().parents[1] / "app/routers/admin.py").read_text(encoding="utf-8")
    assert "refresh_outgoing_proxy" in admin, "nothing re-applies the engine-proxy toggle on save"
    assert "searxng_proxy_engines" in admin
    assert "proxy_fallback_port" in admin, "the port is half the same policy and needs the same refresh"


def test_a_no_op_save_does_not_restart_the_unit():
    """A restart drops every in-flight search; saving an unrelated setting must not cost one."""
    src = inspect.getsource(searxng_native.refresh_outgoing_proxy)
    assert "if after == before:" in src, src
    assert src.index("if after == before:") < src.index("systemctl"), \
        "the restart runs before the no-change check"


def test_the_restart_is_best_effort_and_reported():
    """SearXNG reads `outgoing:` once at import, so the file alone is not enough — but a node with
    no passwordless sudo must still end up with a correct file rather than an exception."""
    src = inspect.getsource(searxng_native.refresh_outgoing_proxy)
    assert '"sudo", "-n", "systemctl"' in src
    assert "except Exception" in src
    assert 'out["restarted"]' in src
    assert "logger.info" in src, "an unattempted or failed restart must say so"
