"""`Relay.subscribe(filters, {onEvent, ...})` -- never a bare callback.

relay.js destructures the second argument (`{ onEvent, onEose, live } = {}`), so a function passed there is silently
dropped: the REQ goes out, events arrive, and nothing is ever called. The terminal's cross-device history did exactly
that from the day it shipped -- every terminal published its commands and none ever heard another's (found
2026-10-22 by tests/client/test_terminal_clears_the_bottom_nav_full_app.py). This reads every call in the client.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _second_argument(src: str, start: int) -> str | None:
    depth, j = 0, start
    while j < len(src):
        c = src[j]
        if c in '([{':
            depth += 1
        elif c in ')]}':
            if depth == 0:
                return None
            depth -= 1
        elif c == ',' and depth == 0:
            return src[j + 1:j + 80].lstrip()
        j += 1
    return None


def callback_calls(src: str):
    for m in re.finditer(r'\bRelay\.subscribe(?:From)?\(', src):
        start = m.end()
        if src[m.start():m.end()].endswith('From('):        # subscribeFrom(urls, filters, opts): skip the urls
            nxt = _second_argument(src, start)
            if nxt is None:
                continue
            start = src.index(nxt, start)
        second = _second_argument(src, start)
        if second and re.match(r'(async\s*)?(\(|function\b|[A-Za-z_$][\w$]*\s*=>)', second):
            yield src.count('\n', 0, m.start()) + 1, second[:40]


def test_no_relay_subscription_is_handed_a_bare_callback():
    bad = [f"{f.relative_to(ROOT)}:{line}: {snippet!r}"
           for f in sorted((ROOT / 'static/js/client').glob('*.js'))
           for line, snippet in callback_calls(f.read_text(encoding='utf-8'))]
    assert not bad, ('Relay.subscribe takes {onEvent, ...}; a bare function is silently ignored', bad)


def test_the_scan_finds_the_shape_it_guards_against():
    assert list(callback_calls("Relay.subscribe([{kinds:[1]}],\n  async (ev) => { use(ev); });"))
    assert list(callback_calls("Relay.subscribe(f, ev => x(ev))"))
    assert not list(callback_calls("Relay.subscribe([{kinds:[1]}], { onEvent: async (ev) => {} })"))
    assert not list(callback_calls("Relay.subscribeFrom(urls, [{kinds:[1]}], { onEvent })"))
