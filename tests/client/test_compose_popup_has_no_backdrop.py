"""A COMPOSER THAT IS ITS OWN WINDOW MUST NOT PAINT A BACKDROP INSIDE IT.

Reported as "Reply modal on desktop has a black box around it". On the desktop a reply opens
through `_composeInWindow`, which asks the compositor for a real window sized to the composer.
The markup inside is the ordinary modal: a `.modal-bg` backdrop with a `.modal` card centred in
it. On the web the backdrop is correct -- it dims the page the composer covers. In its own window
there is no page behind it, so `rgba(4,2,12,.72)` paints against the desktop as a hard dark frame,
and `.modal`'s `width:min(720px,96vw)` / `max-height:92vh` guarantees that frame is visible on
every edge.

Each assertion below is checked to fail with the override removed.
"""
from pathlib import Path
import re

CSS = (Path(__file__).resolve().parents[2] / "static/css/client.css").read_text(encoding="utf-8")


def _decls(selector):
    """Every declaration block belonging to `selector`, in source order."""
    out = []
    for match in re.finditer(re.escape(selector) + r"\s*\{([^}]*)\}", CSS):
        out.append((match.start(), match.group(1)))
    return out


def test_the_base_backdrop_really_is_opaque():
    """The reason the override has to exist. If this ever stops being true the rest is theatre."""
    base = _decls(".modal-bg")
    assert base, "no .modal-bg rule at all"
    body = base[0][1]
    assert "rgba(" in body and "background:" in body, body
    alpha = float(re.search(r"background:\s*rgba\([^)]*?,\s*([\d.]+)\s*\)", body).group(1))
    assert alpha > 0.5, f"backdrop alpha {alpha} — it no longer paints a dark frame"


def test_the_compose_window_clears_the_backdrop():
    hits = _decls(".os-popup-body.os-popup-compose #modal-root .modal-bg")
    assert hits, "the compose popup does not override .modal-bg"
    body = hits[-1][1]
    assert re.search(r"background:\s*(none|transparent)", body), body
    # The blur is a second painted layer and is just as visible over a transparent window.
    assert re.search(r"backdrop-filter:\s*none", body), body
    # Padding is what holds the card off the window edge, i.e. what makes the frame wide enough to see.
    assert re.search(r"padding:\s*0\b", body), body


def test_the_card_is_given_the_whole_window():
    hits = _decls(".os-popup-body.os-popup-compose #modal-root .modal")
    assert hits, "the compose popup does not size .modal to its window"
    body = hits[-1][1]
    assert "width:100%" in body.replace(" ", "") and "max-width:none" in body.replace(" ", ""), body
    assert "height:100%" in body.replace(" ", "") and "max-height:none" in body.replace(" ", ""), body


def test_the_override_wins_the_cascade():
    """Same specificity would still lose if it were written above the base rule."""
    base = _decls(".modal-bg")[0][0]
    override = _decls(".os-popup-body.os-popup-compose #modal-root .modal-bg")[-1][0]
    assert override > base, "the compose override is declared before the rule it overrides"
