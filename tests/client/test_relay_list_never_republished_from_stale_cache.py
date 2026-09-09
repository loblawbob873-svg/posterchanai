"""Regression coverage for NIP-65 relay-list ownership.

Kind 10002 is replaceable.  Republishing a device's old localStorage value with a new
``created_at`` replaces a newer list edited in Amethyst (or any other Nostr client).
PosterChan's all-tabs Settings save must therefore leave NIP-65 alone unless the relay
controls themselves changed during that save.
"""

from pathlib import Path


APP = (Path(__file__).parents[2] / "static/js/client/app.js").read_text()


def _block(start: str, end: str) -> str:
    i = APP.index(start)
    return APP[i : APP.index(end, i)]


def test_global_settings_save_does_not_refresh_an_unchanged_stale_kind_10002():
    """The rule is that `relayChanged` GATES the write, not the shape of the line it is written on.

    This pinned `if(relayChanged && on && urls.length) await publish(10002` as one string, and went
    red when a second gate was added inside it (the write now also requires a confirmed read of the
    user's existing list — see test_relay_list_ownership.py). The contract was never weakened by
    that; it was made stricter, and a test that cannot tell those apart is measuring formatting.
    """
    body = _block("if($('#set-relays-on')){", "if($('input[name=media-mode]'))")

    assert "const relayChanged =" in body
    gate = body.index("if(relayChanged && on && urls.length)")
    publish = body.index("await publish(10002")
    assert gate < publish, "the kind-10002 write is no longer behind the relayChanged gate"
    # Nothing between the gate and the write may re-open it — only narrow it further.
    assert "}" not in body[gate:publish].split("{", 1)[-1], body[gate:publish]
    # And no ungated form anywhere: that is the stale-cache republish itself.
    assert "if(on && urls.length) await publish(10002" not in body


def test_global_settings_save_still_publishes_an_explicit_relay_edit():
    body = _block("if($('#set-relays-on')){", "if($('input[name=media-mode]'))")

    changed = body.index("if(relayChanged){")
    persisted = body.index("ClientSettings.set('relaysEnabled', on)")
    published = body.index("await publish(10002")
    assert changed < persisted < published


def test_disabling_local_relays_never_reannounces_urls_left_in_the_editor():
    body = _block("{ const b=$('#set-relays-save')", "{ const b=$('#set-media-save')")

    assert "const on=$('#set-relays-on').checked" in body
    assert "if(on && urls.length) await publish(10002" in body


def test_login_and_nostr_restore_paths_do_not_publish_relay_lists():
    auth = _block("function _persistAuthRelays()", "function _setInstanceFromAuth")
    restore = _block("async function loadNostrPrefs()", "// Per-user settings")

    assert "publish(10002" not in auth
    assert "publish(10002" not in restore


def test_only_explicit_relay_settings_paths_can_publish_kind_10002():
    # Keep a small tripwire over the complete client. A new login/sync/background path that starts
    # writing the replaceable list must be reviewed instead of silently gaining that authority.
    assert APP.count("publish(10002") == 2

