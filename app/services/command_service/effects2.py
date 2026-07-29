"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
from ._common import Optional


class _Effects2Mixin:
    async def _terminator_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the terminator clip: `terminator`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `terminator`."}

        import asyncio
        from app.services.effects_service import terminator_attachments

        outputs, summary = await asyncio.to_thread(terminator_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _reze_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the reze clip: `reze`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `reze`."}

        import asyncio
        from app.services.effects_service import reze_attachments

        outputs, summary = await asyncio.to_thread(reze_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _vibe_command(self, attachments: Optional[list]) -> dict:
        """Put a cute anime girl dancing over an attached image for 8s: `vibe`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `vibe`."}

        import asyncio
        from app.services.effects_service import vibe_attachments

        outputs, summary = await asyncio.to_thread(vibe_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _makima_command(self, attachments: Optional[list]) -> dict:
        """Makima finger-guns whoever is in an attached image for 8s: `makima`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `makima`."}

        import asyncio
        from app.services.effects_service import makima_attachments

        outputs, summary = await asyncio.to_thread(makima_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _rebecca_command(self, attachments: Optional[list]) -> dict:
        """Put Rebecca dancing with a thumbs up over an attached image for 8s: `rebecca`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `rebecca`."}

        import asyncio
        from app.services.effects_service import rebecca_attachments

        outputs, summary = await asyncio.to_thread(rebecca_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _feliz_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the feliz clip: `feliz`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `feliz`."}

        import asyncio
        from app.services.effects_service import feliz_attachments

        outputs, summary = await asyncio.to_thread(feliz_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _horse_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the horse clip: `horse`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `horse`."}

        import asyncio
        from app.services.effects_service import horse_attachments

        outputs, summary = await asyncio.to_thread(horse_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _knightrider_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the Knight Rider theme: `knightrider`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `knightrider`."}

        import asyncio
        from app.services.effects_service import knightrider_attachments

        outputs, summary = await asyncio.to_thread(knightrider_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _sleepwell_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the Sleep Well clip: `sleepwell`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `sleepwell`."}

        import asyncio
        from app.services.effects_service import sleepwell_attachments

        outputs, summary = await asyncio.to_thread(sleepwell_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _prayer_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the prayer clip: `prayer`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `prayer`."}

        import asyncio
        from app.services.effects_service import prayer_attachments

        outputs, summary = await asyncio.to_thread(prayer_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _sopranos_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Sopranos theme clip: `sopranos`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `sopranos`."}

        import asyncio
        from app.services.effects_service import sopranos_attachments

        outputs, summary = await asyncio.to_thread(sopranos_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _cheers_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Cheers theme clip: `cheers`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `cheers`."}

        import asyncio
        from app.services.effects_service import cheers_attachments

        outputs, summary = await asyncio.to_thread(cheers_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _munsters_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Munsters theme clip: `munsters`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `munsters`."}

        import asyncio
        from app.services.effects_service import munsters_attachments

        outputs, summary = await asyncio.to_thread(munsters_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _happydays_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Happy Days theme clip: `happydays`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `happydays`."}

        import asyncio
        from app.services.effects_service import happydays_attachments

        outputs, summary = await asyncio.to_thread(happydays_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _dontwanttowait_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Dawson's Creek theme clip: `dontwanttowait`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `dontwanttowait`."}

        import asyncio
        from app.services.effects_service import dontwanttowait_attachments

        outputs, summary = await asyncio.to_thread(dontwanttowait_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _strangerthings_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Stranger Things theme clip: `strangerthings`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `strangerthings`."}

        import asyncio
        from app.services.effects_service import strangerthings_attachments

        outputs, summary = await asyncio.to_thread(strangerthings_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _adamsfamily_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Addams Family theme clip: `adamsfamily`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `adamsfamily`."}

        import asyncio
        from app.services.effects_service import adamsfamily_attachments

        outputs, summary = await asyncio.to_thread(adamsfamily_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _xmen_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the X-Men theme clip: `xmen`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `xmen`."}

        import asyncio
        from app.services.effects_service import xmen_attachments

        outputs, summary = await asyncio.to_thread(xmen_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _futurama_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Futurama theme clip: `futurama`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `futurama`."}

        import asyncio
        from app.services.effects_service import futurama_attachments

        outputs, summary = await asyncio.to_thread(futurama_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _charliesangles_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Charlie's Angels theme clip: `charliesangles`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `charliesangles`."}

        import asyncio
        from app.services.effects_service import charliesangles_attachments

        outputs, summary = await asyncio.to_thread(charliesangles_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _differentstroke_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Diff'rent Strokes theme clip: `differentstroke`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `differentstroke`."}

        import asyncio
        from app.services.effects_service import differentstroke_attachments

        outputs, summary = await asyncio.to_thread(differentstroke_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _seinfeld_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Seinfeld theme clip: `seinfeld`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `seinfeld`."}

        import asyncio
        from app.services.effects_service import seinfeld_attachments

        outputs, summary = await asyncio.to_thread(seinfeld_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _jerry_command(self, attachments: Optional[list]) -> dict:
        """Composite Jerry onto an attached image, set to the Seinfeld theme: `jerry`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `jerry`."}

        import asyncio
        from app.services.effects_service import jerry_attachments

        outputs, summary = await asyncio.to_thread(jerry_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _onepiece_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the One Piece theme clip: `onepiece`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `onepiece`."}

        import asyncio
        from app.services.effects_service import onepiece_attachments

        outputs, summary = await asyncio.to_thread(onepiece_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _overtaken_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the overtaken clip: `overtaken`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `overtaken`."}

        import asyncio
        from app.services.effects_service import overtaken_attachments

        outputs, summary = await asyncio.to_thread(overtaken_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _freebird_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Free Bird solo: `freebird`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `freebird`."}

        import asyncio
        from app.services.effects_service import freebird_attachments

        outputs, summary = await asyncio.to_thread(freebird_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _kanye_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Kanye clip: `kanye`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `kanye`."}

        import asyncio
        from app.services.effects_service import kanye_attachments

        outputs, summary = await asyncio.to_thread(kanye_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _darkness_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the darkness clip: `darkness`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `darkness`."}

        import asyncio
        from app.services.effects_service import darkness_attachments

        outputs, summary = await asyncio.to_thread(darkness_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _bike_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the bike clip: `bike`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `bike`."}

        import asyncio
        from app.services.effects_service import bike_attachments

        outputs, summary = await asyncio.to_thread(bike_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _jobs_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the they-took-our-jobs clip: `jobs`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `jobs`."}

        import asyncio
        from app.services.effects_service import jobs_attachments

        outputs, summary = await asyncio.to_thread(jobs_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _ree_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the REEEE clip: `ree`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `ree`."}

        import asyncio
        from app.services.effects_service import ree_attachments

        outputs, summary = await asyncio.to_thread(ree_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _liberal_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the liberal clip: `liberal`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `liberal`."}

        import asyncio
        from app.services.effects_service import liberal_attachments

        outputs, summary = await asyncio.to_thread(liberal_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _moving_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the moving clip: `moving`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `moving`."}

        import asyncio
        from app.services.effects_service import moving_attachments

        outputs, summary = await asyncio.to_thread(moving_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _harlem_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Harlem Shake clip: `harlem`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `harlem`."}

        import asyncio
        from app.services.effects_service import harlem_attachments

        outputs, summary = await asyncio.to_thread(harlem_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _chimp_command(self, attachments: Optional[list]) -> dict:
        """Overlay the animated chimp gif on the lower third of an image: `chimp`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `chimp`."}

        import asyncio
        from app.services.effects_service import chimp_attachments

        outputs, summary = await asyncio.to_thread(chimp_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _consider_command(self, attachments: Optional[list]) -> dict:
        """Overlay the 'consider the following' cutout on an attached image: `consider`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `consider`."}

        import asyncio
        from app.services.effects_service import consider_attachments

        outputs, summary = await asyncio.to_thread(consider_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _clay_command(self, attachments: Optional[list]) -> dict:
        """Overlay the background-removed Clay Davis clip on an image: `clay`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `clay`."}

        import asyncio
        from app.services.effects_service import clay_attachments

        outputs, summary = await asyncio.to_thread(clay_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _uwu_command(self, attachments: Optional[list]) -> dict:
        """Overlay a dancing cute anime girl on an image, set to an uwu clip: `uwu`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `uwu`."}

        import asyncio
        from app.services.effects_service import uwu_attachments

        outputs, summary = await asyncio.to_thread(uwu_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _wasteland_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Teenage Wasteland intro: `wasteland`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `wasteland`."}

        import asyncio
        from app.services.effects_service import wasteland_attachments

        outputs, summary = await asyncio.to_thread(wasteland_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _mixalot_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Baby Got Back clip: `mixalot`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `mixalot`."}

        import asyncio
        from app.services.effects_service import mixalot_attachments

        outputs, summary = await asyncio.to_thread(mixalot_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _thug_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the THUG LIFE clip: `thug`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `thug`."}

        import asyncio
        from app.services.effects_service import thug_attachments

        outputs, summary = await asyncio.to_thread(thug_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _feltedtables_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the felted-tables clip: `feltedtables`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `feltedtables`."}

        import asyncio
        from app.services.effects_service import feltedtables_attachments

        outputs, summary = await asyncio.to_thread(feltedtables_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _4chan_command(self, arg: str) -> dict:
        """Open 4chan catalog browser. Optional board: g, pol, a, or h."""
        allowed_boards = ("g", "pol", "a", "h")
        board = (arg or "g").strip().lower()
        if board not in allowed_boards:
            board = "g"
        return {
            "type": "4chan",
            "content": f"Opening 4chan /{board}/ catalog.",
            "board": board,
        }
