"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
from ._common import Optional, _capture_full_page, _url_is_safe_to_fetch, check_ytdlp_available, download_mp3_and_save_to_storage, download_video_and_save_to_storage, download_ytdl_bytes, extract_download_urls, extract_youtube_urls, format_download_result, logger, re, summarize_youtube


class _MediaMixin:
    async def _screenshot_command(self, arg: str) -> dict:
        """Capture a full-page screenshot of a website via headless Chrome.

        Returns the shared `generated_image` shape so every channel renders it the
        same way: inline in the web UI (with a save button), a photo/document on
        and Telegram.
        """
        import asyncio
        import base64

        url = arg.strip().split()[0] if arg.strip() else ""
        if not url:
            return {"type": "text", "content": "Usage: `screenshot <url>` — e.g. `screenshot example.com`"}
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url

        # SSRF guard: refuse internal/private targets before handing the URL to the
        # browser. Resolved off the event loop since it does a blocking DNS lookup.
        # `screenshot_allowed_hosts` (admin setting) lets the operator's own domains
        # that resolve to a LAN IP via split-horizon DNS (e.g. poster.place) through.
        from app.services import settings_store
        allow_value = settings_store.get("screenshot_allowed_hosts")
        allowed_hosts = re.split(r"[\s,]+", allow_value.strip()) if allow_value else []
        if not await asyncio.to_thread(_url_is_safe_to_fetch, url, allowed_hosts):
            return {"type": "text", "content": f"🚫 Refusing to capture {url} — it resolves to a private or internal address."}

        import subprocess
        try:
            # Backstop above the browser's own timeout (+ settle) so the handler
            # always replies even if the page render stalls.
            png = await asyncio.wait_for(asyncio.to_thread(_capture_full_page, url), timeout=100)
        except (asyncio.TimeoutError, subprocess.TimeoutExpired):
            return {"type": "text", "content": f"📸 Timed out capturing {url} — the page took too long to render."}
        except Exception as e:
            logger.error(f"[screenshot] {url}: {e}", exc_info=True)
            msg = str(e)
            if "no headless browser" in msg.lower():
                return {"type": "text", "content": f"📸 Couldn't capture {url}: no headless browser installed on the server (install google-chrome-stable)."}
            first_line = next((ln for ln in msg.splitlines() if ln.strip()), "unknown error")
            return {"type": "text", "content": f"📸 Couldn't capture {url}: {first_line}"}

        return {
            "type": "generated_image",
            "content": f"📸 {url}",
            "image": base64.b64encode(png).decode("ascii"),
            "prompt": url,
            # Telegram compresses photos (tiny/unreadable for tall pages) — deliver as a
            # full-resolution document instead. Ignored by the web UI renderer.
            "prefer_document": True,
        }

    async def _youtube_command(self, arg: str) -> dict:
        """Summarize a YouTube video transcript"""
        if not arg:
            return {
                "type": "text",
                "content": """## YouTube Commands

**Summarize a video:**
`yt <url>` - Get AI summary of video transcript

**Download:**
- `ytdl <url>` - Download as MP3 to Music (default)
- `ytdl mp3 <url>` - Download as MP3 to Music
- `ytdl video <url>` - Download as video (MP4) to YouTube Videos

Example: `yt https://youtube.com/watch?v=...`""",
            }

        # Extract URL
        urls = extract_youtube_urls(arg)
        if not urls:
            return {"type": "text", "content": "Could not find a valid YouTube URL."}

        target_url = urls[0]
        success, result = await summarize_youtube(target_url, self.chat_service)
        return {"type": "text", "content": result}

    async def _youtube_download_command(self, arg: str) -> dict:
        """Download a YouTube video (audio or video) to storage"""

        if not arg:
            return {
                "type": "text",
                "content": """## YouTube / X (Twitter) / Nitter Download

**Usage:**
- `ytdl <url>` - Download as MP3 to Music (default)
- `ytdl mp3 <url>` - Download as MP3 to Music
- `ytdl video <url>` - Download as video (MP4) to folder

**Supported:** YouTube, X.com (Twitter), and Nitter links (any instance; resolved via X.com).

**Examples:**
- `ytdl https://youtube.com/watch?v=...` - Download as MP3
- `ytdl video https://x.com/i/status/123...` - Download X video
- `ytdl https://nitter.net/user/status/123...` - Download a Nitter post (as MP3)

Files are saved to your Storage.""",
            }

        # Check if yt-dlp is available
        if not check_ytdlp_available():
            return {"type": "text", "content": "❌ yt-dlp not installed. Install with: `pip install yt-dlp`"}

        # Parse: "ytdl video <url>" | "ytdl mp3 <url>" | "ytdl <url>" (default = MP3), plus optional
        # trailing `clip <start> <end>` and/or `compress` modifiers (video only) — same syntax as
        # the bot ytdl endpoints. When clip/compress is present we trim/shrink and deliver the
        # result INLINE in the chat (like the standalone `clip` command), instead of saving to
        # storage and returning a link.
        import re as _re
        full = arg.strip()
        # Pull off the clip/compress modifiers so they don't confuse URL/mode parsing.
        clip_str = None
        m = _re.search(r'\bclip\s+(\S+)\s+(\S+)', full, _re.IGNORECASE)
        if m:
            clip_str = f"{m.group(1)} {m.group(2)}"
            full = (full[:m.start()] + full[m.end():]).strip()
        want_compress = bool(_re.search(r'\bcompress\b', full, _re.IGNORECASE))
        if want_compress:
            full = _re.sub(r'\bcompress\b', '', full, flags=_re.IGNORECASE).strip()

        parts = full.split(maxsplit=1)
        first = parts[0].lower() if parts else ""
        if first == "video":
            url_arg = parts[1] if len(parts) > 1 else ""
            if not url_arg:
                return {"type": "text", "content": "Usage: `ytdl video <url>`\n\nExample: `ytdl video https://youtube.com/watch?v=...`"}
            as_mp3 = False
        elif first == "mp3":
            url_arg = parts[1] if len(parts) > 1 else ""
            if not url_arg:
                return {"type": "text", "content": "Usage: `ytdl mp3 <url>`\n\nExample: `ytdl mp3 https://youtube.com/watch?v=...`"}
            as_mp3 = True
        else:
            url_arg = full
            as_mp3 = True  # default: MP3

        # clip/compress only make sense for video.
        if (clip_str or want_compress):
            as_mp3 = False

        urls = extract_download_urls(url_arg)
        if not urls:
            return {"type": "text", "content": "Could not find a valid YouTube, X (Twitter), or Nitter URL. Example: `ytdl https://x.com/i/status/123`, `ytdl https://nitter.net/user/status/123`, or `ytdl https://youtube.com/watch?v=...`"}

        target_url = urls[0]

        # Inline delivery path: a trimmed/compressed video goes straight into the chat (like the
        # `clip` command), not to storage.
        if clip_str or want_compress:
            import asyncio
            logger.info(f"[ytdl] Command: inline video url={target_url!r} clip={clip_str!r} compress={want_compress} user_id={self.user.id}")
            res = await asyncio.to_thread(
                download_ytdl_bytes, target_url, video=True, clip=clip_str, compress=want_compress,
            )
            if not res.get("ok"):
                return {"type": "text", "content": f"❌ {res.get('error', 'download failed')}"}
            summary = "✂️ Clipped video" if clip_str else "🗜 Compressed video"
            return {
                "type": "files",
                "content": summary,
                "files": [{
                    "filename": res["filename"],
                    "data": res["data"],
                    "content_type": res.get("mime", "video/mp4"),
                }],
            }

        if as_mp3:
            import asyncio
            logger.info(f"[ytdl] Command: mp3 inline url={target_url!r} user_id={self.user.id}")
            # Deliver the MP3 INLINE (a playable/clickable audio item in chat), like the clipped
            # video. Fall back to saving in Storage when it's too big to hold/serve inline.
            res = await asyncio.to_thread(download_ytdl_bytes, target_url, video=False)
            if res.get("ok"):
                return {
                    "type": "files",
                    "content": "🎵 Audio",
                    "files": [{
                        "filename": res["filename"],
                        "data": res["data"],
                        "content_type": res.get("mime", "audio/mpeg"),
                    }],
                }
            # Too large / failed → save to Storage and return the link (original behavior).
            logger.info(f"[ytdl] mp3 inline fell back to storage: {res.get('error')}")
            result = await download_mp3_and_save_to_storage(
                url=target_url,
                user_id=self.user.id,
                db=self.db,
                subfolder="Music",
            )
            return {"type": "text", "content": format_download_result(result)}
        else:
            import asyncio
            logger.info(f"[ytdl] Command: video inline url={target_url!r} user_id={self.user.id}")
            # Deliver the video INLINE, exactly like the mp3 and the clipped/compressed paths above.
            # It used to go straight to a storage DIRECTORY and report a filesystem path — pre-Blossom
            # behaviour that left the download unreachable from the client: not playable in the chat,
            # and absent from Files (nothing references an artifact, so there was nothing to list).
            # Inline means it lands in Blossom like every other artifact. Storage stays as the
            # fallback for something too big to hold in memory.
            res = await asyncio.to_thread(download_ytdl_bytes, target_url, video=True)
            if res.get("ok"):
                return {
                    "type": "files",
                    "content": "🎬 Video",
                    "files": [{
                        "filename": res["filename"],
                        "data": res["data"],
                        "content_type": res.get("mime", "video/mp4"),
                    }],
                }
            logger.info(f"[ytdl] video inline fell back to storage: {res.get('error')}")
            result = await download_video_and_save_to_storage(
                url=target_url,
                user_id=self.user.id,
                db=self.db,
                subfolder="YouTube Videos",
            )

        return {"type": "text", "content": format_download_result(result)}

    async def _translate_command(self, arg: str, attachments: Optional[list] = None) -> dict:
        """Translate an uploaded image/PDF (OCR), or the last response, or an email."""
        if not self.user:
            return {"type": "text", "content": "Please log in to use translate."}

        # Uploaded image/PDF wins: OCR it and translate the whole thing.
        if attachments:
            return await self._translate_attachments(arg, attachments)

        # A URL → fetch the page's real text and translate it (reliable; no OCR).
        _url_match = re.search(r'https?://\S+', arg)
        if _url_match:
            return await self._translate_url(arg, _url_match.group(0).rstrip('.,)>'))

        parts = arg.strip().split()
        if not parts:
            return {
                "type": "text",
                "content": "Usage:\n- `translate <language>` - Translate last response\n- `translate email <language>` - Translate last email\n\nExamples: `translate spanish`, `translate email japanese`",
            }

        # Check if translating email
        if parts[0].lower() == "email":
            language = parts[1] if len(parts) > 1 else "English"
            # Get last email from conversation context
            # For now, suggest using mail translate command
            return {
                "type": "text",
                "content": f"To translate an email, use:\n`mail translate <account> <id> {language}`\n\nFirst check your mail with `mail` to get the email ID.",
            }

        def _unquote(s: str) -> str:
            s = (s or "").strip()
            if len(s) >= 2 and s[0] in "\"'“‘" and s[-1] in "\"'”’":
                return s[1:-1].strip()
            return s

        _known_langs = {
            "english", "spanish", "french", "german", "italian", "portuguese", "dutch", "russian",
            "japanese", "chinese", "mandarin", "cantonese", "korean", "arabic", "hindi", "bengali",
            "punjabi", "urdu", "turkish", "vietnamese", "thai", "indonesian", "malay", "tagalog",
            "filipino", "polish", "ukrainian", "czech", "slovak", "romanian", "hungarian", "greek",
            "hebrew", "swedish", "norwegian", "danish", "finnish", "icelandic", "latin", "persian",
            "farsi", "swahili", "tamil", "telugu", "gujarati", "marathi", "serbian", "croatian",
            "bulgarian", "catalan", "esperanto", "welsh", "irish", "latvian", "lithuanian",
            "estonian", "slovenian", "albanian", "macedonian", "georgian", "armenian", "mongolian",
        }

        # Target-first directive form (the way users actually phrase it, esp. on Telegram):
        # `translate to <lang>: <text>`, `translate <lang>: <text>`, `translate from <src> to
        # <lang>: <text>`. The instruction comes first, then a colon, then the text. Without this,
        # the colon form fell through and translated the WHOLE arg (incl. "to Japanese:") to English.
        _dir = re.match(
            r'^(?:from\s+([A-Za-z][A-Za-z\- ]*?)\s+)?(?:to\s+)?([A-Za-z][A-Za-z\- ]*?)\s*:\s*(.+)$',
            arg.strip(), re.IGNORECASE | re.DOTALL)
        if _dir and _dir.group(2).strip().lower() in _known_langs and _unquote(_dir.group(3)):
            _src = (_dir.group(1) or "").strip().title() or None
            return await self._translate_text(
                _unquote(_dir.group(3)), _dir.group(2).strip().title(), source=_src)

        # Inline form `translate <text> [from <src>] to <lang>` (the documented syntax): translate
        # the GIVEN text, not the last response. Requires non-empty text and a target language, so
        # `translate spanish` / `translate to spanish` still fall through to last-response translation.
        # The target is 1-2 words after "to"; any trailing instruction ("... and explain") is dropped;
        # surrounding quotes on the text are stripped.
        _inline = re.match(
            r'^(.+?)(?:\s+from\s+([A-Za-z][A-Za-z\- ]*?))?\s+to\s+([A-Za-z][A-Za-z\- ]*?)(?:\s+and\s+.*)?$',
            arg.strip(), re.IGNORECASE)
        if _inline and _unquote(_inline.group(1)) and _inline.group(3).strip():
            _src = (_inline.group(2) or "").strip().title() or None
            return await self._translate_text(
                _unquote(_inline.group(1)), _inline.group(3).strip().title(), source=_src)

        # No `to <lang>`. If the whole arg is just a known language name, translate the LAST
        # response into it ("translate spanish"). Otherwise the arg is TEXT to translate to English
        # ("translate dame desuyo") — do NOT treat it as a language for the last response, which
        # mis-translated the previous command's output (e.g. a nyaa listing) instead of the words.
        _norm = re.sub(r"^to\s+", "", _unquote(arg), flags=re.IGNORECASE).strip().lower()
        if _norm not in _known_langs:
            return await self._translate_text(_unquote(arg), "English")

        # Translate the last assistant response.
        language = self._parse_language(arg)
        from app.models import Conversation, Message
        conversation = (
            self.db.query(Conversation)
            .filter(Conversation.user_id == self.user.id)
            .order_by(Conversation.updated_at.desc())
            .first()
        )
        if not conversation:
            return {"type": "text", "content": "No conversation found to translate."}
        from app.services import chat_history
        _msgs = await chat_history.load(self.db, self.user, conversation.id)
        last_msg = next((m for m in reversed(_msgs) if m.get("role") == "assistant" and m.get("content")), None)
        if not last_msg:
            return {"type": "text", "content": "No previous response to translate."}
        return await self._translate_text(last_msg["content"], language)

    @staticmethod
    def _parse_language(arg: str) -> str:
        """'spanish' / 'to spanish' / '' → 'Spanish' / 'Spanish' / 'English'."""
        lang = (arg or "").strip()
        if lang.lower().startswith("to "):
            lang = lang[3:].strip()
        return (lang or "English").title()

    async def _translate_text(self, text: str, language: str, *, kind: str = "text",
                              source: Optional[str] = None) -> dict:
        """Translate `text` into `language`, raising the output budget so long content
        isn't cut off. `kind` labels the prompt ('text' / 'web page text'); `source` is an optional
        known source language. Shared by the last-response, URL and attachment translate paths."""
        _from = f" from {source}" if source else ""
        messages = [
            {"role": "system", "content": (
                f"You are a translation engine. Translate the following {kind}{_from} INTO {language}. "
                f"Your ENTIRE reply MUST be written in {language} — never reply in the source language, and "
                f"never just copy the input. If a word has no {language} equivalent, transliterate it. "
                "Translate ALL of it — every line and list item — do not summarize, omit, add commentary, or "
                "stop early. Preserve the original line breaks and formatting. Output only the translation.")},
            {"role": "user", "content": f"Translate this into {language}:\n\n" + (text or "")[:24000]},
        ]
        # Output is about as long as the input; the default ~2048 cap stops long pages early.
        _orig_np = self.chat_service.num_predict
        self.chat_service.num_predict = max(_orig_np, 8192)
        try:
            translation = await self.chat_service.chat(messages)
            return {"type": "text", "content": f"## Translation ({language})\n\n{translation}"}
        except Exception as e:
            logger.error(f"Translation error: {e}")
            return {"type": "text", "content": f"Translation failed: {str(e)}"}
        finally:
            self.chat_service.num_predict = _orig_np

    async def _translate_url(self, arg: str, url: str) -> dict:
        """Fetch a web page's text and translate the whole thing (no OCR).

        `translate <url>` (→ English) or `translate <url> to <language>`.
        """
        language = self._parse_language(arg.replace(url, ""))
        try:
            fetched = await self.search_service.fetch_urls([url], max_urls=1)
        except Exception as e:
            return {"type": "text", "content": f"Couldn't fetch {url}: {e}"}
        if not fetched or fetched[0].get("error") or not fetched[0].get("content"):
            err = (fetched[0].get("error") if fetched else None) or "no readable text found"
            return {"type": "text", "content": f"Couldn't fetch text from {url}: {err}"}
        title = fetched[0].get("title", "")
        body = (f"Title: {title}\n\n" if title else "") + fetched[0]["content"]
        return await self._translate_text(body, language, kind="web page text")

    async def _translate_attachments(self, arg: str, attachments: list) -> dict:
        """OCR uploaded image(s)/PDF(s) and translate the FULL extracted text.

        Shared by the web UI and Telegram (`translate <lang>` + an upload).
        Returns an `error: 'no_text'` field when nothing could be extracted (e.g. a
        Telegram-compressed photo) so callers can show a tailored hint.
        """
        import base64 as _b64
        from app.services.document_service import extract_image_text, extract_pdf_text
        from app.services.media_service import is_image, is_pdf

        language = self._parse_language(arg)
        parts = []
        for fn, data, ct in attachments:
            try:
                b64 = _b64.b64encode(data).decode()
            except Exception:
                continue
            if is_pdf(fn, ct):
                parts.append(extract_pdf_text(b64) or "")
            elif is_image(fn, ct):
                parts.append(extract_image_text(b64) or "")
        src = "\n\n".join(p for p in parts if p).strip()
        if not src:
            return {"type": "text", "error": "no_text",
                    "content": "Couldn't extract any text to translate from the upload."}
        return await self._translate_text(src, language)

    async def _compress_command(self, attachments: Optional[list]) -> dict:
        """Compress attached image(s), video(s) or PDF(s) and return the smaller files."""
        if not attachments:
            return {
                "type": "text",
                "content": "Attach an image, video or PDF, then send `compress` to shrink it.",
            }
        import asyncio
        from app.services.media_service import compress_attachments

        # ffmpeg transcodes can block; run off the event loop.
        outputs, summary = await asyncio.to_thread(compress_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _removebackground_command(self, attachments: Optional[list]) -> dict:
        """Remove the background from attached image(s), returning transparent PNG(s)."""
        if not attachments:
            return {
                "type": "text",
                "content": "Attach an image, then send `removebackground` to cut out the background.",
            }
        import asyncio
        from app.services.media_service import remove_background_attachments
        # rembg/onnxruntime can block; run off the event loop.
        outputs, summary = await asyncio.to_thread(remove_background_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    # A handful of edge-tts voices under names a person will actually type. The full
    # catalogue is hundreds of `xx-YY-NameNeural` strings (GET /api/tts/voices), which is
    # a dropdown, not something anyone types into a chat box — so the command takes either
    # one of these or a raw voice name, and the Meme Builder offers the same short list.
    TALK_VOICES = {
        "guy": "en-US-GuyNeural",
        "aria": "en-US-AriaNeural",
        "jenny": "en-US-JennyNeural",
        "eric": "en-US-EricNeural",
        "ana": "en-US-AnaNeural",              # child voice — the funny one
        "ryan": "en-GB-RyanNeural",
        "sonia": "en-GB-SoniaNeural",
        "william": "en-AU-WilliamNeural",
        "natasha": "en-AU-NatashaNeural",
        "liam": "en-CA-LiamNeural",
        "prabhat": "en-IN-PrabhatNeural",
        "neerja": "en-IN-NeerjaNeural",
    }

    async def _talk_command(self, arg: str, attachments: Optional[list]) -> dict:
        """Make an attached face say a line: `talk <what to say> | <voice>`.

        Two halves, and they are deliberately in different places: the SPEECH is the
        app's existing edge-tts service (so it inherits the configured rate/pitch and the
        voice catalogue), and the MOUTH is effects_service.talk — a CPU puppet warp, no
        GPU and no lip-sync model. See that module for why.
        """
        import asyncio
        import base64
        import os
        import tempfile
        from app.services.effects_service.talk import talk_attachments, TALK_MAX_CHARS
        from app.services.tts_service import TTSService

        if not attachments:
            return {
                "type": "text",
                "content": "Attach a picture of a face, then send `talk <what to say>` — "
                           "e.g. `talk I am the president now`. Add `| guy` to pick a voice.",
            }
        text, _, voice_raw = (arg or "").partition("|")
        text = text.strip()
        if not text:
            return {"type": "text", "content": "What should they say? `talk <what to say>`"}
        if len(text) > TALK_MAX_CHARS:
            return {"type": "text",
                    "content": f"That's a speech, not a meme — keep it under {TALK_MAX_CHARS} characters."}
        voice_key = voice_raw.strip().lower()
        # An unknown short name would otherwise be handed to edge-tts as a voice id and
        # fail there as an opaque error; a raw `xx-YY-...Neural` is passed through.
        voice = self.TALK_VOICES.get(voice_key) or (voice_raw.strip() if "-" in voice_raw else None)
        if voice_key and not voice:
            return {"type": "text",
                    "content": "Unknown voice. Try one of: " + ", ".join(sorted(self.TALK_VOICES))}

        audio_b64 = await TTSService(self.db).generate_speech(text, voice)
        if not audio_b64:
            return {"type": "text", "content": "❌ Couldn't generate the speech — TTS is unavailable."}

        with tempfile.TemporaryDirectory(prefix="talk_") as td:
            speech = os.path.join(td, "speech.mp3")
            with open(speech, "wb") as fh:
                fh.write(base64.b64decode(audio_b64))
            # ffmpeg + per-frame Pillow work blocks; keep it off the event loop.
            outputs, summary = await asyncio.to_thread(talk_attachments, attachments, speech)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _clip_command(self, arg: str, attachments: Optional[list]) -> dict:
        """Trim an attached video to a [start, end] span: `clip <start> <end>`.

        Times accept seconds or M:SS / H:MM:SS. Telegram drives an interactive
        flow; the web UI passes both times in the command argument.
        """
        from app.services.media_service import clip_attachment, parse_timecode, is_video

        if not attachments:
            return {
                "type": "text",
                "content": "Attach a video, then send `clip <start> <end>` — e.g. `clip 0:10 0:30`.",
            }
        if not any(is_video(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "No video attachment found to clip."}

        parts = (arg or "").split()
        if len(parts) < 2:
            return {
                "type": "text",
                "content": "Usage: `clip <start> <end>` — e.g. `clip 0:10 0:30` or `clip 90 120`.",
            }
        start = parse_timecode(parts[0])
        end = parse_timecode(parts[1])
        if start is None or end is None:
            return {
                "type": "text",
                "content": "Couldn't read those times. Use seconds or M:SS / H:MM:SS, e.g. `clip 0:10 1:30`.",
            }
        if end <= start:
            return {"type": "text", "content": "The end time must be after the start time."}

        import asyncio
        # ffmpeg clipping can block; run it off the event loop.
        outputs, summary = await asyncio.to_thread(clip_attachment, attachments, start, end)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _convert_command(self, arg: str, attachments: Optional[list]) -> dict:
        """Convert attached image(s) to a PDF, or a PDF to images."""
        if not attachments:
            return {
                "type": "text",
                "content": (
                    "Attach file(s) then send `convert`:\n"
                    "- image(s) → a single PDF\n"
                    "- a PDF → one PNG per page"
                ),
            }
        import asyncio
        from app.services.media_service import convert_attachments

        outputs, summary = await asyncio.to_thread(convert_attachments, attachments, arg)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _extractaudio_command(self, attachments: Optional[list]) -> dict:
        """Extract the audio track of attached video(s) to MP3."""
        if not attachments:
            return {"type": "text", "content": "Attach a video, then send `extractaudio` to pull its audio as MP3."}
        import asyncio
        from app.services.media_service import extract_audio_attachments
        outputs, summary = await asyncio.to_thread(extract_audio_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _circlecrop_command(self, attachments: Optional[list]) -> dict:
        """Circle-crop attached image(s) into transparent PNG(s)."""
        if not attachments:
            return {"type": "text", "content": "Attach an image, then send `circlecrop` to crop it into a circle."}
        import asyncio
        from app.services.media_service import circle_crop_attachments
        outputs, summary = await asyncio.to_thread(circle_crop_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _ocr_command(self, attachments: Optional[list]) -> dict:
        """Read the text out of attached image(s)/PDF(s) via OCR — no translation. Mirrors the
        Telegram `media:ocr` flow so the web 🔤 Read text button has a command to run."""
        import base64 as _b64
        from app.services.document_service import extract_image_text, extract_pdf_text
        from app.services.media_service import is_image, is_pdf
        if not attachments:
            return {"type": "text", "content": "Attach an image or PDF, then send `ocr` to read its text."}
        parts = []
        for fn, data, ct in attachments:
            try:
                b64 = _b64.b64encode(data).decode() if isinstance(data, (bytes, bytearray)) else data
            except Exception:
                continue
            if is_pdf(fn, ct):
                parts.append(extract_pdf_text(b64) or "")
            elif is_image(fn, ct):
                parts.append(extract_image_text(b64) or "")
        text = "\n\n".join(p for p in parts if p and p.strip()).strip()
        if not text:
            return {"type": "text", "content": "Couldn't read any text from that file."}
        return {"type": "text", "content": text}
