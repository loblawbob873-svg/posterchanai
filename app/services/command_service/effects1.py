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

    async def _collage_command(self, attachments: Optional[list]) -> dict:
        """Combine ALL attached images into a single collage image: `collage` (attach 2+ images)."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {
                "type": "text",
                "content": "Attach two or more images, then send `collage` to combine them.",
            }

        import asyncio
        from app.services.effects_service import collage_attachments

        outputs, summary = await asyncio.to_thread(collage_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _dildo_command(self, attachments: Optional[list]) -> dict:
        """Scatter dildos all over an attached image: `dildo` (no text needed)."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {
                "type": "text",
                "content": "Attach an image, then send `dildo` to decorate it.",
            }

        import asyncio
        from app.services.effects_service import dildo_attachments

        # Pillow compositing is light, but keep it off the event loop for big images.
        outputs, summary = await asyncio.to_thread(dildo_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _poo_command(self, attachments: Optional[list]) -> dict:
        """Scatter poop all over an attached image: `poo` (no text needed)."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {
                "type": "text",
                "content": "Attach an image, then send `poo` to decorate it.",
            }

        import asyncio
        from app.services.effects_service import poo_attachments

        # Pillow compositing is light, but keep it off the event loop for big images.
        outputs, summary = await asyncio.to_thread(poo_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _cum_command(self, attachments: Optional[list]) -> dict:
        """Scatter cum all over an attached image: `cum` (no text needed)."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {
                "type": "text",
                "content": "Attach an image, then send `cum` to decorate it.",
            }

        import asyncio
        from app.services.effects_service import cum_attachments

        # Pillow compositing is light, but keep it off the event loop for big images.
        outputs, summary = await asyncio.to_thread(cum_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _blood_command(self, attachments: Optional[list]) -> dict:
        """Splatter blood all over an attached image: `blood` (no text needed)."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {
                "type": "text",
                "content": "Attach an image, then send `blood` to decorate it.",
            }

        import asyncio
        from app.services.effects_service import blood_attachments

        # Pillow compositing is light, but keep it off the event loop for big images.
        outputs, summary = await asyncio.to_thread(blood_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _bullethole_command(self, attachments: Optional[list]) -> dict:
        """Punch bullet holes all over an attached image: `bullethole` (no text)."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `bullethole`."}

        import asyncio
        from app.services.effects_service import bullethole_attachments

        outputs, summary = await asyncio.to_thread(bullethole_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _fire_command(self, attachments: Optional[list]) -> dict:
        """Set an attached image on fire: `fire` (no text needed)."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `fire`."}

        import asyncio
        from app.services.effects_service import fire_attachments

        outputs, summary = await asyncio.to_thread(fire_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _nakedman_command(self, attachments: Optional[list]) -> dict:
        """Overlay a fat cartoon man dancing (huge penis) on an attached image → 8s MP4: `nakedman`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `nakedman`."}

        import asyncio
        from app.services.effects_service import nakedman_attachments

        outputs, summary = await asyncio.to_thread(nakedman_attachments, attachments)
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

    async def _gay_command(self, attachments: Optional[list]) -> dict:
        """Stamp a big red GAY rubber stamp on an attached image: `gay`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `gay`."}

        import asyncio
        from app.services.effects_service import gay_attachments

        outputs, summary = await asyncio.to_thread(gay_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _goon_command(self, attachments: Optional[list]) -> dict:
        """Stamp a big red GOON rubber stamp on an attached image: `goon`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `goon`."}

        import asyncio
        from app.services.effects_service import goon_attachments

        outputs, summary = await asyncio.to_thread(goon_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _hag_command(self, attachments: Optional[list]) -> dict:
        """Stamp a big red HAG rubber stamp + a cute old lady on an attached image: `hag`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `hag`."}

        import asyncio
        from app.services.effects_service import hag_attachments

        outputs, summary = await asyncio.to_thread(hag_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _blacked_command(self, attachments: Optional[list]) -> dict:
        """Slap the BLACKED logo on an attached image: `blacked`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `blacked`."}

        import asyncio
        from app.services.effects_service import blacked_attachments

        outputs, summary = await asyncio.to_thread(blacked_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _kosher_command(self, attachments: Optional[list]) -> dict:
        """Stamp a 100% KOSHER certification seal on an attached image: `kosher`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `kosher`."}

        import asyncio
        from app.services.effects_service import kosher_attachments

        outputs, summary = await asyncio.to_thread(kosher_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _blue_command(self, attachments: Optional[list]) -> dict:
        """Smear dripping blue paint around the mouth then stamp KOSHER: `blue`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `blue`."}

        import asyncio
        from app.services.effects_service import blue_attachments

        outputs, summary = await asyncio.to_thread(blue_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _barked_command(self, attachments: Optional[list]) -> dict:
        """Drop a smirking dog and #BARKED on an attached image: `barked`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `barked`."}

        import asyncio
        from app.services.effects_service import barked_attachments

        outputs, summary = await asyncio.to_thread(barked_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _hava_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a 6s MP4 set to Hava Nagila: `hava`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `hava`."}

        import asyncio
        from app.services.effects_service import hava_attachments

        outputs, summary = await asyncio.to_thread(hava_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _indian_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a 6s MP4 set to an Indian song: `indian`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `indian`."}

        import asyncio
        from app.services.effects_service import indian_attachments

        outputs, summary = await asyncio.to_thread(indian_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _yakety_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a 6s MP4 set to Yakety Sax: `yakety`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `yakety`."}

        import asyncio
        from app.services.effects_service import yakety_attachments

        outputs, summary = await asyncio.to_thread(yakety_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _yamete_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a 6s MP4 set to the yamete clip: `yamete`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `yamete`."}

        import asyncio
        from app.services.effects_service import yamete_attachments

        outputs, summary = await asyncio.to_thread(yamete_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _curb_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Curb Your Enthusiasm theme: `curb`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `curb`."}

        import asyncio
        from app.services.effects_service import curb_attachments

        outputs, summary = await asyncio.to_thread(curb_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _depressing_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a 10s MP4 set to a depressing track: `depressing`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `depressing`."}

        import asyncio
        from app.services.effects_service import depressing_attachments

        outputs, summary = await asyncio.to_thread(depressing_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _fahh_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the fahh clip: `fahh`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `fahh`."}

        import asyncio
        from app.services.effects_service import fahh_attachments

        outputs, summary = await asyncio.to_thread(fahh_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _helpme_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a 5s MP4 set to the helpme clip: `helpme`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `helpme`."}

        import asyncio
        from app.services.effects_service import helpme_attachments

        outputs, summary = await asyncio.to_thread(helpme_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _gong_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the gong clip: `gong`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `gong`."}

        import asyncio
        from app.services.effects_service import gong_attachments

        outputs, summary = await asyncio.to_thread(gong_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _fbi_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the FBI open up clip: `fbi`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `fbi`."}

        import asyncio
        from app.services.effects_service import fbi_attachments

        outputs, summary = await asyncio.to_thread(fbi_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _redeem_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the do not redeem clip: `redeem`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `redeem`."}

        import asyncio
        from app.services.effects_service import redeem_attachments

        outputs, summary = await asyncio.to_thread(redeem_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _gigity_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the giggity clip: `gigity`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `gigity`."}

        import asyncio
        from app.services.effects_service import gigity_attachments

        outputs, summary = await asyncio.to_thread(gigity_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _beavis_command(self, attachments: Optional[list]) -> dict:
        """Overlay Beavis and Butt-Head cackling on an image, set to the laugh: `beavis`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `beavis`."}

        import asyncio
        from app.services.effects_service import beavis_attachments

        outputs, summary = await asyncio.to_thread(beavis_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _smell_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the smell clip: `smell`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `smell`."}

        import asyncio
        from app.services.effects_service import smell_attachments

        outputs, summary = await asyncio.to_thread(smell_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _hood_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a 10s MP4 set to the hood clip: `hood`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `hood`."}

        import asyncio
        from app.services.effects_service import hood_attachments

        outputs, summary = await asyncio.to_thread(hood_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _akbar_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the akbar clip: `akbar`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `akbar`."}

        import asyncio
        from app.services.effects_service import akbar_attachments

        outputs, summary = await asyncio.to_thread(akbar_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _retard_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the retard-alert clip: `retard`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `retard`."}

        import asyncio
        from app.services.effects_service import retard_attachments

        outputs, summary = await asyncio.to_thread(retard_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _heat_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a 15s MP4 set to Heat of the Moment: `heat`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `heat`."}

        import asyncio
        from app.services.effects_service import heat_attachments

        outputs, summary = await asyncio.to_thread(heat_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _whoabuddy_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the whoa buddy clip: `whoabuddy`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `whoabuddy`."}

        import asyncio
        from app.services.effects_service import whoabuddy_attachments

        outputs, summary = await asyncio.to_thread(whoabuddy_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _diarrhea_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the diarrhea clip: `diarrhea`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `diarrhea`."}

        import asyncio
        from app.services.effects_service import diarrhea_attachments

        outputs, summary = await asyncio.to_thread(diarrhea_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _seth_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the seth clip: `seth`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `seth`."}

        import asyncio
        from app.services.effects_service import seth_attachments

        outputs, summary = await asyncio.to_thread(seth_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _robocop_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the robocop clip: `robocop`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `robocop`."}

        import asyncio
        from app.services.effects_service import robocop_attachments

        outputs, summary = await asyncio.to_thread(robocop_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _titan_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the titan clip: `titan`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `titan`."}

        import asyncio
        from app.services.effects_service import titan_attachments

        outputs, summary = await asyncio.to_thread(titan_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

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

    async def _theraped_command(self, attachments: Optional[list]) -> dict:
        """Pointing-up meme character + caption on an attached image: `theraped`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `theraped`."}

        import asyncio
        from app.services.effects_service import theraped_attachments

        outputs, summary = await asyncio.to_thread(theraped_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _would_command(self, attachments: Optional[list]) -> dict:
        """Old man points up saying WOULD on an attached image: `would`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `would`."}

        import asyncio
        from app.services.effects_service import would_attachments

        outputs, summary = await asyncio.to_thread(would_attachments, attachments)
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

    async def _shrug_command(self, attachments: Optional[list]) -> dict:
        """Rabbi shrugs "Whaddya gonna do?" on an attached image: `shrug`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `shrug`."}

        import asyncio
        from app.services.effects_service import shrug_attachments

        outputs, summary = await asyncio.to_thread(shrug_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}
