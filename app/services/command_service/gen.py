"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
from ._common import MusicError, Optional, VideoError, datetime, generate_image_for_user, logger, music_factory, video_factory


class _GenMixin:
    async def _geni_command(self, prompt: str, stop_check: Optional[callable] = None) -> dict:
        if not prompt:
            return {
                "type": "text",
                "content": "Please provide a prompt. Example: `geni a beautiful sunset over mountains`",
            }

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

        # Generate image with load balancing support
        # Lock is handled inside image_factory for local generation only
        # Remote requests (load balanced or custom user endpoint) run in parallel
        try:
            logger.info(f"Generating image with prompt: {prompt[:100]}...")
            image_data = await generate_image_for_user(
                db=self.db,
                user=self.user,
                prompt=prompt,
            )
        except Exception as e:
            logger.error(f"Image generation exception: {e}", exc_info=True)
            return {"type": "text", "content": f"Image generation error: {str(e)}\n\nCheck logs for details."}

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

        if not image_data:
            error_msg = "## ❌ Image Generation Failed\n\n"
            error_msg += "**Native diffusers backend**\n\n"
            error_msg += "Possible issues:\n"
            error_msg += "- Model not loaded (check VRAM availability)\n"
            error_msg += "- Generation failed (check logs)\n"
            error_msg += "- GPU/XPU not available\n"
            error_msg += "\n**Prompt:** " + prompt
            logger.warning(f"Image generation returned None for prompt: {prompt[:100]}...")
            
            return {"type": "text", "content": error_msg}

        # Compress before it leaves the server. The backend hands back a full-size PNG (~1.4 MB for
        # 1024²), and that blob is pushed over the WebSocket, held in the conversation, and re-sent on
        # every reload — the same waste the bots had. media_service.compress_image is the compressor
        # the `compress` command uses, so chat images get exactly what the rest of the app applies.
        # `mime` travels with it because the result is now JPEG, and the client must not label it PNG.
        image_mime = "image/png"
        try:
            import base64 as _b64i
            from app.services import media_service as _ms
            _raw = _b64i.b64decode(image_data)
            _small = _ms.compress_image(_raw)
            # Keep the ORIGINAL when the re-encode doesn't actually help (flat/graphic output, where
            # PNG wins) — compressing to a bigger file would be worse than doing nothing.
            if _small and len(_small) < len(_raw):
                logger.info(f"[geni] compressed image {len(_raw)} -> {len(_small)} bytes")
                image_data = _b64i.b64encode(_small).decode()
                image_mime = "image/jpeg"
        except Exception as _c_err:
            # Never fail a generation over an optimisation — ship the original.
            logger.warning(f"[geni] image compression skipped ({type(_c_err).__name__}: {_c_err})")

        # Don't save automatically - just display the image with a save button
        return {
            "type": "generated_image",
            "content": f"Generated image for: {prompt}",
            "image": image_data,
            "mime": image_mime,
            "prompt": prompt,
        }

    async def _music_write_lyrics(self, request: str) -> tuple:
        """Turn a natural-language song request into (style_caption, lyrics) via the LLM, so
        `musicgeni` produces vocals. Falls back to (request, "") — instrumental — on any error."""
        messages = [
            {"role": "system", "content": (
                "You write songs for a music-generation model. From the user's request, produce a "
                "song. Respond EXACTLY in this format and nothing else:\n"
                "STYLE: <one line: genre, mood, tempo, instrumentation, and vocal type>\n"
                "LYRICS:\n"
                "<lyrics using [verse], [chorus], [bridge] section tags. Write a FULL-LENGTH song of "
                "roughly 3-5 minutes: at least two or three verses, a chorus repeated after each, and "
                "a bridge before the final chorus. Around 40-60 lines. Do NOT write a short song — "
                "too few lines and the model pads the rest of the track with instrumental filler.>")},
            {"role": "user", "content": (request or "")[:2000]},
        ]
        _orig_np = self.chat_service.num_predict
        self.chat_service.num_predict = max(_orig_np, 4096)
        try:
            out = (await self.chat_service.chat(messages) or "").strip()
        except Exception as e:
            logger.warning(f"[music] lyric generation failed, going instrumental: {e}")
            return request, ""
        finally:
            self.chat_service.num_predict = _orig_np

        # Parse "STYLE: ...\nLYRICS:\n..."; fall back to using the request as style + output as lyrics.
        style, lyrics = request, ""
        low = out.lower()
        if "lyrics:" in low:
            head, _, body = out.partition("\n")
            li = low.find("lyrics:")
            lyrics = out[li + len("lyrics:"):].strip()
            si = low.find("style:")
            if si != -1 and si < li:
                style = out[si + len("style:"):li].strip().splitlines()[0].strip() or request
        else:
            lyrics = out
        return style, lyrics

    async def _musicgeni_command(self, arg: str, stop_check: Optional[callable] = None) -> dict:
        """Generate a song via the ACE-Step server. `musicgeni <style prompt> [| lyrics]`.

        Returns the shared `generated_audio` shape so the web UI renders an <audio> player and
        Telegram sends it via send_audio. Wired for web UI + Telegram only (not the fedi bots)."""
        if not arg or not arg.strip():
            return {
                "type": "text",
                "content": "Please provide a prompt. Example: `musicgeni upbeat synthwave, driving bassline` "
                           "or `musicgeni dreamy pop ballad | first verse lyrics here`",
            }

        # `musicgeni <style> | <lyrics>` — explicit lyrics after a `|`. With no `|`, auto-write
        # lyrics from the request via the LLM so songs have vocals (unless it's clearly meant to be
        # instrumental, or the user typed a bare `|` for an explicit instrumental).
        has_pipe = "|" in arg
        prompt, _, lyrics = arg.partition("|")
        prompt = prompt.strip()
        lyrics = lyrics.strip()
        instrumental = any(k in prompt.lower() for k in ("instrumental", "no vocals", "no lyrics"))
        if not has_pipe and not instrumental:
            prompt, lyrics = await self._music_write_lyrics(prompt)

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

        try:
            logger.info(f"Generating music with style: {prompt[:100]}... (lyrics: {len(lyrics)} chars)")
            audio_bytes, ext = await music_factory.generate_music_for_user(
                db=self.db, prompt=prompt, lyrics=lyrics,
            )
        except MusicError as e:
            return {"type": "text", "content": f"🎵 {e}"}
        except Exception as e:
            logger.error(f"Music generation exception: {e}", exc_info=True)
            return {"type": "text", "content": f"Music generation error: {str(e)}\n\nCheck logs for details."}

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

        import base64 as _b64
        import asyncio as _asyncio
        from app.services import media_service
        from app.services import settings_store

        # Wrap the song in a branded video: a generic PosterChan background for the song's
        # duration, then the end-card "watermark" outro. music_watermark_enabled gates the outro.
        add_outro = str(settings_store.get("music_watermark_enabled", "true")).lower() != "false"
        try:
            # Background shows only PosterChan branding — NOT the prompt (title="").
            video_bytes = await _asyncio.to_thread(
                media_service.make_music_video, audio_bytes, ext, "", 1280, 720, add_outro
            )
        except Exception as e:
            logger.warning(f"music video wrap failed, falling back to audio: {e}")
            video_bytes = b""

        if video_bytes:
            return {
                "type": "generated_video",
                "content": f"Generated song for: {prompt}",
                "video": _b64.b64encode(video_bytes).decode(),
                "format": "mp4",
                "prompt": prompt,
                # The branded MP4 wraps a real audio track, so the client can offer "Convert to MP3".
                # videogeni's output is silent and deliberately does NOT set this.
                "has_audio": True,
            }
        # Fallback: deliver the raw audio if video wrapping wasn't possible (e.g. no ffmpeg).
        return {
            "type": "generated_audio",
            "content": f"Generated song for: {prompt}",
            "audio": _b64.b64encode(audio_bytes).decode(),
            "format": ext,
            "prompt": prompt,
        }

    async def _narrate_command(self, arg: str, stop_check: Optional[callable] = None) -> dict:
        """Reply as a spoken TTS message wrapped in a branded MP4. `narrate <message>`.

        Generates an AI reply (instructed to be clean/spoken — no emojis/hashtags/markdown),
        speaks it via TTSService, then wraps the audio in a branded PosterChan video with the
        end-card outro 'watermark' (same `make_music_video` path as musicgeni). Returns the shared
        `generated_video` shape (web UI renders <video>, Telegram sends via send_video); falls back
        to `generated_audio`, then plain text, if TTS/ffmpeg are unavailable."""
        if not arg or not arg.strip():
            return {
                "type": "text",
                "content": "Please provide a message. Example: `narrate tell me a fun fact`",
            }

        # Use the admin-configured persona (ollama_system_prompt) as the base, same as the normal
        # chat path, then append TTS-mode guidance so the reply reads cleanly aloud.
        system_prompt = self.chat_service.system_prompt.replace(
            "{{CURRENT_DATE}}", datetime.utcnow().strftime("%Y-%m-%d")
        )
        system_prompt += (
            "\n\nYour reply will be read aloud as speech. Answer in 1-4 short, natural sentences. "
            "Do NOT use emojis, hashtags, markdown, URLs, code blocks, or special formatting — "
            "plain spoken English only."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": arg.strip()},
        ]
        reply_text = await self.chat_service.chat(messages)
        if not reply_text or reply_text.startswith("Error:"):
            return {"type": "text", "content": reply_text or "Failed to generate a reply."}
        reply_text = reply_text.strip()

        if stop_check and stop_check():
            return {"type": "text", "content": "Cancelled."}

        # Speak the reply (edge-tts → base64 MP3).
        from app.services.tts_service import TTSService
        audio_b64 = await TTSService(self.db).generate_speech(reply_text)
        if not audio_b64:
            # TTS unavailable — at least return the text reply.
            return {"type": "text", "content": reply_text}

        import base64 as _b64
        import asyncio as _asyncio
        from app.services import media_service

        audio_bytes = _b64.b64decode(audio_b64)
        # Wrap the speech in a branded video with the end-card outro 'watermark' (title="" → only
        # PosterChan branding on the background), mirroring musicgeni's make_music_video path.
        try:
            video_bytes = await _asyncio.to_thread(
                media_service.make_music_video, audio_bytes, "mp3", "", 1280, 720, True
            )
        except Exception as e:
            logger.warning(f"narrate video wrap failed, falling back to audio: {e}")
            video_bytes = b""

        if video_bytes:
            return {
                "type": "generated_video",
                "content": reply_text,
                "video": _b64.b64encode(video_bytes).decode(),
                "format": "mp4",
                "prompt": arg.strip(),
                "has_audio": True,          # speech track — same MP3 extraction applies
            }
        # Fallback: deliver the raw speech audio if video wrapping wasn't possible (e.g. no ffmpeg).
        return {
            "type": "generated_audio",
            "content": reply_text,
            "audio": audio_b64,
            "format": "mp3",
            "prompt": arg.strip(),
        }

    async def _voice_command(self, arg: str, attachments: Optional[list] = None) -> dict:
        """Speak text in a CLONED voice. `voice <text>` with a short clip of the voice attached.

        The reference travels as an attachment rather than being looked up in a server-side library,
        which is what lets the same command work identically in the web chat and on Telegram — the
        voice library is client-side (the studio in AI Chat keeps it on the user's own Blossom drive),
        and Telegram has no library at all. Reply to a voice note with `voice <text>` and it speaks.

        This is the one local speech model in the stack, and it is deliberately NOT what `narrate`
        uses: narrate is edge-tts (cloud, free, instant), this holds the node's GPU for ~10x realtime.
        """
        import asyncio as _asyncio
        import base64 as _b64
        import os
        import shutil
        import tempfile

        from app.services import media_service, voice_factory, settings_store

        said = (arg or "").strip()
        if not said:
            return {"type": "text",
                    "content": "Attach a short clip of the voice, then send `voice <what to say>`."}
        if not attachments:
            return {"type": "text",
                    "content": "I need a voice to copy — attach a few seconds of clean audio "
                               "(or a video with speech in it), then send `voice <what to say>`."}
        if str(settings_store.get("voice_enabled", "false")).lower() not in ("1", "true", "yes", "on"):
            return {"type": "text", "content": "Voice cloning is switched off on this server."}

        # First attachment with audio in it. A VIDEO is fine — ffmpeg pulls the track out below, which
        # is what makes "reply to a clip with `voice ...`" work without the user converting anything.
        ref = None
        for fn, data, ct in attachments:
            if not data:
                continue
            if (ct or "").startswith(("audio/", "video/")) or media_service.is_video(fn, ct or ""):
                ref = (fn, data)
                break
        if ref is None:
            return {"type": "text", "content": "That attachment has no audio in it — I need a clip of the voice."}

        tmp = tempfile.mkdtemp(prefix="voice_cmd_")
        try:
            src = os.path.join(tmp, ref[0] or "ref")
            with open(src, "wb") as f:
                f.write(ref[1])
            # Normalise to what the model wants: mono 24kHz WAV, trimmed to the reference cap. A long
            # reference buys nothing (the model uses a few seconds) and costs upload + memory on every
            # forwarded request, so the cap is enforced HERE, once, before any of that.
            max_ref = int(float(settings_store.get("voice_max_ref_seconds", "30") or 30))
            wav_path = os.path.join(tmp, "ref.wav")
            ff = media_service.resolve_ffmpeg()
            proc = await _asyncio.create_subprocess_exec(
                ff, "-y", "-i", src, "-t", str(max_ref), "-ar", "24000", "-ac", "1", wav_path,
                stdout=_asyncio.subprocess.DEVNULL, stderr=_asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode != 0 or not os.path.exists(wav_path):
                return {"type": "text",
                        "content": "Couldn't read any audio out of that clip: "
                                   + err[-200:].decode("utf-8", "replace")}
            with open(wav_path, "rb") as f:
                ref_bytes = f.read()

            try:
                wav, where = await voice_factory.generate_voice(
                    self.db, said, ref_bytes, reference_path=wav_path)
            except Exception as e:
                return {"type": "text", "content": f"Voice generation failed: {e}"}
            logger.info("[voice] command spoke %d chars on %s", len(said), where)

            # Deliver as a branded MP4, exactly like musicgeni/narrate — one delivery shape for every
            # generated-audio feature, so the players, the MP3 extraction and the outro all already
            # work. make_music_video takes the EXTENSION and hands the file to ffmpeg, so the WAV goes
            # in as-is; converting it to mp3 first would be a second encode for no one's benefit.
            wrap = str(settings_store.get("voice_watermark_enabled", "true")).lower() in ("1", "true", "yes", "on")
            video_bytes = b""
            if wrap:
                try:
                    video_bytes = await _asyncio.to_thread(
                        media_service.make_music_video, wav, "wav", "", 1280, 720, True)
                except Exception as e:
                    logger.warning("voice video wrap failed, sending audio: %s", e)
            if video_bytes:
                return {"type": "generated_video", "content": said,
                        "video": _b64.b64encode(video_bytes).decode(), "format": "mp4",
                        "prompt": said, "has_audio": True}
            return {"type": "generated_audio", "content": said,
                    "audio": _b64.b64encode(wav).decode(), "format": "wav", "prompt": said}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def _videogeni_command(self, arg: str, stop_check: Optional[callable] = None) -> dict:
        """Generate a short video via the native diffusers Wan pipeline. `videogeni <prompt>`.

        Returns the shared `generated_video` shape so the web UI renders a <video> player and
        Telegram sends it via send_video. Wired for web UI + Telegram only (not the fedi bots)."""
        if not arg or not arg.strip():
            return {
                "type": "text",
                "content": "Please provide a prompt. Example: `videogeni a red fox running through snow, cinematic`",
            }
        # `videogeni <prompt> | <negative>` — optional negative prompt after a `|`.
        prompt, _, negative = arg.partition("|")
        prompt = prompt.strip()
        negative = negative.strip()

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

        try:
            logger.info(f"Generating video: {prompt[:100]}...")
            video_bytes = await video_factory.generate_video_for_user(
                db=self.db, prompt=prompt, negative_prompt=negative,
            )
        except VideoError as e:
            return {"type": "text", "content": f"🎬 {e}"}
        except Exception as e:
            logger.error(f"Video generation exception: {e}", exc_info=True)
            return {"type": "text", "content": f"Video generation error: {str(e)}\n\nCheck logs for details."}

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

        import base64 as _b64
        return {
            "type": "generated_video",
            "content": f"Generated video for: {prompt}",
            "video": _b64.b64encode(video_bytes).decode(),
            "format": "mp4",
            "prompt": prompt,
        }
