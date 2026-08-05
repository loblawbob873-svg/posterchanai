"""`isReply` — a quote is not a reply, and the client used to say it was.

    venv-unified/bin/python -m pytest tests/test_client_is_reply.py

node runs the REAL function, extracted from static/js/client/app.js rather than retyped, so this
tests the shipped code instead of a second copy of my assumptions about it. Same approach as
test_joplin_import.py and test_client_qr_encoder.py; app.js is one big browser IIFE, and this
function is pure, so pulling its source out and evaluating it is enough — no DOM, no stubs.

THE BUG. It was `ev.kind===1 && ev.tags.some(t => t[0]==='e')` — ANY e-tag means a reply. But NIP-10
gives an e-tag a MARKER, and `mention` means "I am pointing at this note", not "I am answering it".
So a quote post made the older way (an e-tag marked `mention` plus the inline `nostr:nevent`) was
classified as a reply, and feedNoteHtml put a "↩ replying to …" header above it — while the body
embedded that same note AGAIN as a quote card. The referenced note appeared twice in one card. Many
clients still quote this way, so it was constant.

The fixture below is a real note off the relay:
nevent1qqsd0rpu5lv6rj6a88cz3tc0zfj4f9j58w8dsf90k5qxzdyy53pws2qrke75x

It also decides more than the header: "hide replies" filtering, whether a notification reads
"replied" or "mentioned", and the split between a profile's Notes and Replies tabs.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

QUOTED = "14b699a72f6291e986cd52eba7672bab3006c0634f43ce83a051ccec1c68a1bb"
OTHER = "aa" * 32


def _fn_source():
    """The real `function isReply(ev){…}` out of app.js, brace-balanced."""
    src = open(APP, encoding="utf-8").read()
    i = src.index("function isReply(ev)")
    j = src.index("{", i)
    depth, k = 0, j
    while True:
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                break
        k += 1
    body = src[i:k + 1]
    assert "tags.some" in body or "es.some" in body, "did not capture the function body"
    return body


def _is_reply(ev):
    js = _fn_source() + "\nconsole.log(JSON.stringify(isReply(%s)));" % json.dumps(ev)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _ev(tags, kind=1):
    return {"kind": kind, "tags": tags, "content": "", "id": "ff" * 32, "pubkey": "bb" * 32}


def test_the_real_note_that_rendered_twice():
    """The note from the report: an e-tag marked `mention` plus the inline nostr:nevent. A QUOTE."""
    ev = _ev([["e", QUOTED, "", "mention"],
              ["p", "65d4aea7976e634f983e3dd2f17a71726e023269faa7605dd7691b9da6652273", "", "mention"],
              ["imeta", "url https://blossom.primal.net/52888b10.png", "m png"]])
    assert _is_reply(ev) is False, "a `mention` e-tag is a quote — the ↩ header double-rendered it"


@pytest.mark.parametrize("tags,want,why", [
    ([], False, "no e-tag at all is a top-level post"),
    ([["e", OTHER, "", "reply"]], True, "marked reply"),
    ([["e", OTHER, "", "root"]], True, "marked root"),
    ([["e", OTHER, "", "mention"]], False, "marked mention is a quote"),
    ([["e", OTHER]], True, "unmarked is the deprecated positional form, which does mean a reply"),
    ([["e", OTHER, ""]], True, "positional with an empty relay hint is still a reply"),
    # a real thread reply also carries the root; one marked reply is enough
    ([["e", OTHER, "", "root"], ["e", QUOTED, "", "reply"]], True, "root + reply"),
    # NIP-18: the modern quote carries BOTH a q tag and an unmarked e-tag for the same event
    ([["q", QUOTED], ["e", QUOTED]], False, "an unmarked e-tag naming the q-tagged event is the quote"),
    # …but a reply that ALSO quotes something else is still a reply
    ([["q", QUOTED], ["e", OTHER, "", "reply"]], True, "quoting does not stop it being a reply"),
    ([["q", QUOTED], ["e", QUOTED], ["e", OTHER]], True, "the OTHER unmarked e-tag is the parent"),
    # mention alongside a genuine reply must not suppress it
    ([["e", QUOTED, "", "mention"], ["e", OTHER, "", "reply"]], True, "a mention beside a real reply"),
])
def test_markers(tags, want, why):
    assert _is_reply(_ev(tags)) is want, why


def test_only_kind_1():
    assert _is_reply(_ev([["e", OTHER, "", "reply"]], kind=6)) is False
    assert _is_reply(_ev([["e", OTHER, "", "reply"]], kind=7)) is False


def test_malformed_tags_do_not_throw():
    """This runs while building every note in the timeline; a throw here blanks the feed."""
    for tags in ([["e"]], [["e", ""]], [[]], [["e", OTHER, None, None]], "nope"):
        ev = _ev(tags)
        assert _is_reply(ev) in (True, False)
