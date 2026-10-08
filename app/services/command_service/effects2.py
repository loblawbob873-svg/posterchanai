"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
from ._common import Optional


class _Effects2Mixin:

    async def _mentioned_command(self, arg: str, attachments: Optional[list]) -> dict:
        """The "<THING> MENTIONED" meme over an image: `mentioned michigan`."""
        from app.services.media_service import is_image
        from app.services.effects_service import MENTIONED_ASK, mentioned_caption

        if not mentioned_caption(arg or ""):
            return {"type": "text", "content": MENTIONED_ASK}

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `mentioned <thing>` (e.g. `mentioned michigan`)."}

        import asyncio
        from app.services.effects_service import mentioned_attachments

        outputs, summary = await asyncio.to_thread(mentioned_attachments, attachments, arg or "")
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}







