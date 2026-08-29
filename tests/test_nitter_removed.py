"""Nitter is gone — and the ONE piece that stayed is not a Nitter feature.

Nitter shut down, so everything here that FETCHED from it was removed: the per-user RSS poller
(`nitter_feeds_service`), the bot's `--nitter` mode (`botframework/nitterListener.py`), the
`nitter_feeds` user setting, and the admin/bot UI for both.

What stayed is `youtube_service`'s URL REWRITER, and the distinction is the whole point of this
file. It does not fetch from Nitter or depend on any instance being up: it turns a pasted
`https://<mirror>/<user>/status/<id>` link into the canonical `x.com` form so yt-dlp's Twitter
extractor can download it. Those links are still everywhere — in old chat logs, and live from the
mirrors people still use — so deleting it would break URLs that work today. A future cleanup that
reads "remove all Nitter references" and greps for the word will land on it; this test is the note
that says don't.
"""
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------- gone


@pytest.mark.parametrize("path", [
    "app/services/nitter_feeds_service.py",
    "botframework/nitterListener.py",
    "tests/test_nitter_poster.py",
])
def test_the_dead_feeds_code_is_deleted(path):
    assert not os.path.exists(os.path.join(ROOT, path)), path


def test_the_worker_no_longer_schedules_the_poller():
    """A scheduler entry naming a module that does not exist is an ImportError in the WORKER
    process, which is a separate process — so the app would start fine and the pollers that share
    that worker would silently never run."""
    src = _read("app/worker.py")
    assert "nitter" not in src.lower()


def test_the_per_user_setting_is_gone_from_the_schema_and_the_route():
    for rel in ("app/schemas.py", "app/routers/auth.py"):
        assert "nitter" not in _read(rel).lower(), rel


def test_the_bot_manager_injects_no_nitter_env():
    """The manager builds each bot's env from its JSON config; a leftover NITTER_FEEDS would be
    handed to a `--nitter` mode that no longer exists.

    Scoped to the ENV BUILD, not to the word: the file must still name `--nitter` in `_RETIRED_MODES`
    so a stored mode is stripped before it reaches the CLI (see below). A blanket word check here
    would read that fix as the bug."""
    src = _read("app/services/bot_manager_service.py")
    assert "NITTER_FEEDS" not in src and "NITTER_POLL_SECONDS" not in src
    assert "nitter_feeds" not in src


def test_the_bot_cli_has_no_nitter_mode_and_still_parses():
    """Removing an argparse entry is easy to get half-right: deleting the argument line and leaving
    its `parser.add_argument(\\n)` wrapper raises TypeError at STARTUP, for every bot, on every
    mode — which no unit test of the removed feature would ever notice."""
    src = _read("botframework/main.py")
    assert "--nitter" not in src
    assert not re.search(r"parser\.add_argument\(\s*\)", src), "an empty add_argument() was left behind"
    p = subprocess.run([sys.executable, "main.py", "--help"],
                       cwd=os.path.join(ROOT, "botframework"), capture_output=True, timeout=90)
    assert p.returncode == 0, p.stderr.decode()[-800:]
    assert b"--nitter" not in p.stdout


def test_no_ui_control_survives_without_the_feature_behind_it():
    """A checkbox whose mode is gone posts a flag the CLI now rejects, and a field group whose id no
    longer exists in the template is a silent `show()` no-op that hides nothing."""
    js, html = _read("static/js/admin-bots.js"), _read("templates/admin/tabs/bots.html")
    for token in ("bot_ft_nitter", "bot_grp_nitter", "bot_f_nitter_feeds", "bot_f_nitter_poll_seconds"):
        assert token not in js, "%s still referenced in admin-bots.js" % token
        assert token not in html, "%s still in the bots template" % token
    assert "nitter" not in _read("templates/includes/modals/user_settings.html").lower()
    # app.js keeps ONE mention on purpose: the link-action bar that offers MP3/Video on a pasted
    # mirror URL, which is the client half of the kept rewriter. Forbid the removed FEATURE, not the
    # word, or this test becomes an argument for deleting something that works.
    app = _read("static/js/client/app.js")
    assert "us-nitter" not in app and "nitter_feeds" not in app
    assert "xcancel" in app, ("the link-action bar must know the same mirror hosts the server "
                              "rewrites, or a pasted one loses its download buttons")


def test_a_retired_config_key_is_SHOWN_not_silently_dropped():
    """`nitter_feeds` was in the admin form's `known` set, which decides what does NOT fall through
    to the Advanced JSON box. Removing the field without removing it from that set would drop the
    key from an existing bot on its next save — the only record of what that bot used to do, gone
    with no warning. Out of `known`, it surfaces in Advanced where an operator can clear it."""
    js = _read("static/js/admin-bots.js")
    m = re.search(r"const known = new Set\(\[([^\]]*)\]\)", js)
    assert m, "the escape-hatch `known` set moved — re-point this test"
    assert "nitter" not in m.group(1)


