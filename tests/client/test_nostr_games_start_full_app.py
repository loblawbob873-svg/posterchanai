"""The Nostr games start a game against their bot, through the real UI (2026-10-22: every screen gets a browser test).

The games are played THROUGH a bot: pressing start publishes a public kind-1 note tagging the bot, which then DMs
each player the board. What the client owns, and what a person sees break, is this: the screen draws its start
control and its "your games" list finishes loading, the press puts that start note on the wire -- kind 1, the bot in
a `p` tag, the game's hashtag -- and with no bot configured the screen says how to get one instead of looking broken.
Real login, Relay, DOM and the shipped game modules; only the network is a fixture.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

FEATURE_COVERS = ("ttt", "chess", "blackjack", "hangman", "holdem", "connect4")   # scripts/feature_coverage.py

GAMES = [
    # view, client-config key, start button, (kind, hashtag) of what the start sends to the bot. A table game played
    # alone (blackjack, hold'em) starts with a PRIVATE kind-30388 command; the others post a public kind-1 note.
    ("ttt", "ttt_bot_npub", "#ttt-play-bot", (1, "tictactoe")),
    ("chess", "chess_bot_npub", "#chess-play-bot", (1, "chess")),
    ("blackjack", "blackjack_bot_npub", "#bj-solo", (30388, "blackjackcmd")),
    ("hangman", "hangman_bot_npub", "#hm-play", (1, "hangman")),
    ("holdem", "holdem_bot_npub", "#hm-solo", (30388, "holdemcmd")),
    ("connect4", "connect4_bot_npub", "#c4-play-bot", (1, "connect4")),
]

RECORD = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__sentEvents=[];
const __gameSend=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){try{const m=JSON.parse(raw);if(m[0]==='EVENT'&&m[1])__sentEvents.push(m[1]);}catch(_){}return __gameSend.call(this,raw)};
'''


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize("view,key,button,tag", GAMES, ids=[g[0] for g in GAMES])
def test_pressing_start_puts_the_start_note_on_the_wire(view, key, button, tag):
    async def check(b):
        await desktop.login(b)
        bot = await b.js("(()=>{const sk=new Uint8Array(32).fill(9);return NostrTools.getPublicKey(sk)})()")
        await b.js(f"__PC.CFG[{json.dumps(key)}]=NostrTools.nip19.npubEncode({json.dumps(bot)}); __PC.switchView({json.dumps(view)}); 1")
        await b.until(f"!!document.querySelector({json.dumps(button)})")
        await b.until("!document.querySelector('.chess-games .spinner')")     # "your games" finished loading
        await b.js(f"document.querySelector({json.dumps(button)}).click(); 1")
        kind, hashtag = tag
        sent = f"__sentEvents.find(e=>e.kind==={kind}&&(e.tags||[]).some(t=>t[0]==='p'&&t[1]==={json.dumps(bot)}))"
        await b.until(f"!!{sent}")
        start = await b.js(sent)
        tags = [t[1] for t in start["tags"] if t[0] == "t"]
        assert hashtag in tags, (view, start["tags"])
        if kind == 30388:
            assert json.loads(start["content"]).get("action") == "start", start["content"]
            assert ["nofederate", "1"] in start["tags"], "a private game command was allowed to federate"
        assert start["sig"] and start["id"], "the start note went out unsigned"
    asyncio.run(desktop.with_browser('online', '', check, RECORD))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize("view,key,button,tag", GAMES, ids=[g[0] for g in GAMES])
def test_with_no_bot_the_screen_says_how_to_get_one(view, key, button, tag):
    async def check(b):
        await desktop.login(b)
        await b.js(f"delete __PC.CFG[{json.dumps(key)}]; __PC.switchView({json.dumps(view)}); 1")
        await b.until("/is configured on this server yet/.test(document.querySelector('#feed').innerText)")
        assert not await b.js(f"!!document.querySelector({json.dumps(button)})"), "a start button with no bot to start"
    asyncio.run(desktop.with_browser('online', '', check, RECORD))


# view, config key, (invite box, its results, Deal button), hashtag of the table note
TABLES = [("blackjack", "blackjack_bot_npub", ("#bj-inv", "#bj-inv-res", "#bj-deal"), "blackjack"),
          ("holdem", "holdem_bot_npub", ("#hm-inv", "#hm-inv-res", "#hm-deal"), "holdem")]


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize("view,key,ids,hashtag", TABLES, ids=[t[0] for t in TABLES])
def test_seating_a_friend_and_dealing_tags_them_in_the_table_note(view, key, ids, hashtag):
    """Seat somebody by npub, press "Deal table": one public note that tags the bot AND the friend."""
    async def check(b):
        await desktop.login(b)
        bot = await b.js("NostrTools.getPublicKey(new Uint8Array(32).fill(9))")
        friend = await b.js("NostrTools.getPublicKey(new Uint8Array(32).fill(5))")
        await b.js(f"__PC.CFG[{json.dumps(key)}]=NostrTools.nip19.npubEncode({json.dumps(bot)}); __PC.switchView({json.dumps(view)}); 1")
        inv, res, deal = ids
        await b.until(f"!!document.querySelector('{inv}')")
        assert await b.js(f"getComputedStyle(document.querySelector('{deal}')).display") == "none", "Deal shown with nobody seated"
        await b.js(f"(()=>{{const i=document.querySelector('{inv}');i.value=NostrTools.nip19.npubEncode({json.dumps(friend)});"
                   "i.dispatchEvent(new Event('input',{bubbles:true}));})(); 1")
        await b.until(f"!!document.querySelector('{res} [data-add]')")
        await b.js(f"document.querySelector('{res} [data-add]').click(); 1")
        await b.until(f"getComputedStyle(document.querySelector('{deal}')).display!=='none'")
        await b.js(f"document.querySelector('{deal}').click(); 1")
        sent = (f"__sentEvents.find(e=>e.kind===1&&(e.tags||[]).some(t=>t[0]==='t'&&t[1]==={json.dumps(hashtag)}))")
        await b.until(f"!!{sent}")
        ps = await b.js(f"{sent}.tags.filter(t=>t[0]==='p').map(t=>t[1])")
        assert bot in ps and friend in ps, ps
    asyncio.run(desktop.with_browser('online', '', check, RECORD))
