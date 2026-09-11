"""#feed's full-height modifiers, and the view that could not be scrolled because one stayed on.

`#feed` is ONE element shared by every screen. Seven views put a modifier class on it — feed-chat,
feed-dm, feed-ai, feed-translate, feed-meme, feed-admin, feed-term — and every one of those is
`overflow:hidden` in client.css, because those screens own their own inner scroller. The class lives
on the element until something takes it off, so a normal scrolling view painted straight afterwards
renders perfectly and then refuses to move.

renderView() toggles all seven against the current VIEW, so anything routed through switchView() is
safe. The views that paint into #feed directly (a profile, the Music app, search, a thread) have to
clear it themselves, and they did that against a WRITTEN-OUT list of five. feed-admin and feed-term
were in none of the copies — so opening a profile from the Admin panel or the terminal drew the
header and the first post and clipped everything below it at the fold, on every tab, with nothing in
any log. ("Loaded my profile, see 1 post, then can't scroll down to see anything else. All profile
tabs show little bit.")

Adding the two names to the list would fix this instance and nothing else; the list had already been
extended twice for exactly this bug. So the check here is that the strip is DERIVED — it asks the
element which modifiers it is carrying — and that every modifier the app can set is in fact covered
by running the shipped helper against each one.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APPJS = os.path.join(ROOT, "static", "js", "client", "app.js")
CSS = os.path.join(ROOT, "static", "css", "client.css")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


APP = _read(APPJS)
STYLE = _read(CSS)


def _modifiers():
    """Every `feed-*` class the app puts on #feed, read from the code that sets them."""
    mods = set(re.findall(r"classList\.toggle\('(feed-[\w-]+)'", APP))
    mods |= set(re.findall(r"classList\.add\('(feed-[\w-]+)'", APP))
    return mods


def test_the_app_still_sets_the_modifiers_this_is_about():
    mods = _modifiers()
    # A sanity floor: if this drops to a handful the regexes above stopped matching and every
    # assertion below would pass while testing nothing.
    assert {"feed-ai", "feed-dm", "feed-admin", "feed-term"} <= mods, mods


def test_every_modifier_that_hides_overflow_is_one_a_scrollable_view_must_clear():
    """The ones that matter are the ones that stop the container scrolling. This is the list that
    grew past the hand-written copies — it is asserted to be non-trivial so the run below is real."""
    hides = set()
    for rule in re.findall(r"([^\n{}]*)\{([^{}]*)\}", STYLE):
        sel, body = rule
        if "overflow:hidden" not in body.replace(" ", ""):
            continue
        hides |= set(re.findall(r"\.(feed-[\w-]+)\b", sel))
    assert {"feed-admin", "feed-term"} <= hides, \
        "the two modifiers nobody's list had are supposed to be exactly the overflow-hiding ones"
    assert len(hides) >= 6, hides


def test_the_strip_is_derived_from_the_element_not_from_a_written_out_list():
    body = re.search(r"function _feedScrollable\(feed\)\{(.*?)\n  \}", APP, re.S)
    assert body, "_feedScrollable moved — re-point this test"
    body = body.group(1)
    assert "classList" in body and "feed-" in body, body
    # The failure this file exists for is a list that is missing a name. A list is what must not
    # come back — not `classList.remove('feed-ai','feed-chat',…)` anywhere in the file.
    assert not re.search(r"classList\.remove\('feed-[\w-]+'\s*,", APP), \
        "a hand-written modifier list is back; that is the bug, not the fix"


def test_the_views_that_paint_into_feed_directly_all_use_it():
    """A view that renders straight into #feed without going through switchView() inherits whatever
    the last one left there. These four are the ones that used to carry their own copy."""
    assert APP.count("_feedScrollable(") >= 6
    prof = APP[APP.index("async function renderProfileView(pk){"):]
    prof = prof[: prof.index("<div id=\"prof-list\">")]
    assert "_feedScrollable(feed);" in prof, "the profile no longer clears the modifiers"


