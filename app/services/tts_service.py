import edge_tts
import base64
import io
import re
import logging
from typing import Optional
from sqlalchemy.orm import Session
from app.services import settings_store

logger = logging.getLogger(__name__)


class TTSService:
    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        settings = settings_store.all_settings()
        self.default_voice = settings.get("tts_voice", "en-GB-SoniaNeural")
        self.rate = settings.get("tts_rate", "+5%")
        self.pitch = settings.get("tts_pitch", "+10Hz")

    def _clean_text(self, text: str) -> str:
        """Clean text for TTS - remove URLs, mentions, hashtags, emojis, markdown"""
        # Remove URLs
        cleaned = re.sub(r'https?://\S+', '', text)
        # Remove mentions
        cleaned = re.sub(r'@[\w@.]+', '', cleaned)
        # Remove hashtags
        cleaned = re.sub(r'#\w+', '', cleaned)
        # Remove custom emojis :emoji_name:
        cleaned = re.sub(r':[\w_]+:', '', cleaned)
        # Remove markdown bold/italic
        cleaned = re.sub(r'\*+([^*]+)\*+', r'\1', cleaned)
        cleaned = re.sub(r'_+([^_]+)_+', r'\1', cleaned)
        # Strip markdown heading markers (## Heading → Heading) so they aren't read as "hash hash"
        cleaned = re.sub(r'(?m)^\s{0,3}#{1,6}\s+', '', cleaned)
        # Remove markdown links [text](url)
        cleaned = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', cleaned)
        # Remove code blocks
        cleaned = re.sub(r'```[\s\S]*?```', '', cleaned)
        cleaned = re.sub(r'`[^`]+`', '', cleaned)
        # Remove unicode emojis (basic range)
        cleaned = re.sub(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001F900-\U0001F9FF]', '', cleaned)
        # Clean up whitespace
        cleaned = ' '.join(cleaned.split())
        # Limit length
        return cleaned.strip()[:1000]

    # Dominant non-Latin script → a matching edge-tts voice. The default voice is English, which reads
    # Latin text fine but is SILENT on other scripts — so narrating a Thai (or CJK/Arabic/…) translation
    # spoke only the English "Translation (Thai)" heading and none of the body (the reported bug). Pick a
    # voice for the text's script so the actual translated content is vocalized.
    _SCRIPT_VOICE = {
        "th": "th-TH-PremwadeeNeural", "ru": "ru-RU-SvetlanaNeural", "ar": "ar-SA-ZariyahNeural",
        "he": "he-IL-HilaNeural", "el": "el-GR-AthinaNeural", "hi": "hi-IN-SwaraNeural",
        "ja": "ja-JP-NanamiNeural", "ko": "ko-KR-SunHiNeural", "zh": "zh-CN-XiaoxiaoNeural",
    }

    def _voice_for_text(self, text: str) -> Optional[str]:
        """A voice matching the text's DOMINANT non-Latin script, or None to keep the configured
        (Latin/English) default. Only switches when the script is a real share (≥4 chars) so a stray
        foreign character can't flip the voice."""
        counts: dict = {}
        for ch in text:
            c = ord(ch)
            if   0x0E00 <= c <= 0x0E7F: k = "th"
            elif 0x0400 <= c <= 0x04FF: k = "ru"
            elif 0x0600 <= c <= 0x06FF: k = "ar"
            elif 0x0590 <= c <= 0x05FF: k = "he"
            elif 0x0370 <= c <= 0x03FF: k = "el"
            elif 0x0900 <= c <= 0x097F: k = "hi"
            elif 0x3040 <= c <= 0x30FF: k = "ja"
            elif 0xAC00 <= c <= 0xD7AF: k = "ko"
            elif 0x4E00 <= c <= 0x9FFF: k = "zh"
            else: continue
            counts[k] = counts.get(k, 0) + 1
        if not counts:
            return None
        top = max(counts, key=counts.get)
        return self._SCRIPT_VOICE.get(top) if counts[top] >= 4 else None

    async def generate_speech(
        self,
        text: str,
        voice: Optional[str] = None
    ) -> Optional[str]:
        """Generate speech and return as base64 MP3"""
        cleaned_text = self._clean_text(text)
        if not cleaned_text:
            logger.debug(f"TTS: No text after cleaning (original length: {len(text) if text else 0})")
            return None

        # An explicit voice wins; else auto-match the text's script (so foreign/translated text is
        # actually spoken), else the configured default.
        voice = voice or self._voice_for_text(cleaned_text) or self.default_voice
        logger.debug(f"TTS: Generating speech for {len(cleaned_text)} chars with voice {voice}")

        try:
            communicate = edge_tts.Communicate(
                cleaned_text,
                voice,
                rate=self.rate,
                pitch=self.pitch
            )

            audio_buffer = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_buffer.write(chunk["data"])

            audio_buffer.seek(0)
            audio_data = audio_buffer.read()

            if not audio_data:
                logger.warning("TTS: No audio data generated")
                return None

            logger.debug(f"TTS: Generated {len(audio_data)} bytes of audio")
            return base64.b64encode(audio_data).decode()
        except Exception as e:
            logger.error(f"TTS error: {type(e).__name__}: {e}")
            return None

    @staticmethod
    async def list_voices() -> list[dict]:
        """List available TTS voices"""
        try:
            voices = await edge_tts.list_voices()
            return [
                {
                    "name": v["Name"],
                    "locale": v["Locale"],
                    "gender": v["Gender"],
                    "friendly_name": v.get("FriendlyName", v["Name"])
                }
                for v in voices
            ]
        except Exception as e:
            logger.error(f"Error listing voices: {e}")
            return []


def get_tts_service(db: Session) -> TTSService:
    return TTSService(db)
