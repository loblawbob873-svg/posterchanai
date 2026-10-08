"""Telegram's picture-in, effect-out buttons are one table (_IMAGE_EFFECT_BUTTONS), not 78 copied branches.

Checked against the old branches for every button (with pictures, with only a text file) and identical; this
drives the real callback handler for each row the way a tap does: the progress line, the effect run on the
PICTURES only, the files delivered -- or the "no image" reply when the upload has none.
"""
import asyncio
import time

import pytest

from app.routers.telegram import callbacks_media as cbm

log = []


class _TS:
    async def send_message(self, chat_id, text, **kw):
        log.append(("send", text))


class _CS:
    def __init__(self, db, user=None):
        pass

    async def execute_command(self, cmd, arg, attachments=None):
        log.append(("exec", cmd, arg, [a[0] for a in attachments]))
        return {"type": "files", "files": [cmd]}


async def _deliver(chat_id, user, result, offer):
    log.append(("deliver", result, offer))


class _DB:
    def query(self, *a):
        return self

    def filter(self, *a):
        return self

    def first(self):
        return object()


def _tap(monkeypatch, action, atts):
    monkeypatch.setattr(cbm, "telegram_service", _TS())
    monkeypatch.setattr(cbm, "CommandService", _CS)
    monkeypatch.setattr(cbm, "_deliver_files_result", _deliver)
    monkeypatch.setitem(cbm._media_action_cache, 7, {"attachments": atts, "ts": time.time()})
    log.clear()
    asyncio.run(cbm._cb_media(None, _DB(), 7, "media:" + action, None, "q"))
    return list(log)


@pytest.mark.parametrize("action", sorted(cbm._IMAGE_EFFECT_BUTTONS))
def test_each_button_runs_its_effect_on_the_pictures(action, monkeypatch):
    cmd, empty, working = cbm._IMAGE_EFFECT_BUTTONS[action]
    pics = [("a.png", b"1", "image/png"), ("n.txt", b"t", "text/plain"), ("c.jpg", b"2", "image/jpeg")]
    assert _tap(monkeypatch, action, pics) == [
        ("send", working), ("exec", cmd, "", ["a.png", "c.jpg"]), ("deliver", {"type": "files", "files": [cmd]}, True)]
    assert _tap(monkeypatch, action, [("n.txt", b"t", "text/plain")]) == [("send", empty)]


def test_every_button_names_a_real_command():
    from app.services.command_service.core import CommandService
    for action, (cmd, _, _) in cbm._IMAGE_EFFECT_BUTTONS.items():
        assert cmd in CommandService.COMMANDS, (action, cmd)
