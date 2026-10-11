"""EVERY COMMAND A MEMBER CAN TYPE, SENT TO THE BOT -- AND THE BOT RUNS THAT FEATURE OR NONE, NEVER ANOTHER.

2026-10-11: the operator typed `musicgeni <a song>` to the PosterChan bot in a Concord room and got an
IMAGE. The bot has no musicgeni; its dispatchers asked `"geni" in text`, and "musicgeni" contains "geni".
tests/test_every_bot_command.py could not see it: its table lists the commands the BOT knows, and the bug
lives in the commands it does NOT know -- which is everything else in the app.

So this table is the APP's own catalogue (CommandService.COMMANDS + COMMAND_ALIASES, read at run time, so
a new command is covered the moment it exists) plus words that merely CONTAIN a command, each sent on the
timeline and in a room. For every bot backend that ran, the typed word must be one that backend belongs to.
A command the bot does not have may go to the model or say so; it may not run something else.
"""
import pytest

from tests.test_every_bot_command import bot, _nostr, _room, _backends   # noqa: F401  (bot is a fixture)
from app.services.command_service import CommandService
from bot_commands import MEDIA_COMMANDS

# Which typed command words each tool backend may answer. smart_search and the model are left out on
# purpose: a question can legitimately reach the web search, and anything at all may reach the model.
OWNS = {
    "geni": {"geni"},
    "search_and_download_images": {"images"},
    "fetch_news_from_source": {"news"},
    "capture_screenshot": {"screenshot", "shot", "ss"},
    "fetch_ytdl_media": {"ytdl"},
    "process_media": set(MEDIA_COMMANDS),
    "narration_video": {"/narrate"},
    "speech": {"/narrate"},
    "generate_song": {"musicgeni"},
}

WORDS = sorted(set(CommandService.COMMANDS) | set(CommandService.COMMAND_ALIASES)
               | {"genius", "ingenious", "imagesque", "newsletter", "screenshots"})


@pytest.mark.parametrize("word", WORDS)
@pytest.mark.parametrize("where", ["nostr", "concord"])
def test_the_bot_never_runs_a_feature_you_did_not_ask_for(bot, where, word):
    text = f"{word} a quiet song about the sea"
    said = _nostr(bot, text) if where == "nostr" else _room(bot, text)[0]
    wrong = [b for b in _backends(bot) if b in OWNS and word not in OWNS[b]]
    assert not wrong, f"{where}: typing {word!r} ran {wrong} -- a different feature than the one asked for"
    assert said, f"{where}: {word!r} got no answer at all"
