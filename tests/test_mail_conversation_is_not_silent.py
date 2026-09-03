"""THE CONVERSATION IS FOUND — THE READER JUST NEVER SAID IT WAS LOOKING.

Reported roughly ten times, most recently "i asked you probably 10 times to fix email right about
including the sent conversation in the thread view".

Each earlier round fixed something real (sent-folder detection, the 5000-doc scan cap, the seed that
could not be found in one page, a cache keyed on `id(sk)` that never hit, a headerless seed
short-circuited to a singleton). Measured after all of them, on the reporting mailbox:

    my own sent mail, newest 60 opened : 56 of 60 DO show a conversation
    newest 60 inbox/archive opened     : 13 contain my sent mail, 35 are genuinely one message

So the data was arriving. What was not arriving was any indication of it. The client paints the
opened message immediately and upgrades when `/thread` answers — and that call walks the whole
mailbox:

    COLD scan : 11.0s   (17,921 documents of NIP-44 decrypts)
    WARM scan : 0.000s  (60s cache)

Eleven seconds of a reader showing exactly one message and saying nothing, on every first open after
a restart — and this session restarted the service repeatedly. The only conclusion available to
somebody looking at it is that their sent mail is missing.

Two changes, and neither touches the threading rules: the reader says what it is doing, and the scan
is warmed while the message LIST is on screen so opening a message rarely pays for it.
"""
from __future__ import annotations

import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def test_the_reader_says_it_is_still_looking():
    """THE REPORT. A single message with no explanation is indistinguishable from a broken one."""
    assert "Loading the rest of this conversation…" in APP, (
        "the reader shows one message and says nothing for the ~11s the thread scan takes")


def test_it_says_so_when_there_is_genuinely_nothing_else():
    """35 of 60 opened messages really are one message. Leaving 'Loading…' up for ever would be the
    same lie in the other direction."""
    assert "No other messages in this conversation" in APP


def test_a_failed_thread_call_stops_claiming_to_be_loading():
    block = APP[APP.index("this.api('/thread?account="):]
    block = block[:block.index("_linkify(")]
    assert "could not be loaded" in block
    assert ".mail-convo-state" in block, (
        "a failed conversation fetch leaves 'Loading…' on screen for ever")


def test_a_one_message_answer_still_re_renders():
    """The seed is painted with 'loading'; if the answer is 'there is nothing else', the reader has
    to be redrawn to say so — returning early would strand the spinner text."""
    # The first paint may contain locally cached siblings, but it always remains in the loading
    # state until the authoritative thread request answers.
    block = APP[APP.index("this._renderThread(pane, _local, folder, acct, uid,"):]
    block = block[:block.index("_linkify(")]
    assert "'alone'" in block


# ── the warm ─────────────────────────────────────────────────────────────────────────────────────

def test_the_scan_is_warmed_from_the_list_view():
    """Somebody is already sitting in the list before they open anything; that is where the 11s
    belongs, not in front of the message they just clicked."""
    from app.routers import mail as M
    src = inspect.getsource(M.mail_messages)
    assert "_warm_thread_scan(" in src, "the message list no longer warms the thread scan"


def test_the_warm_is_never_awaited():
    """The list must not wait on an 11-second scan; a cold cache is the only cost of it failing."""
    from app.routers import mail as M
    src = inspect.getsource(M.mail_messages)
    assert "await _warm_thread_scan" not in src


def test_one_warm_at_a_time_per_mailbox():
    """The list fires this on every page. Without the guard a fast scroll starts a dozen concurrent
    11-second scans of the same mailbox on a single-worker node."""
    from app.routers import mail as M
    src = inspect.getsource(M._warm_thread_scan)
    assert "_WARMING" in src and "done()" in src


def test_an_already_warm_cache_is_not_rescanned():
    from app.routers import mail as M
    src = inspect.getsource(M._warm_thread_scan)
    assert "_THREAD_SCAN.get(key)" in src and "_THREAD_SCAN_TTL" in src


def test_the_warm_cannot_raise_into_the_request():
    """No running loop (a sync context, a test) must not 500 the message list."""
    from app.routers import mail as M
    M._warm_thread_scan(b"\x01" * 32, 1, None)      # no event loop here — must simply return


def test_the_warm_keys_match_the_cache_keys():
    """A warm under a different key fills a cache nobody reads — the `id(sk)` bug wearing a hat."""
    from app.routers import mail as M
    warm = inspect.getsource(M._warm_thread_scan)
    scan = inspect.getsource(M._thread_scan)
    assert '(user_id, account_email or "*")' in warm
    assert '(user_id, account_email or "*")' in scan
