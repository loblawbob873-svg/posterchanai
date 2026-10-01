"""An EAGER module's dependencies exist when it is built.

timeline.js is built at boot, where its block used to run in app.js. Its factory destructures `dep` at
that moment, so any dependency app.js declares with const/let/class FURTHER DOWN is read inside its
temporal dead zone and the whole client fails to start: "Uncaught ReferenceError: Cannot access
'CMP_BGS' before initialization", a blank page on every device. Six such names (CMP_BGS, Drafts,
InstEmoji, MusicOffline, Scheduled, _BG_WORDS) reached it. They are read through `dep.state` getters.
Function declarations are hoisted and always fine.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_name_timeline_js_destructures_is_initialised_before_it_is_built():
    app = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
    tl = (ROOT / "static/js/client/timeline.js").read_text(encoding="utf-8")
    build = app[:app.index("  if(!_timelineMod()){")].count("\n") + 1
    names = [n.strip() for n in re.search(r"const \{\s*\n(.*?)\n\s*\} = dep;", tl, re.S).group(1)
             .replace("\n", " ").split(",") if n.strip()]
    assert len(names) > 20, names
    late = []
    for n in names:
        for i, line in enumerate(app.split("\n"), 1):
            if re.match(r"\s*(?:const|let|var|class)\s+" + re.escape(n) + r"\b", line):
                if i > build:
                    late.append((n, i))
                break
    assert not late, ("declared below the point timeline.js is built (line %d) -- pass a getter in "
                      "dep.state instead" % build, late)