# ---------------------------------------------------------------- the stored `modes` column


def test_a_stored_nitter_mode_never_reaches_the_cli():
    """THE BUG THIS FILE ALMOST SHIPPED WITHOUT.

    `bot.modes` is a stored comma list handed straight to `botframework/main.py`, and nothing
    rewrites it when a mode is retired. argparse rejects the WHOLE invocation on one unknown flag
    (exit 2) — so a `--pleroma,--nitter` bot does not merely lose Nitter, it loses its PLEROMA
    LISTENER: it never starts, crash-loops, and is parked by the manager's 10-restarts-per-hour cap
    until a human notices and re-saves it in Admin → Bots. Removing a CLI flag is therefore never
    just a CLI change."""
    from app.services.bot_manager_service import _cmd_for
    cmd = _cmd_for({"name": "b", "platform": "pleroma", "modes": ["--pleroma", "--nitter"]})
    assert "--nitter" not in cmd
    assert "--pleroma" in cmd, "stripping the retired mode must not cost the bot its real listener"


def test_a_bot_whose_ONLY_mode_was_nitter_still_gets_a_listener():
    """Stripping the last mode must not leave an empty command: `_cmd_for` reads empty as "default
    to the bot's own platform", so the fallback has to survive the filter or the bot silently
    changes meaning."""
    from app.services.bot_manager_service import _cmd_for
    for platform, want in (("pleroma", "--pleroma"), ("nostr", "--nostr")):
        cmd = _cmd_for({"name": "b", "platform": platform, "modes": ["--nitter"]})
        assert "--nitter" not in cmd
        assert want in cmd, (platform, cmd)


def test_a_nostr_bot_whose_only_mode_was_nitter_remains_presence_only():
    """Filtering the retired CLI flag must not silently opt the bot into mention replies."""
    from app.services.bot_manager_service import _build_env
    env = _build_env({"name": "b", "platform": "nostr", "modes": ["--nitter"]}, {})
    assert env.get("NOSTR_PRESENCE_ONLY") == "1"


def test_the_retired_set_is_explicit_not_a_catch_all():
    """"Drop anything argparse doesn't know" would swallow a typo'd flag and turn a loud startup
    failure into a bot quietly missing a feature. The list is named, so it is greppable and each
    entry says why it is there."""
    from app.services.bot_manager_service import _RETIRED_MODES, _cmd_for
    assert "--nitter" in _RETIRED_MODES
    cmd = _cmd_for({"name": "b", "platform": "nostr", "modes": ["--nostr", "--typoflag"]})
    assert "--typoflag" in cmd, "an unknown flag must still reach the CLI and fail loudly"


def test_the_migration_strips_the_dead_mode_without_emptying_the_column():
    """The other half of the fix: `_cmd_for` un-breaks a RUNNING node, the migration takes the dead
    flag out of the stored value so the Bots UI stops offering a feature that is gone. Asserted on
    the SQL, since running it needs Postgres."""
    src = _read("app/database.py")
    assert "array_remove" in src and "--nitter" in src
    assert "NULLIF" in src, ("the UPDATE must not leave an empty-string modes column — `_cmd_for` "
                             "reads empty as 'default to this bot's platform', which is a different "
                             "bot from one with no modes at all")


# ---------------------------------------------------------------- deliberately kept


def test_a_pasted_mirror_link_still_resolves_to_x_com():
    """The kept rewriter, RUN — not grepped. These hosts are alive and people paste them."""
    from app.services.youtube_service import normalize_download_url, extract_download_urls
    for host in ("nitter.net", "xcancel.com", "twiiit.com", "lightbrd.com", "nitter.example.org"):
        u = "https://%s/someone/status/1234567890" % host
        assert normalize_download_url(u) == "https://x.com/someone/status/1234567890", host
    found = extract_download_urls("look at https://xcancel.com/someone/status/1234567890 please")
    assert found == ["https://x.com/someone/status/1234567890"], found


def test_the_rewriter_leaves_every_other_url_alone():
    """It runs on every download entry point, so a false positive would silently rewrite an
    unrelated site's /<x>/status/<n> path into a Twitter URL that does not exist."""
    from app.services.youtube_service import normalize_download_url
    for u in ("https://youtube.com/watch?v=abc123",
              "https://example.com/user/status/1234567890",
              "https://github.com/o/r/issues/1",
              "", "not a url"):
        assert normalize_download_url(u) == u, u


def test_the_shared_post_card_renderer_survived():
    """`_render_post_card_png` was the Nitter poller's renderer AND is what Nostr share-images and
    the bot-facing /api/media/render-post-card use. Deleting it with the poller would have taken out
    two live features that have nothing to do with Nitter."""
    from app.services.command_service import _render_post_card_png
    assert callable(_render_post_card_png)
    assert "render_post_card" in _read("app/routers/media_api.py")
    assert "_render_post_card_png" in _read("app/routers/client.py")
