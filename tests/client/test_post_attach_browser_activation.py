from pathlib import Path


APP = (Path(__file__).parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")


def _compose():
    start = APP.index("function compose({reply=")
    end = APP.index("// ---------- Blossom uploads", start)
    return APP[start:end]


def test_the_browser_file_chooser_never_depends_on_a_programmatic_click():
    """Firefox must retain the trusted click needed to open its native file chooser.

    This used to be spelled "no popover at all on the web" — `input.click()` straight from the
    paperclip. That kept the chooser working and silently cost the OTHER half of the button: 📁
    Files was an item in the menu that no longer opened. Reported as "you broke Reply modal? no
    more attach from Files" and then, when it was pointed at the ⋯ menu, "it is missing if I can't
    see it and it defaults to local files!".

    The property that actually matters is narrower than "no menu": the chooser must not be opened
    BY SCRIPT from inside a popover callback, because `openMenuPopover` closes the menu first and
    Firefox distrusts the click that follows. A <label> bound to the input has no such problem —
    the browser opens the chooser as the label's own default action, with no JS in the path. So the
    menu is back and the assertion is on the mechanism."""
    body = _compose()
    handler = body[body.index("$('#cmp-attach',root).onclick"):]
    handler = handler[:handler.index("attachEmojiAutocomplete(ta)")]
    assert "{htmlFor:'cmp-file'}" in handler, (
        "the web device item no longer binds a label to the file input, so it is back to a "
        "programmatic click that Firefox will refuse")
    assert "$('#cmp-file',root).click(); return; }" not in handler, (
        "the paperclip short-circuits to the device chooser again, which is what removed Files")


def test_a_label_item_is_not_closed_before_the_browser_acts_on_it():
    """The half that makes the label work. `close()` removes the popover, and a detached label has
    no default action left to run — so the menu must be torn down AFTER, not before."""
    pop = APP[APP.index("function openMenuPopover("):]
    pop = pop[:pop.index("\n  }")]
    assert "if(b.tagName === 'LABEL'){ setTimeout(close, 0); return; }" in pop
    assert "extra && extra.htmlFor" in pop, "openMenuPopover can no longer render a label item"


def test_existing_files_remain_available_from_composer_more_menu():
    body = _compose()
    assert "items.push(['files','📁 Attach from Files'])" in body
    assert "if(a==='files'){ blossomPicker(ta); return; }" in body


def test_the_reply_composer_offers_files_too():
    """THE REPORT: "you broke Reply modal? no more attach from Files, aka blossom".

    It was not removed — the browser paperclip stopped opening a menu at all (the fix above, which
    is real: the popover cost Firefox the trusted click and Attach did nothing), and Files moved to
    the ⋯ menu. But ⋯ is built once for every composer, and the reply/quote/community variants only
    ever DROP controls (`${(reply||quote||...)?'':...}` around Poll and Background). Files is pushed
    unconditionally, so a reply has it — and this asserts that rather than trusting the ternaries to
    stay that way."""
    body = _compose()
    menu = body[body.index("const mb=$('#cmp-more',root)"):]
    menu = menu[:menu.index("🧹 Clean links — its OWN button")]
    push = "items.push(['files','📁 Attach from Files'])"
    assert push in menu
    # Nothing may make it conditional on the composer's mode, which is how Poll and Background go.
    line = [l for l in menu.splitlines() if push in l][0]
    assert "reply" not in line and "quote" not in line and "community" not in line, (
        "Attach from Files became conditional on the composer variant, so a reply loses it: " + line)


def test_the_overflow_button_says_what_is_behind_it():
    """The whole of this report was discoverability: Files left the paperclip and the only route to
    it was a ⋯ labelled "More". A control that is the sole path to a feature has to name it."""
    assert 'id="cmp-more" title="More — attach from Files' in APP, (
        "the ⋯ tooltip no longer mentions Files, which is the only place it can be reached from")


def test_the_file_input_id_the_label_points_at_is_unique():
    """The label lives on <html> (the popover is appended there, deliberately — see openMenuPopover)
    while the input is inside the modal, so they are bound by ID across the whole document. A second
    element carrying `cmp-file` would silently aim the chooser at the wrong input, and the symptom
    would be a picker that opens and attaches nothing."""
    assert APP.count('id="cmp-file"') == 1, (
        "more than one element is emitted with id=cmp-file; the attach label binds by id")
    assert 'type="file" id="cmp-file" multiple hidden' in APP, (
        "the composer's file input changed shape — check the label still names it")
