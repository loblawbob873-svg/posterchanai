"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
from ._common import Optional


class _Effects1Mixin:
    async def _meme_command(self, arg: str, attachments: Optional[list]) -> dict:
        """Add outlined white meme text to an attached image: `meme <text>`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {
                "type": "text",
                "content": "Attach an image, then send `meme <text>` to caption it.",
            }
        if not (arg or "").strip():
            return {"type": "text", "content": "Usage: `meme <text>` — the caption to add."}

        import asyncio
        from app.services.effects_service import meme_attachments

        # Pillow text rendering is light, but keep it off the event loop for big images.
        outputs, summary = await asyncio.to_thread(meme_attachments, attachments, arg)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _alive_command(self, arg: str, attachments: Optional[list]) -> dict:
        """Make an attached photo come alive with 3D parallax motion:
        `alive [subtle|normal|strong]` (default normal)."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach a photo, then send `alive [subtle|normal|strong]`."}

        import asyncio
        from app.services.parallax_service import alive_attachments

        outputs, summary = await asyncio.to_thread(alive_attachments, attachments, arg or "")
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _glow_command(self, arg: str, attachments: Optional[list]) -> dict:
        """Generic "make it stand out": with an attached image → breathing zoom + colour
        pop + a sweeping light (`glow`). With NO image but text → a glowing neon text-card
        post (`glow <text>`)."""
        from app.services.media_service import is_image
        import asyncio

        has_image = attachments and any(is_image(fn, ct) for fn, _, ct in attachments)
        if has_image:
            from app.services.effects_service import glow_attachments
            outputs, summary = await asyncio.to_thread(glow_attachments, attachments)
            if not outputs:
                return {"type": "text", "content": summary}
            return {"type": "files", "content": summary, "files": outputs}

        # No image: render the text as a glowing neon card (a "glowing text post").
        if (arg or "").strip():
            from app.services.effects_service import render_glow_text_card
            png = await asyncio.to_thread(render_glow_text_card, arg.strip())
            return {"type": "files", "content": "## ✨ Glow", "files": [
                {"filename": "glow.png", "data": png, "content_type": "image/png"},
            ]}
        return {"type": "text", "content": "Attach an image, or send `glow <text>` for a glowing text post."}

    async def _nodontthinkiwill_command(self, attachments: Optional[list], args: str = "") -> dict:
        """Old Steve Rogers + caption on an attached image: `nodontthinkiwill [text]`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `nodontthinkiwill`."}

        import asyncio
        from app.services.effects_service import add_nodontthinkiwill
        from app.services.effects_service.character import _pointing_attachments

        caption = (args or "").strip()
        fn = ((lambda d: add_nodontthinkiwill(d, caption)) if caption else add_nodontthinkiwill)
        outputs, summary = await asyncio.to_thread(
            _pointing_attachments, attachments, "nodontthinkiwill", "No, I Don't Think I Will", fn)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _ruckus_command(self, attachments: Optional[list], args: str = "") -> dict:
        """Uncle Ruckus over an attached image, set to his theme: `ruckus`. Takes NO caption — he is
        a reaction overlay (like `carl`/`soyjack`), so any argument is ignored rather than drawn on
        the picture. Output is video/mp4 wherever assets/ruckus.mp3 is installed."""
        from app.services.effects_service import ruckus_attachments
        return await self._reaction_command(attachments, "ruckus", ruckus_attachments)

    async def _nothingeverhappens_command(self, attachments: Optional[list], args: str = "") -> dict:
        """The angry teacher + caption on an attached image: `nothingeverhappens [text]`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `nothingeverhappens`."}

        import asyncio
        from app.services.effects_service import add_nothingeverhappens
        from app.services.effects_service.character import _pointing_attachments

        # An argument replaces the default line, so `nothingeverhappens rent is going down` works.
        caption = (args or "").strip()
        fn = ((lambda d: add_nothingeverhappens(d, caption)) if caption else add_nothingeverhappens)
        outputs, summary = await asyncio.to_thread(
            _pointing_attachments, attachments, "nothingeverhappens", "Nothing Ever Happens", fn)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _reaction_command(self, attachments: Optional[list], name: str, fn) -> dict:
        """Shared body for the caption-less reaction overlays (`carl`/`soyjack`/`anyways`): the cutout
        stands bottom-centre over the attached image. One implementation so they can't drift."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn_, ct) for fn_, _, ct in attachments):
            return {"type": "text", "content": f"Attach an image, then send `{name}`."}

        import asyncio

        outputs, summary = await asyncio.to_thread(fn, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _carl_command(self, attachments: Optional[list]) -> dict:
        """Carl points at an attached image: `carl`."""
        from app.services.effects_service import carl_attachments
        return await self._reaction_command(attachments, "carl", carl_attachments)

    async def _soyjack_command(self, attachments: Optional[list]) -> dict:
        """Two soyjaks point and yell at an attached image: `soyjack`."""
        from app.services.effects_service import soyjack_attachments
        return await self._reaction_command(attachments, "soyjack", soyjack_attachments)

    async def _lookingaway_command(self, attachments: Optional[list]) -> dict:
        """The monkey puppet looks away from an attached image, then turns to you: `lookingaway`
        (`anyways` is the original name, kept as an alias in COMMAND_ALIASES)."""
        from app.services.effects_service import lookingaway_attachments
        return await self._reaction_command(attachments, "lookingaway", lookingaway_attachments)

