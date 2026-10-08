"""/api/media/process (the fediverse bots' effect path) accepts exactly the effects the app has, and runs one with no
branch of its own through the same dispatch chat and Telegram use (code review, 2026-10-07).

Its allowlist was a hand-typed tuple of ~90 names that had drifted: 14 supported effects (carl, collage, gura, shrug,
soyjack, woodchipper, …) answered "unsupported command" -- and had they passed, they would have fallen into the
final `else`, which was the CLIP handler, and answered "clip needs <start> <end>".
"""
import asyncio
import base64
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _req(command, arg=""):
    from app.routers import media_api
    return media_api.MediaProcessRequest(command=command, arg=arg, media=[media_api.MediaItem(
        filename="a.jpg", data=base64.b64encode(b"x").decode(), content_type="image/jpeg")])


def test_an_effect_without_its_own_branch_runs_through_the_shared_dispatch(monkeypatch):
    from app.routers import media_api
    from app.services.command_service.core import CommandService
    from app.services import media_service
    seen = {}

    async def fake_inner(self, command, arg, *a, **k):
        seen["call"] = (command, arg)
        return {"type": "files", "content": "## shrugged", "files": [{"filename": "a.mp4", "data": b"vid", "content_type": "video/mp4"}]}
    monkeypatch.setattr(CommandService, "_execute_command_inner", fake_inner)
    monkeypatch.setattr(media_service, "compress_effect_outputs", lambda o: o)
    monkeypatch.setattr(media_api, "_brand_videos", lambda o, *a, **k: o)
    assert "shrug" in CommandService.MOTION_EFFECTS
    r = asyncio.run(media_api.process_media(_req("shrug"), None, None, True))
    assert seen.get("call") == ("shrug", ""), seen
    assert r.get("summary") == "## shrugged" and r["files"][0]["filename"] == "a.mp4", r


def test_an_unknown_command_is_still_refused():
    from app.routers import media_api
    r = asyncio.run(media_api.process_media(_req("rm"), None, None, True))
    assert r == {"error": "unsupported command 'rm'"}, r


def test_the_allowlist_is_not_a_hand_typed_copy_again():
    src = open(os.path.join(ROOT, "app", "routers", "media_api.py")).read()
    assert '"dildo", "poo", "cum"' not in src, "the hand-typed effect tuple is back"
    assert "CommandService.MOTION_EFFECTS" in src and "CommandService.ANIMATED_EFFECTS" in src
