"""Files → Blossom can forget a synced folder's shared record.

Removing a folder from every device left its manifest behind: it is keyed on the NAME, not on any
device, so the pair went on existing with all of its history. It showed up in the account list as
"🔄 Pictures · 0 files" — every path a tombstone — and any device that later paired that name
inherited it. The only escape was a name nobody had used, which is not an answer, and it was asked
for three times before it was built.

It WIPES the document rather than tombstoning its contents. Tombstones are exactly what makes a
record poisonous — they say "these files were deleted", for ever, to everyone who joins later. An
empty document says nothing, which is the truth about a folder nobody syncs.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNC = os.path.join(ROOT, "static", "js", "client", "sync.js")
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
CSS = os.path.join(ROOT, "static", "css", "client.css")


def _src(p):
    return open(p, encoding="utf-8").read()


def _fn(src, name):
    at = src.index("async %s(key)" % name)
    i = src.index("{", at)
    depth = 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return src[at:i + 1]


def test_forget_wipes_the_document_rather_than_tombstoning_it():
    body = _fn(_src(SYNC), "forget")
    assert "manifest: {}" in body, \
        "it writes something other than an empty document — tombstones are what poison a record"
    assert "drop(" not in body, "it tombstones the paths, which is the state being escaped from"
    assert "removed:" in body, \
        "no `removed` count, so the server's collapse guard will refuse the write"


def test_it_does_nothing_when_there_is_nothing_to_forget():
    body = _fn(_src(SYNC), "forget")
    assert "if(!all.length) return" in body, "an empty record is rewritten for no reason"


def test_the_button_exists_and_says_what_it_does_not_do():
    app = _src(APP)
    assert "data-syncforget=" in app, "there is no button in Files → Blossom"
    # The markup and the handler live apart, so anchor on the HANDLER — the markup alone is a button
    # that does nothing.
    at = app.index("'.fx-syncx[data-syncforget]'")
    block = app[at:at + 2600]
    assert "uiConfirm" in block, "it forgets a folder without asking"
    assert "No file is deleted anywhere" in block, \
        "the dialog does not say the one thing people need to know before pressing it"
    assert "still syncing" in block, \
        "it does not warn about the one case where this is the wrong action"
    assert "stopPropagation" in block, \
        "clicking the ✕ also opens the folder underneath it"


def test_the_button_is_styled_so_it_is_not_an_invitation():
    css = _src(CSS)
    assert ".fx-syncx" in css and ".fx-syncwrap" in css, "the control has no styling at all"
    assert re.search(r"\.fx-syncx\{[^}]*opacity:\.45", css), \
        "a destructive control should be quiet until hovered"
