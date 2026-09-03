"""THE COMPOSER OPENS IN ITS OWN WINDOW — "oh for fucks sake, the new post, reply, modal gets stuck
behind windows".

Last of four reports with one cause: sway paints floating windows above tiled ones, the desktop
shell IS the tiled window, and nothing inside that page can be raised above an application. The
start menu, the notification centre and the tray flyout were the other three.

A composer is the one of the four where moving it costs something, so two things were measured
before it was written rather than after:

  * a popup window maps in **0.26s** on the real machine, across three trials — the same as the
    notification centre. A menu could afford more; a composer could not, and this is the number that
    made it acceptable.
  * a popup is addressed by a QUERY STRING, so it can only carry what survives one.

The second is why `_composeInWindow` is a gate with two real answers, and why compose_host_sim.js
runs it. Saying yes to something unserialisable is worse than the bug: a composer opened with
`files` would come up EMPTY, having silently dropped the picture somebody just chose. So files, a
community object, the article parents and an explicit open target all keep the in-page modal — which
works perfectly and is merely behind a window.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def _fn(decl: str, src: str = "") -> str:
    """The function's BODY. The brace scan starts after the parameter list, because `compose` takes a
    destructured object — its parameters are themselves wrapped in braces, and scanning from the
    first `{` returns the parameter list and nothing else."""
    src = src or OS_JS
    start = src.index(decl)
    depth, params_end = 0, start
    for j in range(src.index("(", start), len(src)):     # walk the parameter list
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
            if depth == 0:
                params_end = j
                break
    depth = 0
    for j in range(src.index("{", params_end), len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(decl)


def test_the_client_asks_a_host_before_drawing_the_modal():
    """THE HOOK. It has to be the first thing compose() does — after the modal is built there is
    nothing to hand over."""
    body = _fn("  function compose({reply=null", APP_JS)
    assert "__PC_COMPOSE_HOST" in body
    hook = body.index("__PC_COMPOSE_HOST")
    assert hook < body.index("modal(`<h3"), (
        "the host is consulted after the modal has already been built")


def test_the_host_exists_only_while_the_windowed_desktop_does():
    """Installed by enter(), removed by exit(). Anywhere else — a browser, the phone, Classic mode —
    the global is absent and compose() behaves exactly as it always has."""
    assert "window.__PC_COMPOSE_HOST = _composeInWindow" in _fn("  function enter(){")
    assert "__PC_COMPOSE_HOST" in _fn("  function exit(remember){"), (
        "leaving the desktop leaves the host installed, so the composer keeps opening windows")


def test_the_composer_window_does_not_close_when_you_click_away():
    """Every other popup is a menu and should. This one holds what somebody typed."""
    assert "STICKY_POPUPS = new Set(['compose'])" in MAIN.replace('"', "'")


def test_it_is_resizable_unlike_a_menu():
    body = MAIN[MAIN.index("ipcMain.handle('pc:popup:open'"):]
    body = body[:body.index("ipcMain.handle('pc:popup:close'")]
    assert "resizable: sticky" in body


def test_the_window_draws_the_clients_own_modal_layer():
    assert "function renderComposePopup(){" in OS_JS
    body = _fn("  function renderComposePopup(){")
    assert "PC().compose(" in body, "the compose window opens and draws no composer"
    assert "pcarg" in body, "the reply target never reaches the window"


def test_closing_the_modal_closes_the_window():
    """Otherwise cancelling a reply leaves an empty floating rectangle on the desktop."""
    body = _fn("  function renderComposePopup(){")
    assert "MutationObserver" in body and "window.close()" in body


def test_the_modal_layer_is_what_the_compose_popup_shows():
    """The general popup rule hides everything but the popup host; a composer has no host of its
    own because the composer is the CLIENT's, drawn into #modal-root. Written as a separate rule
    because `:not(#id)` carries an ID's specificity and no class-only rule can override it."""
    tight = CSS.replace(" ", "")
    assert ".os-popup-body:not(.os-popup-compose)>*:not(#os-popup-host){display:none!important}" in tight
    assert ".os-popup-body.os-popup-compose>*:not(#modal-root){display:none!important}" in tight


def test_the_gate_itself():
    """Runs `_composeInWindow` over every shape it is asked about — see the module docstring for
    why the refusals matter more than the acceptances."""
    done = subprocess.run(["node", "compose_host_sim.js", "../../static/js/client/os.js"],
                          cwd=ROOT / "tests/client", capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_window_fetches_the_post_it_is_replying_to():
    """MEASURED on the machine: the composer opened as "Reply" with no "Replying to" card above it.

    compose() renders that card from `Store.get(id)` when the modal is built, and in the desktop the
    event is already there — it is the thing the person clicked. This window is a FRESH page with a
    cold Store, so the card came out empty and the composer was "an empty box with no reminder of
    what you were answering", which is the exact regression the card was added to fix."""
    body = _fn("  function renderComposePopup(){")
    assert "Relay.query" in body, (
        "the compose window never fetches the post it is replying to, so the 'Replying to' card is "
        "empty on every reply opened this way")
    assert "Store.saveEvent" in body, (
        "the event is fetched and thrown away — Relay.query RESOLVES with events and stores none of "
        "them, and compose() reads Store.get(). Measured: the fetch worked and the card was still "
        "empty")


def test_the_fetch_is_bounded_and_never_costs_the_composer():
    """The reply is correctly tagged either way — the id and the author travelled in the argument —
    so a slow or dead relay must cost the PREVIEW, never the composer itself."""
    body = _fn("  function renderComposePopup(){")
    assert "setTimeout(once," in body, "a dead relay would leave an empty window for ever"
    assert "drawn" in body, "the composer could be drawn twice — once on timeout, once on arrival"


def test_a_post_already_in_the_store_is_not_waited_for():
    """A quote of something the page already holds must not pay a relay round trip."""
    body = _fn("  function renderComposePopup(){")
    assert "Store.get(need)" in body
