"""THE APP BUNDLES MUST SHIP EVERY STYLESHEET THE SHELL ASKS FOR.

Reported as "monero works fine on web, android is broken", after four fixes that were each a real
defect and none of them this one.

`templates/client.html` links three stylesheets. `mobile/build-www.sh` and `desktop/build-www.sh`
copied three files into the bundle — client.css, concord.css and rtl.css — and `monero-wallet.css`
was not among them. On the web that is invisible, because the server serves everything under
/static. In a bundle the fetch shim treats `/static/` as BUNDLE-LOCAL, so the request never reaches
the instance: it 404s against the bundle and the entire Monero wallet renders unstyled. Web perfect,
Android broken, with nothing in any log and no error on screen — the view is there, it just has no
CSS.

This is the third time this exact shape has been paid for in these two scripts, and both of the
earlier ones are commented in them:

  * `/static/fonts/*.woff2` — @font-face'd from INSIDE client.css, so the shim never sees the URL;
    a bundle without them silently drops the whole app to a system font.
  * `/static/i18n/*.json` — fetched at runtime by i18n.js; without them the language picker offers
    Arabic and Japanese and quietly falls back to English.

So the fix is not "add monero-wallet.css to the list". It is to stop keeping a list: the scripts
derive the stylesheets from the template itself. These tests hold that line — and they check the
PROPERTY (everything referenced is shipped), not the current filenames, so the next stylesheet is
covered before anyone thinks about it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT_HTML = (ROOT / "templates/client.html").read_text(encoding="utf-8")
BUNDLERS = {
    "mobile": (ROOT / "mobile/build-www.sh").read_text(encoding="utf-8"),
    "desktop": (ROOT / "desktop/build-www.sh").read_text(encoding="utf-8"),
}


def referenced_stylesheets() -> set[str]:
    """Every /static/css file the client shell links, as bare filenames."""
    return {href.rsplit("/", 1)[-1]
            for href in re.findall(r'href="(/static/css/[^"?]+)', CLIENT_HTML)}


def test_the_shell_references_the_stylesheets_this_file_thinks_it_does():
    """The check before the checks: if this cannot read the template, everything below passes
    vacuously — which is precisely how the original bug survived."""
    sheets = referenced_stylesheets()
    assert len(sheets) >= 3, f"only found {sheets} — the template's link tags have changed shape"
    assert "client.css" in sheets


@pytest.mark.parametrize("which", sorted(BUNDLERS))
def test_the_bundler_derives_its_stylesheets_from_the_template(which):
    """THE FIX, stated as the rule rather than as a filename. A hardcoded list is what went stale;
    reading the template is what cannot."""
    script = BUNDLERS[which]
    assert 'templates/client.html' in script, (
        f"{which}/build-www.sh no longer reads the shell template, so its stylesheet list is "
        f"hand-maintained again — that is the thing that went wrong")
    assert re.search(r"for _css in .*static/css.*client\.html", script), (
        f"{which}/build-www.sh does not loop over the template's stylesheets")


@pytest.mark.parametrize("which", sorted(BUNDLERS))
def test_a_missing_stylesheet_fails_the_build_instead_of_shipping_without_it(which):
    """A silently absent asset is the whole problem. `cp` failing must stop the build, not leave a
    bundle that looks fine and renders unstyled on somebody's phone."""
    script = BUNDLERS[which]
    loop = script[script.index("for _css in"):]
    loop = loop[:loop.index("done") + 4]
    assert "exit 1" in loop, (
        f"{which}/build-www.sh continues after a stylesheet fails to copy — the bundle ships "
        f"incomplete and the failure only shows up on a device")


@pytest.mark.parametrize("which", sorted(BUNDLERS))
def test_the_runtime_only_stylesheet_is_still_copied(which):
    """rtl.css is loaded by i18n.js at runtime and appears in no template, so the loop cannot see
    it. Deriving the list must not have dropped it — that would break right-to-left languages in
    the app while leaving them perfect on the web, which is this bug again in a different shirt."""
    assert "static/css/rtl.css" in BUNDLERS[which]


@pytest.mark.parametrize("which", sorted(BUNDLERS))
def test_the_assets_that_live_inside_a_stylesheet_are_still_copied(which):
    """The two earlier instances of this exact bug, kept honest. Neither URL appears in any
    template — the fonts are @font-face'd from inside client.css and the catalogues are fetched by
    script — so neither can ever be derived, and both must stay explicit."""
    script = BUNDLERS[which]
    assert "static/fonts" in script, "the bundle stopped copying the fonts client.css @font-face's"
    assert "static/i18n" in script, "the bundle stopped copying the translation catalogues"


def test_every_referenced_stylesheet_actually_exists_on_disk():
    """A template referencing a file nobody has is the same 404, one step earlier."""
    for sheet in referenced_stylesheets():
        assert (ROOT / "static/css" / sheet).is_file(), f"client.html links a missing {sheet}"
