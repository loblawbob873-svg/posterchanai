"""The stream popout window — "🗔 Window" on a stream.

Three bugs, all of which looked EXACTLY like the styling had never been written, and two of which
were "fixed" twice from memory before anyone rendered the page:

  1. `.stream-player` IS the <video> — the class sits on the element itself, not on a wrapper. Two
     passes styled `body.popout .stream-player video`, a selector that matches nothing in this
     markup, so the window kept drawing exactly as if none of it existed. That is what left the
     player stretched to the pane and a 16:9 picture letterboxing itself inside it: measured, a
     1920x1080 stream in a 1600x768 box. Sizing the element by its own aspect ratio (width/height
     auto + max 100%) makes the box BE the picture, so there is nothing left to paint black.
  2. The "Pop out player" / "Open stream URL" row was hidden with `.row:last-child`. A stream with a
     `summary` renders `.about` AFTER that row, and every live stream on the relay has one (12 of 12
     when this was measured), so the qualifier meant the rule never matched a real stream.
  3. In the desktop bundle the window opened onto "not found" — literally the app:// scheme
     handler's 404 body. openStreamWindow built `<origin>/<naddr>?popout=1`, which is a real route
     on the web and, in a bundle whose page is /index.html, the path
     `app://posterchan/index.html/naddr1…` — a file that exists nowhere. There is no router in a
     bundle, so the entity has to travel as a QUERY on the document that is already loaded.

Every assertion here is on a fact one of those bugs got wrong. The layout itself is verified by
rendering the real popout in headless Chrome (letterbox came out 0px, the row `display:none`); this
file is the cheap guard that stops the selectors drifting back.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = open(os.path.join(ROOT, "static", "css", "client.css"), encoding="utf-8").read()
APP_JS = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()


# ---- 1. the player is the video ---------------------------------------------------------------

def test_stream_player_class_is_on_the_video_element():
    """The premise the dead selector got wrong. If this ever becomes a wrapper, the popout rules
    that style `.stream-player` directly have to be revisited — which is the point of asserting it."""
    assert re.search(r'<video class="stream-player"', APP_JS), \
        ".stream-player is no longer the <video> itself — recheck every body.popout .stream-player rule"


def test_no_popout_rule_targets_a_descendant_video():
    """`.stream-player video` matches nothing. A rule written that way is not a weak fix, it is no
    fix, and it reads like one — which is how the same mistake shipped twice."""
    dead = [ln.strip() for ln in CSS.splitlines()
            if "popout" in ln and re.search(r"\.stream-player\s+video", ln)]
    assert not dead, f"selector matches nothing (.stream-player IS the video): {dead}"


def test_popout_player_is_sized_by_its_own_aspect_ratio():
    """Stretching the element is what produced the black bands. height:auto + max-height is what
    makes a replaced element scale to fit while keeping its ratio."""
    block = _popout_rule(r"body\.popout \.stream-player\{")
    assert re.search(r"height:\s*auto", block), "popout player must not be stretched (black bands)"
    assert re.search(r"max-height:\s*100%", block), "popout player must still be bounded by the pane"
    assert not re.search(r"height:\s*100%", block.replace("max-height:100%", "")), \
        "height:100% stretches the box and the picture letterboxes inside it"


# ---- 2. the redundant button row --------------------------------------------------------------

def test_button_row_is_hidden_without_a_last_child_qualifier():
    assert re.search(r"body\.popout \.stream-main\s*>\s*\.row\{", CSS), \
        "the popout's button row must be hidden by position-independent selector"
    assert not re.search(r"body\.popout \.stream-main\s*>\s*\.row:last-child", CSS), \
        ":last-child never matches — .about follows the row on any stream with a summary"


def test_the_markup_still_puts_about_after_the_row():
    """The reason :last-child failed. If the order ever changes this test is the note explaining
    why the selector is written the way it is."""
    main = APP_JS[APP_JS.index('<div class="stream-main">'):]
    main = main[:main.index('<div class="stream-chat"')]
    assert main.index('class="row"') < main.index('class="about"'), \
        "the summary no longer follows the button row — recheck the popout hide rule"


def test_only_one_row_in_stream_main():
    """The unqualified selector is only safe while that holds."""
    main = APP_JS[APP_JS.index('<div class="stream-main">'):]
    main = main[:main.index('<div class="stream-chat"')]
    assert main.count('<div class="row"') == 1, \
        "a second .row in .stream-main would now be hidden in the popout too"


# ---- 3. the desktop window's URL --------------------------------------------------------------

def test_popout_url_carries_the_entity_as_a_query():
    """The INVARIANT is that the entity is a query parameter, not a path segment.

    This used to assert one exact source literal, `'?popout=1&e=' + encodeURIComponent(naddr)`, and
    that broke the moment the chat popout added `&chat=1` between the two halves — the URL was still
    a perfectly good query, the string just was not contiguous any more. A guard that fails on a
    correct refactor teaches people to edit the guard, which is the opposite of the job. So it
    asserts the two things that actually matter: the entity is appended as `&e=`/`?e=` and encoded,
    and the old path-segment form is nowhere."""
    assert re.search(r"[?&]e=' \+ encodeURIComponent\(naddr\)", APP_JS), \
        "the popout entity must travel as an ENCODED query param — a bundle has no router to " \
        "resolve a path segment"
    assert "'?popout=1'" in APP_JS, "the popout marker itself is gone from the URL"
    assert "+ '/' + naddr + '?popout=1'" not in APP_JS, \
        "path-segment form 404s in the desktop bundle (app:// reads a file off disk)"


def test_query_deep_link_is_actually_read():
    """Writing the URL is half of it; the boot path has to decode it or the window opens on Global."""
    assert "function _entityFromQuery(" in APP_JS
    body = APP_JS[APP_JS.index("function _entityFromPath("):]
    body = body[:body.index("\n  async function routeFromPath")]
    assert body.count("_entityFromQuery()") == 2, \
        "_entityFromPath must fall back to the query both when the path is empty and when it holds no entity"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_popout_url_resolves_to_a_real_document_in_every_shell():
    """The path arithmetic, run for real against each shell's actual entry point.

    /index.html is the desktop bundle (APP_URL in desktop/main.js) and is the case that was broken.

    The web cases all have to land on /client, and a BARE "/" is the trap: `GET /` is a 302 to
    /client (app/main.py) and a redirect DROPS THE QUERY, so a popout URL of "/?popout=1&e=…" opens
    the ordinary timeline with no stream — after this tab has already torn its own player down. It
    is reachable, not theoretical: a shared root link leaves location.pathname at "/" all session.
    """
    naddr = "naddr1qqjrgwp5x3jrqvpk95cnzvfk9"
    body = (APP_JS[APP_JS.index("let doc = location.pathname"):]
            .split("const url =")[0])
    src = (
        "function build(pathname, BUNDLED){ const location={pathname}; %s return doc; }\n" % body
        + "const naddr=%s;\n" % json.dumps(naddr)
        + "process.stdout.write(JSON.stringify({"
        + "bundle:build('/index.html', true),"
        + "bundleRoot:build('/', true),"
        + "web:build('/client/'+naddr, false),"
        + "webroot:build('/'+naddr, false),"
        + "webslash:build('/', false),"
        + "client:build('/client', false)}));"
    )
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    # The bundle's page is a FILE served off disk. It must be left alone — this is the whole bug.
    assert got["bundle"] == "/index.html"
    assert got["bundleRoot"] == "/index.html", "a bundle must never be sent to /client — there is no router"
    # The entity segment comes off, so the popout of a deep-linked stream does not inherit a path
    # the query is about to supersede…
    assert got["web"] == "/client"
    assert got["client"] == "/client"
    # …and nothing on the web is ever left pointing at "/".
    assert got["webroot"] == "/client"
    assert got["webslash"] == "/client"
    assert "/" not in {got["webroot"], got["webslash"]}, "a bare / redirects and loses the query"


# ---- helpers -----------------------------------------------------------------------------------

def _popout_rule(pattern):
    m = re.search(pattern, CSS)
    assert m, f"no popout rule matching {pattern}"
    end = CSS.index("}", m.end())
    return CSS[m.end():end]
