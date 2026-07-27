"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
from typing import TYPE_CHECKING

from ._common import Callable, ChatService, Optional, SearchService, Session, Tuple, extract_youtube_urls, is_youtube_url, logger, re, summarize_youtube
from .bill import _BillMixin
from .search import _SearchMixin
from .gen import _GenMixin
from .media import _MediaMixin
from .torrents import _TorrentsMixin
from .system import _SystemMixin
from .comms import _CommsMixin

if TYPE_CHECKING:   # annotation-only; app.models imports would cycle at runtime
    from app.models import User
from .productivity import _ProductivityMixin
from .effects1 import _Effects1Mixin
from .effects2 import _Effects2Mixin


class CommandService(_BillMixin, _SearchMixin, _GenMixin, _MediaMixin, _TorrentsMixin, _SystemMixin, _CommsMixin, _ProductivityMixin, _Effects1Mixin, _Effects2Mixin):
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
        "compress": "Compress attached image(s), video(s) or PDF(s)",
        "removebackground": "Remove the background from an attached image (transparent PNG): removebackground",
        "clip": "Clip an attached video: clip <start> <end> (e.g. clip 0:10 0:30)",
        "convert": "Convert image(s) to PDF or a PDF to images",
        "extractaudio": "Extract the audio from an attached video as MP3",
        "circlecrop": "Circle-crop an attached image (transparent PNG)",
        "ocr": "Read the text out of an attached image or PDF (OCR, no translation): ocr",
        "flashcards": "Make an interactive multiple-choice study quiz from an attached PDF/image/slide deck, or a URL: flashcards <url>",
        "post": "Share text (and an optional attached image) to your connected Misskey/Pleroma: post <text>",
        "remind": "Set a reminder in natural language: remind <what> <when> (e.g. remind open the oven in 10m, remind me next tuesday to call mom). Delivered in the web UI and Telegram.",
        "reminders": "Show your pending reminders (clickable to cancel): reminders",
        "pin": "Pin something you run often — a search or any command: pin <query|command> (e.g. pin latest xrp news, or pin screenshot https://google.com)",
        "pins": "Show your pins — click to run, or delete: pins",
        "collage": "Combine all attached images into one collage: collage (attach 2+ images)",
        "meme": "Add outlined white meme text to an attached image: meme <text>",
        "theraped": "Point at an attached image with the pointing-up meme character: theraped",
        "would": "Old man points up at an attached image saying WOULD: would",
        "shrug": "Rabbi shrugs at an attached image: \"Whaddya gonna do?\": shrug",
        "dildo": "Scatter dildos all over an attached image: dildo",
        "poo": "Scatter poop all over an attached image: poo",
        "cum": "Scatter cum all over an attached image: cum",
        "blood": "Splatter blood all over an attached image: blood",
        "bullethole": "Punch bullet holes all over an attached image: bullethole",
        "fire": "Set an attached image on fire: fire",
        "nakedman": "Overlay a fat cartoon man dancing (with a huge penis) on an attached image → 8s MP4: nakedman",
        "gay": "Stamp a big red GAY rubber stamp on an attached image: gay",
        "hag": "Stamp a big red HAG stamp + draw a cute old lady on an attached image: hag",
        "goon": "Stamp a big red GOON rubber stamp on an attached image: goon",
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
        "diarrhea": "Turn an attached image into a short MP4 set to the explosive diarrhea clip: diarrhea",
        "seth": "Turn an attached image into a short MP4 set to the seth clip: seth",
        "robocop": "Turn an attached image into a short MP4 set to the robocop clip: robocop",
        "titan": "Turn an attached image into a short MP4 set to the titan clip: titan",
        "terminator": "Turn an attached image into a short MP4 set to the terminator clip: terminator",
        "reze": "Turn an attached image into a short MP4 set to the reze clip: reze",
        "feliz": "Turn an attached image into a short MP4 set to the feliz clip: feliz",
        "sleepwell": "Turn an attached image into a short MP4 set to the Sleep Well clip: sleepwell",
        "horse": "Turn an attached image into a short MP4 set to the horse clip: horse",
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
        "node": "Agentic node mgmt: node <name> <cmd> | node all <cmd> | node agent <name> <goal> | node agent all <goal> | node list | node jobs | node log <id> | node kill <id>",
        # budget/bills/pay/addbill are GONE: the budget now lives in a Nostr event encrypted to the
        # user's own key (Discover → Budget), which the server cannot read or write. `bill` stays —
        # OCR and extraction are server work — but it only reads the photo and sets the reminder now.
        "bill": "Snap a bill: attach a photo/PDF and send bill — reads the vendor, total and due date, then `bill add` sets a reminder (add it to Discover → Budget with one tap)",
        "screenshot": "Full-page screenshot of a website: screenshot <url>",
        "poll": "Create a poll: poll <question> | <option 1> | <option 2> — 2 to 20 options, separated by |",
    }
    # Commands that USED to exist and now can't, with the reason. The budget moved into a Nostr event
    # encrypted to the user's own key, so the server genuinely cannot answer these — saying so beats
    # letting the model guess, and beats "Unknown command", which reads like a bug to someone who used
    # the feature yesterday.
    _BUDGET_MOVED = ("💰 Your budget moved into the app itself — open **Discover → Budget**.\n"
                     "It's stored in a Nostr event encrypted to your own key, so only your client can "
                     "read or change it. That's why I can't show or pay bills for you here any more.\n"
                     "The `bill` command still works: send a photo of a bill and I'll read it.")
    RETIRED_COMMANDS = {
        "budget": _BUDGET_MOVED,
        "bills": _BUDGET_MOVED,
        "pay": _BUDGET_MOVED,
        "addbill": _BUDGET_MOVED,
        "finance": _BUDGET_MOVED,
    }

    COMMAND_ALIASES = {
        "torrent": "torrents",
        "bt": "torrents",
        "yt-dlp": "ytdl",
        "ytdlp": "ytdl",
        "youtube": "yt",
        "nodes": "node",
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
        "collage", "meme", "theraped", "would", "shrug", "dildo", "poo", "cum", "blood", "bullethole", "fire", "nakedman", "gay", "hag", "goon",
        "blacked", "kosher", "blue", "barked", "hava", "indian", "yakety", "yamete",
        "curb", "depressing", "fahh", "helpme", "gong", "fbi", "redeem",
        "gigity", "beavis", "smell", "hood", "akbar", "retard", "whoabuddy", "diarrhea", "seth", "robocop", "titan", "terminator", "reze",
        "sopranos", "cheers", "munsters", "happydays", "dontwanttowait", "strangerthings", "adamsfamily", "xmen", "futurama", "charliesangles", "differentstroke", "seinfeld", "onepiece", "overtaken", "freebird", "kanye", "darkness", "bike", "jobs", "ree", "liberal", "moving",
        "harlem", "chimp", "consider", "clay", "wasteland", "mixalot", "thug",
        "feltedtables", "glow", "prayer", "alive", "feliz", "sleepwell", "horse",
    }
    MOTION_ARGS = ("zoom", "shake", "medshake", "beginshake", "trippy", "pulse", "glow", "alive")
    # Effects whose output is ALWAYS a video (they animate the still themselves).
    ANIMATED_EFFECTS = {"chimp", "clay", "reze", "nakedman"}
    OVERLAY_MOTIONS = {"glow"}
    # --- effect modifier combination rules (ONE source of truth: the command path, the
    # media API and the web studio all resolve combos through check_motion_combo) ---
    # MOVEMENTS animate the output; each one is a full re-render of every frame, so two of
    # them just fight over the same frames — at most one. LOOKS recolour/relight the real
    # frames, so they compose with a movement and with each other. Order: movement → glow →
    # trippy. `alive` is the one movement that genuinely can't run on a video (3D parallax
    # needs a still), so it's refused on the always-animated effects.
    MOVEMENT_MOTIONS = ("zoom", "shake", "medshake", "beginshake", "pulse", "alive")
    LOOK_MOTIONS = ("glow", "trippy")
    STILL_ONLY_MOTIONS = ("alive",)
    PHRASE_COMMANDS = {}

    def __init__(self, db: Session, user: Optional["User"] = None, is_bot: bool = False):
        self.db = db
        self.user = user
        # Bot-driven contexts (Pleroma/Misskey listeners) are configured in Admin → Bots,
        # so they're exempt from per-user feature gating. Pleroma/Misskey hit /api/generate-image
        # directly (never this service).
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
        # client. Used by the web UI (routes through parse_command).
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

        # RETIRED commands: match them so they get a straight answer instead of falling through to the
        # LLM, which has no idea the feature moved and will cheerfully invent a budget it cannot read.
        # Kept OUT of COMMANDS on purpose, so they stay out of `help` and the command lists.
        for cmd in self.RETIRED_COMMANDS:
            if lower == cmd or lower.startswith(f"{cmd} "):
                return cmd, message[len(cmd) + 1:].strip() if lower != cmd else ""

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

    @classmethod
    def check_motion_combo(cls, command: str, mods) -> Tuple[list, Optional[str]]:
        """Resolve effect modifiers against the combination rules.

        Returns `(mods_in_apply_order, error)`. `error` is a user-facing message when the
        combination can't render — a second movement, `alive` on an effect that outputs a
        video, or a modifier repeating the base effect. Callers should refuse the whole
        command in that case: the old parsers silently kept ONE motion and dropped the rest,
        so `curb zoom glow` quietly rendered as plain `curb glow`.
        """
        seen, picked = set(), []
        for m in (mods or []):
            m = (m or "").strip().lower()
            if m in cls.MOTION_ARGS and m not in seen:
                seen.add(m)
                picked.append(m)
        moves = [m for m in picked if m in cls.MOVEMENT_MOTIONS]
        if len(moves) > 1:
            return [], (f"⚠️ '{moves[0]}' and '{moves[1]}' are both movements — an effect takes "
                        f"only one. Pick one, then add glow and/or trippy on top.")
        _still = [m for m in moves if m in cls.STILL_ONLY_MOTIONS]
        if _still and command in cls.ANIMATED_EFFECTS:
            return [], (f"⚠️ '{_still[0]}' needs a still image and '{command}' outputs a video, so "
                        f"it would do nothing. zoom, shake, pulse, glow and trippy all work on it.")
        if command in picked:
            return [], f"⚠️ '{command}' is already the effect — drop the extra '{command}'."
        # movement first (it builds the frames), then the looks over those frames in a FIXED
        # order (glow's light sweep, then trippy's hue cycle over the lot) — so the same set of
        # modifiers renders identically no matter what order they were typed in.
        return moves + [m for m in cls.LOOK_MOTIONS if m in picked], None

    @staticmethod
    def motion_applier(mod: str):
        """effects_service function that applies a single modifier (used by every path)."""
        from app.services import effects_service
        return {
            "zoom": effects_service.apply_zoom,
            "shake": effects_service.apply_shake,
            "medshake": effects_service.apply_medshake,
            "beginshake": effects_service.apply_beginshake,
            "pulse": effects_service.apply_pulse,
            "alive": effects_service.apply_alive,
            "glow": effects_service.apply_glow,
            "trippy": effects_service.apply_trippy,
        }.get(mod)

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
        if command in self.RETIRED_COMMANDS:
            return {"type": "text", "content": self.RETIRED_COMMANDS[command]}
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
        from app.services import settings_store
        from app.services import media_service
        try:
            s = settings_store.get("effect_outro_enabled")
            if s is not None and str(s).strip().lower() in ("false", "0", "no", "off"):
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
        #   <effect> [movement] [glow] [trippy] [char <name>] [meme <text>]
        # e.g. `dildo zoom glow meme top text`. `meme <text>` consumes the rest as the
        # caption; the modifiers are the tokens before it. Re-enter with all the
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
            # Trailing modifier cluster: one movement plus the looks (glow/trippy), in any
            # order, at the very END of the arg. Only TRAILING tokens are consumed, so a
            # caption word like "trippy" mid-text — e.g. `meme so trippy bro` — is never
            # mistaken for a modifier. The cap is deliberately LOOSER than the 3 that can
            # validly combine: check_motion_combo can only refuse a bad combination it can
            # see, and stopping at 3 let `curb zoom shake glow trippy` drop `zoom` into the
            # effect's own arg instead of reporting the two-movement conflict.
            _mods = []
            for _ in range(len(self.MOTION_ARGS)):
                if not _low or _low[-1] not in self.MOTION_ARGS:
                    break
                _mods.insert(0, _low.pop())
                _toks.pop()
            # Refuse combinations that can't render, with the reason — rather than rendering
            # a silently different effect (the old parse kept one motion and dropped the rest).
            _mods, _combo_err = self.check_motion_combo(command, _mods)
            if _combo_err:
                return {"type": "text", "content": _combo_err}
            if _mods or _meme_text or _character:
                import asyncio
                from app.services import effects_service
                inner = await self._execute_command_inner(
                    command, " ".join(_toks), last_prompt, stop_check, attachments, node_notify,
                )
                if isinstance(inner, dict) and inner.get("type") == "files" and inner.get("files"):
                    files = inner["files"]
                    # `alive` is 3D parallax on a STILL — check_motion_combo catches the effects
                    # that ALWAYS output video, but plenty produce a clip only for some inputs.
                    # Say it was skipped rather than returning an un-parallaxed file.
                    if "alive" in _mods and not any(
                            (f.get("content_type") or "").startswith("image/") for f in files):
                        inner["content"] = ((inner.get("content") or "").rstrip()
                                            + f"\n\n⚠️ 'alive' needs a still image, but {command} "
                                              f"produced a video — it was skipped.")
                    # check_motion_combo ordered these: the movement builds the frames, then
                    # glow/trippy recolour those real frames (keeping the motion).
                    for _mod in _mods:
                        _apply = self.motion_applier(_mod)
                        if _apply:
                            files = await asyncio.to_thread(_apply, files)
                    if _character:
                        files = await asyncio.to_thread(effects_service.apply_character, files, _character)
                    if _meme_text:
                        files = await asyncio.to_thread(effects_service.apply_meme_text, files, _meme_text)
                    inner["files"] = files
                return inner

        # Node LB for ffmpeg effects. The Meme Builder balances its own renders in client.py, but
        # this path — AI Chat and Telegram — used to run every effect on whichever node held the
        # session while the rest of the fleet idled. Round-robin here, past the modifier parsing
        # above, so a peer renders the BASE effect and the modifiers are applied to what comes back.
        if ((command in self.MOTION_EFFECTS or command in self.ANIMATED_EFFECTS)
                and attachments and not getattr(self, "_effects_no_forward", False)):
            try:
                from app.services import effects_factory, settings_store as _ss_fx
                _fx = await effects_factory.run_effect_balanced(
                    _ss_fx.get("chat_server_urls", "") or "", command, arg, attachments)
                if _fx is not None:
                    return _fx
            except Exception as e:
                logger.warning("[EFFECTS] LB skipped for %s: %s", command, e)

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
        # NOTE: `4chan` is no longer offered — it was dropped from COMMANDS (so it's out of the help
        # sheet and no longer parsed as a command). The branch is kept because the client's Discover →
        # 4chan VIEW is a separate feature and still renders this response type.
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
        elif command == "extractaudio":
            return await self._extractaudio_command(attachments)
        elif command == "circlecrop":
            return await self._circlecrop_command(attachments)
        elif command == "flashcards":
            return await self._flashcards_command(arg, attachments)
        elif command == "ocr":
            return await self._ocr_command(attachments)
        elif command == "post":
            return await self._post_command(arg, attachments)
        elif command == "remind":
            return await self._remind_command(arg, attachments)
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
        elif command == "theraped":
            return await self._theraped_command(attachments)
        elif command == "would":
            return await self._would_command(attachments)
        elif command == "shrug":
            return await self._shrug_command(attachments)
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
        elif command == "nakedman":
            return await self._nakedman_command(attachments)
        elif command == "alive":
            return await self._alive_command(arg, attachments)
        elif command == "glow":
            return await self._glow_command(arg, attachments)
        elif command == "prayer":
            return await self._prayer_command(attachments)
        elif command == "gay":
            return await self._gay_command(attachments)
        elif command == "hag":
            return await self._hag_command(attachments)
        elif command == "goon":
            return await self._goon_command(attachments)
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
        elif command == "diarrhea":
            return await self._diarrhea_command(attachments)
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
        elif command == "sleepwell":
            return await self._sleepwell_command(attachments)
        elif command == "horse":
            return await self._horse_command(attachments)
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
        elif command == "bill":
            return await self._bill_command(arg, attachments)
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
            "\n**Effect modifiers** (append to any effect): ONE movement — "
            "`zoom` `shake` `medshake` `beginshake` `pulse` `alive` — plus any of the looks "
            "`glow` `trippy`, which stack on top. E.g. `dildo zoom trippy`, "
            "`whoabuddy pulse glow`. A second movement is refused rather than half-applied, "
            "and `alive` (3D parallax needs a still) is refused on the effects that always "
            "output a video — `chimp` `clay` `reze` — where the other movements work fine.\n"
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
