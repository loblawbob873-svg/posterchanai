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

        voice = voice or self.default_voice
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
