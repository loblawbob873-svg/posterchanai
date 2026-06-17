"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
from ._common import Callable, ChatService, Optional, SearchService, Session, Tuple, extract_youtube_urls, is_youtube_url, logger, re, summarize_youtube
from .finance import _FinanceMixin
from .search import _SearchMixin
from .gen import _GenMixin
from .media import _MediaMixin
from .torrents import _TorrentsMixin
from .system import _SystemMixin
from .comms import _CommsMixin
from .productivity import _ProductivityMixin
from .effects1 import _Effects1Mixin
from .effects2 import _Effects2Mixin


class CommandService(_FinanceMixin, _SearchMixin, _GenMixin, _MediaMixin, _TorrentsMixin, _SystemMixin, _CommsMixin, _ProductivityMixin, _Effects1Mixin, _Effects2Mixin):
    COMMANDS = {
        "files": "Search for files in your storage",
        "help": "Show this help message",
        "search": "Web search: search <query>",
        "images": "Image search: images <query>",
        "geni": "Generate image: geni <prompt>",
        "musicgeni": "Generate a song: musicgeni <style prompt> [| lyrics]",
        "videogeni": "Generate a short video: videogeni <prompt>",
        "narrate": "Reply as a spoken (TTS) audio message: narrate <message>",
        "yt": "YouTube search: yt <query>",
        "ytdl": "Download YouTube, X, or Nitter: ytdl <url> (MP3 default), ytdl mp3/video <url>. For video, add clip <start> <end> and/or compress, e.g. ytdl video <url> clip 0:10 0:30 compress",
        "torrents": "Torrent search: torrents <query>",
        "nyaa": "Anime torrents: nyaa <query>",
        "dailynews": "Web news: dailynews <source>",
        "logs": "View system logs",
        "mail": "Email: mail <to> [subject] <body>",
        "translate": "Translate: translate <text> to <lang>",
        "4chan": "4chan browser: 4chan [g|pol|h] - view catalog",
        "compress": "Compress attached image(s) or video(s)",
        "removebackground": "Remove the background from an attached image (transparent PNG): removebackground",
        "clip": "Clip an attached video: clip <start> <end> (e.g. clip 0:10 0:30)",
        "convert": "Convert image(s) to PDF or a PDF to images",
        "ocr": "Read the text out of an attached image or PDF (OCR, no translation): ocr",
        "flashcards": "Make an interactive multiple-choice study quiz from an attached PDF/image/slide deck, or a URL: flashcards <url>",
        "post": "Share text (and an optional attached image) to your connected Misskey/Pleroma: post <text>",
        "remind": "Set a reminder in natural language: remind <what> <when> (e.g. remind open the oven in 10m, remind me next tuesday to call mom). Delivered in the web UI and Telegram.",
        "reminders": "Show your pending reminders (clickable to cancel): reminders",
        "pin": "Pin something you run often — a search or any command: pin <query|command> (e.g. pin latest xrp news, or pin screenshot https://google.com)",
        "pins": "Show your pins — click to run, or delete: pins",
        "collage": "Combine all attached images into one collage: collage (attach 2+ images)",
        "meme": "Add outlined white meme text to an attached image: meme <text>",
        "dildo": "Scatter dildos all over an attached image: dildo",
        "poo": "Scatter poop all over an attached image: poo",
        "cum": "Scatter cum all over an attached image: cum",
        "blood": "Splatter blood all over an attached image: blood",
        "bullethole": "Punch bullet holes all over an attached image: bullethole",
        "fire": "Set an attached image on fire: fire",
        "gay": "Stamp a big red GAY rubber stamp on an attached image: gay",
        "blacked": "Slap the BLACKED logo on an attached image: blacked",
        "kosher": "Stamp a 100% KOSHER certification seal on an attached image: kosher",
        "blue": "Smear dripping blue paint around the mouth (then stamp KOSHER) on an attached image: blue",
        "barked": "Drop a smirking dog and #BARKED on an attached image: barked",
        "alive": "Make an attached photo come alive with 3D parallax motion: alive [subtle(default)|normal|strong]",
        "glow": "Make an attached image stand out — gentle motion, colour pop and a sweeping light: glow",
        "hava": "Turn an attached image into a 6s MP4 set to Hava Nagila: hava",
        "indian": "Turn an attached image into a 6s MP4 set to an Indian song: indian",
        "yakety": "Turn an attached image into a 9s MP4 set to Yakety Sax: yakety",
        "yamete": "Turn an attached image into a 6s MP4 set to the yamete clip: yamete",
        "curb": "Turn an attached image into an MP4 set to the Curb Your Enthusiasm theme: curb",
        "depressing": "Turn an attached image into a 10s MP4 set to a depressing track: depressing",
        "fahh": "Turn an attached image into a short MP4 set to the fahh clip: fahh",
        "helpme": "Turn an attached image into a 5s MP4 set to the helpme clip: helpme",
        "gong": "Turn an attached image into a short MP4 set to the gong clip: gong",
        "fbi": "Turn an attached image into a short MP4 set to the FBI open up clip: fbi",
        "redeem": "Turn an attached image into a short MP4 set to the do not redeem clip: redeem",
        "gigity": "Turn an attached image into a short MP4 set to the giggity clip: gigity",
        "beavis": "Turn an attached image into a short MP4 set to the Beavis laugh: beavis",
        "smell": "Turn an attached image into a short MP4 set to the can you imagine the smell clip: smell",
        "hood": "Turn an attached image into a 10s MP4 set to the hood clip: hood",
        "akbar": "Turn an attached image into a short MP4 set to the akbar clip: akbar",
        "retard": "Turn an attached image into a short MP4 set to the retard-alert clip: retard",
        "whoabuddy": "Turn an attached image into a short MP4 set to the whoa buddy clip: whoabuddy",
        "seth": "Turn an attached image into a short MP4 set to the seth clip: seth",
        "robocop": "Turn an attached image into a short MP4 set to the robocop clip: robocop",
        "titan": "Turn an attached image into a short MP4 set to the titan clip: titan",
        "terminator": "Turn an attached image into a short MP4 set to the terminator clip: terminator",
        "reze": "Turn an attached image into a short MP4 set to the reze clip: reze",
        "feliz": "Turn an attached image into a short MP4 set to the feliz clip: feliz",
        "sopranos": "Turn an attached image into an MP4 set to the Sopranos theme clip: sopranos",
        "cheers": "Turn an attached image into an MP4 set to the Cheers theme clip: cheers",
        "munsters": "Turn an attached image into an MP4 set to the Munsters theme clip: munsters",
        "happydays": "Turn an attached image into an MP4 set to the Happy Days theme clip: happydays",
        "dontwanttowait": "Turn an attached image into an MP4 set to the Dawson's Creek theme clip: dontwanttowait",
        "strangerthings": "Turn an attached image into an MP4 set to the Stranger Things theme clip: strangerthings",
        "adamsfamily": "Turn an attached image into an MP4 set to the Addams Family theme clip: adamsfamily",
        "xmen": "Turn an attached image into an MP4 set to the X-Men theme clip: xmen",
        "futurama": "Turn an attached image into an MP4 set to the Futurama theme clip: futurama",
        "charliesangles": "Turn an attached image into an MP4 set to the Charlie's Angels theme clip: charliesangles",
        "differentstroke": "Turn an attached image into an MP4 set to the Diff'rent Strokes theme clip: differentstroke",
        "seinfeld": "Turn an attached image into an MP4 set to the Seinfeld theme clip: seinfeld",
        "onepiece": "Turn an attached image into an MP4 set to the One Piece theme clip: onepiece",
        "overtaken": "Turn an attached image into an MP4 set to the overtaken clip: overtaken",
        "freebird": "Turn an attached image into an MP4 set to the Free Bird solo: freebird",
        "kanye": "Turn an attached image into an MP4 set to the Kanye clip: kanye",
        "darkness": "Turn an attached image into an MP4 set to the darkness clip: darkness",
        "bike": "Turn an attached image into an MP4 set to the bike clip: bike",
        "jobs": "Turn an attached image into an MP4 set to the they-took-our-jobs clip: jobs",
        "ree": "Turn an attached image into an MP4 set to the REEEE clip: ree",
        "liberal": "Turn an attached image into an MP4 set to the liberal clip: liberal",
        "moving": "Turn an attached image into an MP4 set to the moving clip: moving",
        "harlem": "Turn an attached image into an MP4 set to the Harlem Shake clip: harlem",
        "chimp": "Overlay the animated chimp gif on the lower third of an attached image: chimp",
        "consider": "Overlay the 'consider the following' cutout on an attached image: consider",
        "clay": "Overlay the background-removed Clay Davis 'Shiiiit' clip on an image: clay",
        "wasteland": "Turn an attached image into an MP4 set to the Teenage Wasteland intro: wasteland",
        "mixalot": "Turn an attached image into an MP4 set to the Baby Got Back clip: mixalot",
        "thug": "Turn an attached image into an MP4 set to the THUG LIFE clip: thug",
        "feltedtables": "Turn an attached image into an MP4 set to the felted-tables clip: feltedtables",
        "prayer": "Turn an attached image into an MP4 set to the prayer clip: prayer",
        "node": "Remote node mgmt: node <name> <cmd> | node all <cmd> | node agent <name> <goal> | node agent all <goal> | node list | node jobs | node log <id> | node kill <id>",
        "budget": "Show your budget summary (income, unpaid bills, remaining)",
        "bills": "List your bills: bills (unpaid) | bills all | bills paid",
        "pay": "Pay a bill by name: pay <bill name>",
        "addbill": "Add a bill: addbill <name> <amount> [income]",
        "screenshot": "Full-page screenshot of a website: screenshot <url>",
        "poll": "Create a poll (Matrix): poll <question> | <option 1> | <option 2> — 2 to 20 options, separated by |",
    }
    COMMAND_ALIASES = {
        "torrent": "torrents",
        "bt": "torrents",
        "yt-dlp": "ytdl",
        "ytdlp": "ytdl",
        "youtube": "yt",
        "nodes": "node",
        "finance": "budget",
        "shot": "screenshot",
        "ss": "screenshot",
        "cards": "flashcards",
        "flashcard": "flashcards",
        "study": "flashcards",
        "quiz": "flashcards",
        "removebg": "removebackground",
        "rmbg": "removebackground",
        "nobg": "removebackground",
        "readtext": "ocr",
        "read": "ocr",
        "share": "post",
        "remindme": "remind",
        "reminder": "reminders",
        "savesearch": "pin",
        "savedsearches": "pins",
        "savedsearch": "pins",
        "saved": "pins",
    }
    MOTION_EFFECTS = {
        "collage", "meme", "dildo", "poo", "cum", "blood", "bullethole", "fire", "gay",
        "blacked", "kosher", "blue", "barked", "hava", "indian", "yakety", "yamete",
        "curb", "depressing", "fahh", "helpme", "gong", "fbi", "redeem",
        "gigity", "beavis", "smell", "hood", "akbar", "retard", "whoabuddy", "seth", "robocop", "titan", "terminator", "reze",
        "sopranos", "cheers", "munsters", "happydays", "dontwanttowait", "strangerthings", "adamsfamily", "xmen", "futurama", "charliesangles", "differentstroke", "seinfeld", "onepiece", "overtaken", "freebird", "kanye", "darkness", "bike", "jobs", "ree", "liberal", "moving",
        "harlem", "chimp", "consider", "clay", "wasteland", "mixalot", "thug",
        "feltedtables", "glow", "prayer", "alive", "feliz",
    }
    MOTION_ARGS = ("zoom", "shake", "medshake", "beginshake", "trippy", "pulse", "glow", "alive")
    ANIMATED_EFFECTS = {"chimp", "clay", "reze"}
    OVERLAY_MOTIONS = {"glow"}
    PHRASE_COMMANDS = {}

    def __init__(self, db: Session, user: Optional["User"] = None, is_bot: bool = False):
        self.db = db
        self.user = user
        # Bot-driven contexts (Matrix/Pleroma/Misskey listeners) are configured in Admin → Bots,
        # so they're exempt from per-user feature gating. Pleroma/Misskey hit /api/generate-image
        # directly (never this service); Matrix routes through here, so it sets is_bot=True.
        self.is_bot = is_bot
        self.search_service = SearchService(db)
        self.chat_service = ChatService(db, user=user)

    def parse_command(self, message: str) -> Tuple[Optional[str], str]:
        """Parse message for commands, return (command, argument)"""
        # Remove emojis and other unicode symbols that might interfere with matching
        import re
        # Remove common emojis and symbols (✏️, 🔄, etc.) but keep the text
        cleaned_message = re.sub(r'[✏️🔄📅📆🗓️➕➖✕×]', '', message)
        lower = cleaned_message.lower().strip()

        # Bare magnet link or .torrent URL (just pasted, no command word) → add to the torrent
        # client. Shared by the web UI + Matrix (both route through parse_command).
        _stripped = message.strip()
        if _stripped.startswith("magnet:?") or (
            re.match(r'^https?://\S+$', _stripped, re.IGNORECASE)
            and re.search(r'\.torrent(\?|$)', _stripped, re.IGNORECASE)
        ):
            return "torrents", f"add {_stripped}"

        # Check natural language phrases first (exact match)
        if lower in self.PHRASE_COMMANDS:
            cmd, arg = self.PHRASE_COMMANDS[lower]
            return cmd, arg

        # Video downloads
        for prefix in ["download this video ", "download video "]:
            if lower.startswith(prefix):
                url = message[len(prefix):].strip()
                return "ytdl", f"video {url}"
        
        # Generic download with YouTube URL
        if lower.startswith("download ") and ("youtube" in lower or "youtu.be" in lower):
            url = message[9:].strip()
            return "ytdl", url

        # Check canonical commands
        for cmd in self.COMMANDS:
            if lower.startswith(f"{cmd} "):
                return cmd, message[len(cmd) + 1 :].strip()
            if lower == cmd:
                return cmd, ""

        # Check aliases
        for alias, canonical in self.COMMAND_ALIASES.items():
            if lower.startswith(f"{alias} "):
                return canonical, message[len(alias) + 1 :].strip()
            if lower == alias:
                return canonical, ""

        return None, message

    async def execute_command(
        self,
        command: str,
        arg: str,
        last_prompt: Optional[str] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        attachments: Optional[list] = None,
        node_notify: Optional[Callable] = None,
    ) -> dict:
        """Execute a command, then shrink any oversized video outputs via the shared
        `compress` feature before returning (so effects don't hand back 10 MB clips)."""
        result = await self._execute_command_inner(
            command, arg, last_prompt, stop_check, attachments, node_notify,
        )
        # Only auto-compress EFFECT outputs — not the `compress`/`clip`/`convert`/`ytdl`
        # media tools, where the user controls quality (and `compress` already ran).
        if (command in self.MOTION_EFFECTS and isinstance(result, dict)
                and result.get("type") == "files" and result.get("files")):
            import asyncio
            from app.services import media_service
            result["files"] = await asyncio.to_thread(
                media_service.compress_output_videos, result["files"],
            )
        # TikTok-style branding end-card on effect VIDEOS: per-user avatar/@username + the
        # PosterChan mascot + "made with PosterChanAI". Gated by `effect_outro_enabled` (default on).
        if ((command in self.MOTION_EFFECTS or command in self.ANIMATED_EFFECTS)
                and isinstance(result, dict) and result.get("type") == "files" and result.get("files")):
            import asyncio
            result["files"] = await asyncio.to_thread(self._brand_effect_videos, result["files"])
        return result

    def _brand_effect_videos(self, files: list) -> list:
        """Append the GENERIC PosterChanAI end-card to each video output. Effects invoked from the
        web UI / Telegram brand WITHOUT any user info (the static 'made with PosterChanAI' card) —
        only fediverse mentions carry the poster's avatar/@username (done in the media API path).
        Best-effort: any failure leaves the original file untouched."""
        from app.models import Setting
        from app.services import media_service
        try:
            s = self.db.query(Setting).filter(Setting.key == "effect_outro_enabled").first()
            if s and str(s.value).strip().lower() in ("false", "0", "no", "off"):
                return files
        except Exception:
            pass
        out = []
        for f in files:
            try:
                if isinstance(f, dict) and f.get("content_type") == "video/mp4" and f.get("data"):
                    f = {**f, "data": media_service.append_outro(
                        f["data"], f.get("filename", "video.mp4"),
                        username=None, avatar_bytes=None)}
            except Exception as e:
                logger.warning(f"outro branding failed for {f.get('filename') if isinstance(f, dict) else '?'}: {e}")
            out.append(f)
        return out

    # Per-user feature access: command → capability flag on User (admins always allowed).
    # Configured in Admin → Users. A user without the capability is refused before dispatch.
    _CAPABILITY_BY_COMMAND = {
        "geni": ("can_image", "image generation"),
        "musicgeni": ("can_music", "music generation"),
        "videogeni": ("can_video", "video generation"),
        "torrents": ("can_torrent", "torrents"),
        "nyaa": ("can_torrent", "torrents"),
    }

    def _user_has_capability(self, attr: str) -> bool:
        """True if the current user may use a gated feature. System/internal callers
        (no user, e.g. load-balanced or bot paths) and admins are always allowed; the
        flags default True so existing users are unaffected until an admin restricts them."""
        if self.is_bot:
            return True
        u = self.user
        if u is None:
            return True
        if getattr(u, "is_admin", False) or getattr(u, "id", None) == 1:
            return True
        return bool(getattr(u, attr, True))

    async def _execute_command_inner(
        self,
        command: str,
        arg: str,
        last_prompt: Optional[str] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        attachments: Optional[list] = None,
        node_notify: Optional[Callable] = None,
    ) -> dict:
        """Execute a command and return the result.

        Args:
            command: The command name
            arg: Command arguments
            last_prompt: Last image generation prompt (for regeneration)
            stop_check: Callable to check if execution should stop
            attachments: List of (filename, data_bytes, content_type) tuples for mail
        """
        # Resolve aliases (e.g. "shot" → "screenshot") centrally so callers that match
        # commands literally (Telegram) accept them just like the web UI's parse_command.
        command = self.COMMAND_ALIASES.get(command, command)

        # Per-user feature access gate (Admin → Users). Admins + bot listeners are exempt.
        _cap = self._CAPABILITY_BY_COMMAND.get(command)
        if _cap and not self._user_has_capability(_cap[0]):
            return {"type": "text",
                    "content": f"⛔ You don't have access to {_cap[1]} on this server. "
                               f"Ask an admin to enable it for your account."}

        # Trailing subcommands on an effect, applied to its output in order:
        #   <effect> [zoom|shake] [meme <text>]
        # e.g. `dildo zoom meme top text`. `meme <text>` consumes the rest as the
        # caption; zoom/shake is a single token before it. Re-enter with all the
        # trailing parts stripped so the base effect renders untouched, then
        # transform the files (motion first, caption last so it sits on top).
        if command in self.MOTION_EFFECTS and arg:
            _toks = arg.split()
            _low = [t.lower() for t in _toks]
            from app.services import effects_service as _fx_chk
            # Strip the `meme <caption>` suffix FIRST so the caption's words (which may themselves
            # contain "char <name>" or "meme") are never re-parsed as modifiers. `meme` consumes to
            # the end; it doesn't apply to the meme effect itself (whole arg is the caption) nor thug
            # (which bakes its own "THUG LIFE" text).
            _meme_text = None
            if command not in ("meme", "thug") and "meme" in _low:
                _i = _low.index("meme")
                _meme_text = " ".join(_toks[_i + 1:]).strip()
                _toks, _low = _toks[:_i], _low[:_i]
            # `char <name>` (anywhere in the REMAINING pre-meme tokens) → overlay a cute character
            # bottom-right; only consumed when <name> is a real character.
            _character = None
            if "char" in _low:
                _ci = _low.index("char")
                if _ci + 1 < len(_toks) and _fx_chk._character_path(_toks[_ci + 1]):
                    _character = _toks[_ci + 1].lower()
                    _toks = _toks[:_ci] + _toks[_ci + 2:]
                    _low = [t.lower() for t in _toks]
            # Trailing motion cluster: at most one geometry motion (zoom/shake/
            # medshake/beginshake/pulse) plus the `trippy` colour pass, in either
            # order, at the very END of the arg. Only TRAILING tokens are consumed
            # (cap 2), so a caption word like "trippy" mid-text — e.g. `meme so
            # trippy bro` — is never mistaken for a motion. Geometry motions don't
            # stack (they'd fight over the crop); trippy layers on top.
            _motion = None
            _trippy = False
            for _ in range(2):
                if not _low or _low[-1] not in self.MOTION_ARGS:
                    break
                _t = _low.pop()
                _toks.pop()
                if _t == "trippy":
                    _trippy = True
                elif _motion is None:
                    _motion = _t
            if _motion or _trippy or _meme_text or _character:
                import asyncio
                from app.services import effects_service
                inner = await self._execute_command_inner(
                    command, " ".join(_toks), last_prompt, stop_check, attachments, node_notify,
                )
                if isinstance(inner, dict) and inner.get("type") == "files" and inner.get("files"):
                    files = inner["files"]
                    # Freeze-type motions (zoom/shake/pulse/alive) extract a single still frame,
                    # which would kill an already-animated effect — skip those for ANIMATED_EFFECTS.
                    # Overlay-type motions (glow) recolour/relight the real frames and KEEP the
                    # motion (like trippy), so they're allowed on animated effects too.
                    if _motion and (command not in self.ANIMATED_EFFECTS or _motion in self.OVERLAY_MOTIONS):
                        _apply = {
                            "zoom": effects_service.apply_zoom,
                            "shake": effects_service.apply_shake,
                            "medshake": effects_service.apply_medshake,
                            "beginshake": effects_service.apply_beginshake,
                            "pulse": effects_service.apply_pulse,
                            "glow": effects_service.apply_glow,
                            "alive": effects_service.apply_alive,
                        }.get(_motion, effects_service.apply_zoom)
                        files = await asyncio.to_thread(_apply, files)
                    # trippy recolours frame-by-frame (keeps motion) → safe to layer
                    # on top of a geometry motion, and even on animated effects.
                    if _trippy:
                        files = await asyncio.to_thread(effects_service.apply_trippy, files)
                    if _character:
                        files = await asyncio.to_thread(effects_service.apply_character, files, _character)
                    if _meme_text:
                        files = await asyncio.to_thread(effects_service.apply_meme_text, files, _meme_text)
                    inner["files"] = files
                return inner

        if command == "help":
            return await self._help_command()
        elif command == "search":
            return await self._search_command(arg)
        elif command == "images":
            return await self._images_command(arg)
        elif command == "files":
            return await self._files_command(arg)
        elif command == "geni":
            return await self._geni_command(arg, stop_check)
        elif command == "musicgeni":
            return await self._musicgeni_command(arg, stop_check)
        elif command == "videogeni":
            return await self._videogeni_command(arg, stop_check)
        elif command == "narrate":
            return await self._narrate_command(arg, stop_check)
        elif command == "yt":
            return await self._youtube_command(arg)
        elif command == "ytdl":
            return await self._youtube_download_command(arg)
        elif command == "torrents":
            return await self._torrents_command(arg)
        elif command == "nyaa":
            return await self._nyaa_command(arg)
        elif command == "news":
            return await self._news_command(arg)
        elif command == "dailynews":
            return await self._dailynews_command(arg)
        elif command == "logs":
            return await self._logs_command(arg, notify=node_notify)
        elif command == "mail":
            return await self._mail_command(arg, attachments=attachments)
        elif command == "todo":
            return await self._todo_command(arg)
        elif command == "translate":
            return await self._translate_command(arg, attachments=attachments)
        elif command == "4chan":
            return await self._4chan_command(arg)
        elif command == "compress":
            return await self._compress_command(attachments)
        elif command == "removebackground":
            return await self._removebackground_command(attachments)
        elif command == "clip":
            return await self._clip_command(arg, attachments)
        elif command == "convert":
            return await self._convert_command(arg, attachments)
        elif command == "flashcards":
            return await self._flashcards_command(arg, attachments)
        elif command == "ocr":
            return await self._ocr_command(attachments)
        elif command == "post":
            return await self._post_command(arg, attachments)
        elif command == "remind":
            return await self._remind_command(arg)
        elif command == "reminders":
            return await self._reminders_command()
        elif command == "pin":
            return await self._pin_command(arg)
        elif command == "pins":
            return await self._pins_command()
        elif command == "collage":
            return await self._collage_command(attachments)
        elif command == "meme":
            return await self._meme_command(arg, attachments)
        elif command == "dildo":
            return await self._dildo_command(attachments)
        elif command == "poo":
            return await self._poo_command(attachments)
        elif command == "cum":
            return await self._cum_command(attachments)
        elif command == "blood":
            return await self._blood_command(attachments)
        elif command == "bullethole":
            return await self._bullethole_command(attachments)
        elif command == "fire":
            return await self._fire_command(attachments)
        elif command == "alive":
            return await self._alive_command(arg, attachments)
        elif command == "glow":
            return await self._glow_command(arg, attachments)
        elif command == "prayer":
            return await self._prayer_command(attachments)
        elif command == "gay":
            return await self._gay_command(attachments)
        elif command == "blacked":
            return await self._blacked_command(attachments)
        elif command == "kosher":
            return await self._kosher_command(attachments)
        elif command == "blue":
            return await self._blue_command(attachments)
        elif command == "barked":
            return await self._barked_command(attachments)
        elif command == "hava":
            return await self._hava_command(attachments)
        elif command == "indian":
            return await self._indian_command(attachments)
        elif command == "yakety":
            return await self._yakety_command(attachments)
        elif command == "yamete":
            return await self._yamete_command(attachments)
        elif command == "curb":
            return await self._curb_command(attachments)
        elif command == "depressing":
            return await self._depressing_command(attachments)
        elif command == "fahh":
            return await self._fahh_command(attachments)
        elif command == "helpme":
            return await self._helpme_command(attachments)
        elif command == "gong":
            return await self._gong_command(attachments)
        elif command == "fbi":
            return await self._fbi_command(attachments)
        elif command == "redeem":
            return await self._redeem_command(attachments)
        elif command == "gigity":
            return await self._gigity_command(attachments)
        elif command == "beavis":
            return await self._beavis_command(attachments)
        elif command == "smell":
            return await self._smell_command(attachments)
        elif command == "hood":
            return await self._hood_command(attachments)
        elif command == "akbar":
            return await self._akbar_command(attachments)
        elif command == "retard":
            return await self._retard_command(attachments)
        elif command == "whoabuddy":
            return await self._whoabuddy_command(attachments)
        elif command == "seth":
            return await self._seth_command(attachments)
        elif command == "robocop":
            return await self._robocop_command(attachments)
        elif command == "titan":
            return await self._titan_command(attachments)
        elif command == "terminator":
            return await self._terminator_command(attachments)
        elif command == "reze":
            return await self._reze_command(attachments)
        elif command == "feliz":
            return await self._feliz_command(attachments)
        elif command == "sopranos":
            return await self._sopranos_command(attachments)
        elif command == "cheers":
            return await self._cheers_command(attachments)
        elif command == "munsters":
            return await self._munsters_command(attachments)
        elif command == "happydays":
            return await self._happydays_command(attachments)
        elif command == "dontwanttowait":
            return await self._dontwanttowait_command(attachments)
        elif command == "strangerthings":
            return await self._strangerthings_command(attachments)
        elif command == "adamsfamily":
            return await self._adamsfamily_command(attachments)
        elif command == "xmen":
            return await self._xmen_command(attachments)
        elif command == "futurama":
            return await self._futurama_command(attachments)
        elif command == "charliesangles":
            return await self._charliesangles_command(attachments)
        elif command == "differentstroke":
            return await self._differentstroke_command(attachments)
        elif command == "seinfeld":
            return await self._seinfeld_command(attachments)
        elif command == "onepiece":
            return await self._onepiece_command(attachments)
        elif command == "overtaken":
            return await self._overtaken_command(attachments)
        elif command == "freebird":
            return await self._freebird_command(attachments)
        elif command == "kanye":
            return await self._kanye_command(attachments)
        elif command == "darkness":
            return await self._darkness_command(attachments)
        elif command == "bike":
            return await self._bike_command(attachments)
        elif command == "jobs":
            return await self._jobs_command(attachments)
        elif command == "ree":
            return await self._ree_command(attachments)
        elif command == "liberal":
            return await self._liberal_command(attachments)
        elif command == "moving":
            return await self._moving_command(attachments)
        elif command == "harlem":
            return await self._harlem_command(attachments)
        elif command == "chimp":
            return await self._chimp_command(attachments)
        elif command == "consider":
            return await self._consider_command(attachments)
        elif command == "clay":
            return await self._clay_command(attachments)
        elif command == "wasteland":
            return await self._wasteland_command(attachments)
        elif command == "mixalot":
            return await self._mixalot_command(attachments)
        elif command == "thug":
            return await self._thug_command(attachments)
        elif command == "feltedtables":
            return await self._feltedtables_command(attachments)
        elif command == "node":
            return await self._node_command(arg, notify=node_notify)
        elif command == "budget":
            return await self._budget_command()
        elif command == "bills":
            return await self._bills_command(arg)
        elif command == "pay":
            return await self._pay_command(arg)
        elif command == "addbill":
            return await self._addbill_command(arg)
        elif command == "screenshot":
            return await self._screenshot_command(arg)
        else:
            return {"type": "text", "content": f"Unknown command: {command}"}

    async def _help_command(self) -> dict:
        """Show available commands and plugins"""
        help_text = "## Available Commands\n\n"

        # Built-in commands
        for cmd, desc in self.COMMANDS.items():
            help_text += f"**{cmd}** - {desc}\n"

        # Motion/colour modifiers — appended to any effect, not standalone commands.
        help_text += (
            "\n**Effect modifiers** (append to any effect): "
            "`zoom` `shake` `medshake` `beginshake` `pulse` motion, and/or `trippy` "
            "colours — e.g. `dildo zoom trippy`, `whoabuddy pulse trippy`.\n"
        )

        return {"type": "text", "content": help_text}

    def _format_size(self, size_bytes: int) -> str:
        """Format bytes to human readable size"""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} PB"

    async def check_youtube_url(self, message: str) -> Optional[dict]:
        """Check if message contains a YouTube URL and summarize it"""
        if not is_youtube_url(message):
            return None

        # Don't auto-summarize if user wants to download
        lower = message.lower()
        download_keywords = ["download", "ytdl", "mp3", "save", "get song", "get video", "download song", "download video"]
        if any(kw in lower for kw in download_keywords):
            return None

        urls = extract_youtube_urls(message)
        if not urls:
            return None

        # Summarize the first YouTube URL found
        success, result = await summarize_youtube(urls[0], self.chat_service)
        # Return result whether success or failure (so user sees error messages)
        return {"type": "text", "content": result}


def get_command_service(db: Session) -> CommandService:
    return CommandService(db)
