"""/api/media/process (the fediverse bots' effect path) accepts exactly the effects the app has, and runs one with no
branch of its own through the same dispatch chat and Telegram use (code review, 2026-10-07).

Its allowlist was a hand-typed tuple of ~90 names that had drifted: 14 supported effects (carl, collage, gura, shrug,
soyjack, woodchipper, …) answered "unsupported command" -- and had they passed, they would have fallen into the
final `else`, which was the CLIP handler, and answered "clip needs <start> <end>".
"""
import asyncio
import base64
import os
import pytest

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
    """Every effect the command service supports is accepted here -- a hand-typed copy is what drifted."""
    from app.routers import media_api
    from app.services.command_service.core import CommandService
    refused = [c for c in sorted(set(CommandService.MOTION_EFFECTS) | set(CommandService.ANIMATED_EFFECTS))
               if asyncio.run(media_api.process_media(media_api.MediaProcessRequest(command=c, arg="", media=[]),
                                                      None, None, True)).get("error", "").startswith("unsupported")]
    assert refused == [], refused


@pytest.mark.parametrize("name", sorted(__import__("app.routers.media_api", fromlist=["x"])._DIRECT_EFFECTS))
def test_each_direct_effect_runs_its_own_function_and_keeps_its_summary(name, monkeypatch):
    """The 81 copied `elif command == X: X_attachments(...)` branches are one table now (checked identical against the
    old endpoint for every accepted command). Each name must still reach ITS function, and a failed render must still
    come back as that function's own summary with no files -- not as the shared dispatch's error."""
    from app.routers import media_api
    from app.services import effects_service, media_service
    from app.services.command_service.core import CommandService
    assert name in CommandService.MOTION_EFFECTS or name in CommandService.ANIMATED_EFFECTS, "not even allowlisted"
    monkeypatch.setattr(media_service, "compress_effect_outputs", lambda o: o)
    monkeypatch.setattr(media_api, "_brand_videos", lambda o, *a, **k: o)
    monkeypatch.setattr(effects_service, f"{name}_attachments",
                        lambda atts: ([{"filename": f"{name}.mp4", "data": b"v", "content_type": "video/mp4"}], f"## {name}")
                        if [a[0] for a in atts] == ["a.jpg"] else ([], "the picture never arrived"))
    r = asyncio.run(media_api.process_media(_req(name), None, None, True))
    assert r.get("summary") == f"## {name}" and r["files"][0]["filename"] == f"{name}.mp4", r
    monkeypatch.setattr(effects_service, f"{name}_attachments", lambda atts: ([], "it broke"))
    r = asyncio.run(media_api.process_media(_req(name), None, None, True))
    assert r.get("summary") == "it broke" and not r.get("files") and "error" not in r, r
