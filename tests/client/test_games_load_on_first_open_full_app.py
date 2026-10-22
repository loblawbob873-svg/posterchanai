"""The six game modules are fetched when a game is opened, not on every cold load.

2026-10-21: the boot payload crossed its ceiling (tests/test_client_boot_payload_budget.py), and chess/ttt/hangman/
connect4/blackjack/holdem (~93 KB) were <script> tags nothing reads until the game's own view opens. They now load
through renderModuleView. Real client: none of them is on the page after boot, and pressing each game's sidebar
row draws that game's hub with no error -- the first open, where a lazy module either arrives or strands a spinner.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

GAMES = [("chess", "PCChess"), ("ttt", "PCTtt"), ("hangman", "PCHangman"),
         ("connect4", "PCConnect4"), ("blackjack", "PCBlackjack"), ("holdem", "PCHoldem")]
NO_OS = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_each_game_loads_when_its_row_is_pressed():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 1280, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
        await desktop.login(b)
        got["at_boot"] = await b.js("[" + ",".join(f"!!window.{g}" for _, g in GAMES) + "]")
        got["tags"] = await b.js("[...document.scripts].map(s=>s.src).filter(s=>/\\/(chess|ttt|hangman|connect4|blackjack|holdem)\\.js/.test(s)).length")
        for view, glob in GAMES:
            await b.js(f"document.querySelector('.nav-item[data-view=\"{view}\"]').click()")
            await b.until(f"__PC.VIEW==='{view}' && !!window.{glob} && !!document.querySelector('#feed .chess-hub')")
            got[view] = await b.js("({hub:!!document.querySelector('#feed .chess-hub'),"
                                   "broken:/Could not open this app/.test(document.querySelector('#feed').textContent)})")
    asyncio.run(desktop.with_browser("online", "", check, NO_OS))
    assert got["at_boot"] == [False] * len(GAMES), ("a game module was loaded at boot", got)
    assert got["tags"] == 0, got
    for view, _ in GAMES:
        assert got[view] == {"hub": True, "broken": False}, (view, got)
