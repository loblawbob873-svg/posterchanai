"""THE GAME BOTS, RUNNING FOR REAL, next to a real node (tests/client/realnode_harness.py).

The Nostr games (chess, Connect 4, tic-tac-toe, Hangman, Blackjack, Hold'em) are refereed by a bot: the client
posts a start note or a private command, the bot keeps the game as a kind-30388 document, DMs the player to act,
and reads moves back as NIP-17 DMs (or, for the table games, a kind-30388 command channel). So a flow test that
stops at the client tests half a game. Here the bot is the SHIPPED `botframework/main.py --ttt --chess ...`, the
process the bot manager starts, with the environment the manager gives it (app/services/bot_manager_service.py:
NOSTR_RELAYS = this node's relay, NOSTR_MEDIA_ENDPOINT = this node's Blossom), against the node's real RelayServer.
Only its poll interval is shortened.

The bot writes its dedup files (`.processed_*`) beside its own source, so it runs from a private directory of
symlinks to botframework/ -- the worktree never collects them -- and PCAI_BOT_STATE_DIR points there too.
`PC_GAMEBOT_OVERRIDE=<file>=<path>,...` swaps one bot file for another copy (the fail-first runs), as
`PC_JOURNEY_OVERRIDE` does for a client file.

    with R.Node(cfg=..., members=[alice, bob, gamebot_harness.BOT_PK]) as n, Bots(n, 'ttt', 'connect4'):
        ...   # every listed game's `<game>_bot_npub` is in /client/config before a client loads
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path

from tests.client import realnode_harness as R

ROOT = R.ROOT
BOT_FILL = 66                         # the bot's secret key = bytes([66])*32 -- no client account uses it
BOT_PK = R.pubkey(BOT_FILL)
GAMES = ('chess', 'ttt', 'hangman', 'connect4', 'blackjack', 'holdem')
_POLL = {g: g.upper() + '_POLL_SECONDS' for g in GAMES}


class Bots:
    """`with Bots(node, 'ttt', ...)`: the shipped bot process running those games against `node`."""

    def __init__(self, node: R.Node, *games: str, poll: int = 1):
        assert games and all(g in GAMES for g in games), games
        self.node, self.games, self.poll = node, games, poll

    def __enter__(self):
        from app.services.nostr import nostr_service
        npub = nostr_service.npub_of(BOT_PK)
        for g in self.games:
            R.NODE['config'][g + '_bot_npub'] = npub
        self.dir = tempfile.mkdtemp(prefix='pc-gamebot-')
        bf = Path(self.dir) / 'botframework'
        bf.mkdir()
        # PC_GAMEBOT_OVERRIDE=holdemListener.py=/path/to/older.py,... runs another copy of one bot file (fail-first)
        swap = dict(p.split('=', 1) for p in filter(None, os.environ.get('PC_GAMEBOT_OVERRIDE', '').split(',')))
        for f in (ROOT / 'botframework').iterdir():
            if f.name.startswith('.') or f.name == '__pycache__':
                continue
            src = Path(swap.get(f.name, f)).resolve()
            if f.name == 'main.py':
                # COPIED, not linked: Python resolves a symlinked SCRIPT to its real directory for sys.path[0], which
                # would import every listener from the worktree (and write their dedup files there) instead of here.
                shutil.copyfile(src, bf / f.name)
            else:
                (bf / f.name).symlink_to(src)
        env = {k: v for k, v in os.environ.items() if not k.lower().endswith('_proxy')}
        env.update({
            'NOSTR_NSEC': bytes([BOT_FILL]).hex() * 32,
            'NOSTR_RELAYS': self.node.home.url,
            'NOSTR_MEDIA_SERVICE': 'blossom',
            'NOSTR_MEDIA_ENDPOINT': self.node.origin + '/blossom',
            'PCAI_BOT_STATE_DIR': self.dir,
            'PYTHONPATH': os.pathsep.join([str(ROOT), str(bf)]),
            'PYTHONUNBUFFERED': '1',
            'NO_PROXY': '127.0.0.1,localhost',
            'NOSTR_POLL_SECONDS': str(self.poll),
        })
        for g in self.games:
            env[_POLL[g]] = str(self.poll)
        self.log = open(Path(self.dir) / 'bot.log', 'w+')
        self.proc = subprocess.Popen([sys.executable, str(bf / 'main.py')] + ['--' + g for g in self.games],
                                     cwd=str(bf), env=env, stdout=self.log, stderr=subprocess.STDOUT)
        return self

    def output(self) -> str:
        try:
            self.log.flush()
            return Path(self.dir, 'bot.log').read_text(errors='replace')
        except OSError:
            return ''

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if exc and exc[0] is not None:
            sys.stderr.write('---- game bot output (tail) ----\n' + self.output()[-6000:] + '\n')
        self.log.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def state(self, game_prefix: str, root: str) -> dict | None:
        """The bot's current state document for one game, read off the node's relay like any client."""
        evs = self.node.home.query({'authors': [BOT_PK], 'kinds': [30388], '#d': [game_prefix + root]})
        evs.sort(key=lambda e: e['created_at'], reverse=True)
        return json.loads(evs[0]['content']) if evs else None
