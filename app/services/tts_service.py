import edge_tts
import base64
import io
import re
from typing import Optional
from sqlalchemy.orm import Session
from app.models import Setting


class TTSService:
    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        settings = {s.key: s.value for s in self.db.query(Setting).all()}
        self.default_voice = settings.get("tts_voice", "zh-CN-XiaoxiaoNeural")
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
            return None

        voice = voice or self.default_voice

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
                return None

            return base64.b64encode(audio_data).decode()
        except Exception as e:
            print(f"TTS error: {e}")
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
            print(f"Error listing voices: {e}")
            return []


def get_tts_service(db: Session) -> TTSService:
    return TTSService(db)