@pytest.mark.skipif(not shutil.which("node"), reason="node is what runs the shipped helper")
def test_the_shipped_helper_strips_every_modifier_the_app_can_set():
    """Not a grep: the real function, against a stand-in element carrying each modifier in turn.

    A profile opened from the Admin panel arrives with `class="feed feed-admin"`; the assertion is
    that what comes back is a plain `.feed`, whichever of the seven it was — and that the base class
    survives, since removing THAT would break the scrolling in the other direction."""
    # Any `const _FEED_…` immediately above comes along, so a list-based implementation runs here and
    # fails on the ASSERTION below rather than on a ReferenceError — a test that only ever fails by
    # crashing does not tell you what is wrong.
    body = re.search(r"((?:const _FEED_\w+ = [^\n]*\n\s*)?function _feedScrollable\(feed\)\{.*?\n  \})",
                     APP, re.S)
    assert body, "_feedScrollable moved — re-point this test"
    mods = sorted(_modifiers())
    script = """
      const $ = () => null;
      %s
      const el = (classes) => {
        const set = new Set(classes);
        return { classList: {
          remove: (...c) => c.forEach(x => set.delete(x)),
          [Symbol.iterator]: () => set[Symbol.iterator](),
          get length(){ return set.size; },
        }, _set: set };
      };
      const out = {};
      for (const m of %s) {
        const e = el(['feed', m]);
        _feedScrollable(e);
        out[m] = [...e._set];
      }
      console.log(JSON.stringify(out));
    """ % (body.group(1), json.dumps(mods))
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    for mod in mods:
        assert got[mod] == ["feed"], f"{mod} survived the strip: {got[mod]}"


# ───────────────── the hole the prefix rule had, and how it came back ─────────────────────────────

OS_JS = open(os.path.join(ROOT, "static/js/client/os.js"), encoding="utf-8").read()
CSS_ALL = open(os.path.join(ROOT, "static/css/client.css"), encoding="utf-8").read()


def _feed_class_tokens():
    """Every class this client puts on #feed, read from the code that ASSIGNS it.

    The enumeration above greps for `classList.toggle('feed-…')`, so it can only ever find classes
    that already carry the prefix — it is blind to a whole className being written at once. That is
    exactly how four modifiers came to exist that the strip cannot see."""
    found = set()
    for src in (APP, OS_JS):
        # `className = 'feed something-else'`
        for literal in re.findall(r"""className\s*=\s*['"]feed([^'"]*)['"]""", src):
            found |= {t for t in literal.split() if t}
        # `_paintExtraInFeed('something-else', …)` — the class arrives as an argument
        found |= set(re.findall(r"_paintExtraInFeed\(\s*['\"]([\w-]+)['\"]", src))
    return found


def test_every_class_put_on_feed_can_be_taken_off_again():
    """THE REGRESSION, RETURNING A THIRD TIME — reported as "i opened My Profile and can't scroll,
    why is this regression returning again!".

    `_feedScrollable()` clears modifiers by scanning #feed's classList for the `feed-` PREFIX. That
    is the right shape and it is why a written-out list was removed. But four full-height modifiers
    were named the other way round — `os-settings-feed`, `os-taskmgr-feed`, `os-vms-feed`,
    `os-remote-feed` — so the scan could not see any of them, and every one is
    `overflow:hidden!important`. Open System Settings (or Task Manager, or Virtual Machines) and
    then a profile, and the profile draws and refuses to move, exactly as before.

    A prefix rule only works if everything obeys the prefix, so that is what this asserts — from the
    code that sets the class, not from a list anybody has to remember to extend."""
    tokens = _feed_class_tokens()
    assert tokens, "re-point this test: nothing appears to assign a class to #feed any more"
    bad = sorted(t for t in tokens if not t.startswith("feed-"))
    assert not bad, (
        "these classes are put on #feed but cannot be stripped by `_feedScrollable`, which scans for "
        "the `feed-` prefix: %s. Every one of them is a full-height modifier, so the next view that "
        "paints into #feed is clipped at the fold with no way to scroll." % ", ".join(bad))


def test_those_modifiers_really_are_the_dangerous_kind():
    """Proof this test is about something: each of those classes hides overflow, which is what makes
    a leftover one cost the next view its scrolling. If they were harmless the rule above would be
    bureaucracy."""
    for cls in _feed_class_tokens():
        at = CSS_ALL.find(".%s{" % cls)
        if at < 0:
            continue                       # a modifier whose rule lives in another sheet
        rule = CSS_ALL[at:CSS_ALL.index("}", at)]
        if "overflow:hidden" in rule:
            assert cls.startswith("feed-"), cls
            break
    else:
        raise AssertionError("no #feed modifier hides overflow — re-read this file, the hazard moved")
