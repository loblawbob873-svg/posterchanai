"""The 88 picture-in, effect-out chat commands are generated (command_service/_image_effects.py), not copied.

The rewrite was checked against the old hand-written methods for every command (with a picture, without one,
with only a text file, and when the effect fails) and came out identical; this keeps every name honest the way
chat reaches it: `execute_command`'s dispatch hands the attached files to that effect's own `_attachments`
function and returns its files, asks for a picture when there is none, and passes the effect's own error on.
"""
import asyncio

import pytest

from app.services import effects_service
from app.services.command_service.core import CommandService
from app.services.command_service._image_effects import IMAGE_EFFECT_COMMANDS, _ASK


def _svc():
    svc = CommandService.__new__(CommandService)
    svc._effects_no_forward = True
    return svc


@pytest.mark.parametrize("name", sorted(IMAGE_EFFECT_COMMANDS))
def test_each_command_runs_its_own_effect(name, monkeypatch):
    got = []

    def fake(attachments):
        got.append([a[0] for a in attachments])
        return [{"filename": f"out_{name}.mp4"}], f"made {name}"
    monkeypatch.setattr(effects_service, f"{name}_attachments", fake)
    run = lambda atts: asyncio.run(_svc()._execute_command_inner(name, "", None, None, atts, None))
    assert run([("a.png", b"1", "image/png"), ("n.txt", b"t", "text/plain")]) == \
        {"type": "files", "content": f"made {name}", "files": [{"filename": f"out_{name}.mp4"}]}
    assert got == [["a.png", "n.txt"]]
    ask = _ASK.get(name, f"Attach an image, then send `{name}`.")
    assert run(None) == {"type": "text", "content": ask}
    assert run([("n.txt", b"t", "text/plain")]) == {"type": "text", "content": ask}
    monkeypatch.setattr(effects_service, f"{name}_attachments", lambda a: ([], "it broke"))
    assert run([("a.png", b"1", "image/png")]) == {"type": "text", "content": "it broke"}


def test_every_command_is_still_advertised_and_takes_the_upload():
    for name in IMAGE_EFFECT_COMMANDS:
        assert name in CommandService.COMMANDS, name
        assert CommandService.wants_attachments(name), name
