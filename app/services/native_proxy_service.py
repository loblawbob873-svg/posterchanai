"""
Native Image Proxy Service
Forwards image generation requests to a remote native diffusers backend.
Used when the local server should proxy requests to another posterchanai instance.
"""
import json
import logging
from typing import Optional
from urllib import request
from urllib.error import URLError, HTTPError

from sqlalchemy.orm import Session
from app.models import Setting

logger = logging.getLogger(__name__)


class NativeProxyService:
    """Proxy service that forwards image generation to a remote native backend"""

    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        settings = {s.key: s.value for s in self.db.query(Setting).all()}
        # Use comfyui_url as the remote endpoint
        self.remote_url = settings.get("comfyui_url", "http://localhost:3051").rstrip('/')
        self.timeout = int(settings.get("comfyui_timeout", "300000")) / 1000  # Convert to seconds

    def _call_remote(self, endpoint: str, payload: dict) -> Optional[dict]:
        """Make a POST request to the remote API"""
        url = f"{self.remote_url}{endpoint}"
        try:
            data = json.dumps(payload).encode('utf-8')
            req = request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
            resp = request.urlopen(req, timeout=self.timeout)
            return json.loads(resp.read())
        except HTTPError as e:
            logger.error(f"[NATIVE-PROXY] HTTP error {e.code} from {url}")
            return None
        except URLError as e:
            logger.error(f"[NATIVE-PROXY] URL error: {e.reason}")
            return None
        except Exception as e:
            logger.error(f"[NATIVE-PROXY] Error: {e}")
            return None

    async def generate_image(self, prompt: str, **kwargs) -> Optional[str]:
        """Generate image from text prompt, returns base64"""
        payload = {
            "prompt": prompt,
            "negative_prompt": kwargs.get("negative_prompt", ""),
            "width": kwargs.get("width"),
            "height": kwargs.get("height"),
            "steps": kwargs.get("steps"),
            "cfg": kwargs.get("cfg")
        }
        # Remove None values
        payload = {k: v for k, v in payload.items() if v is not None}

        logger.info(f"[NATIVE-PROXY] Forwarding generate_image to {self.remote_url}")
        result = self._call_remote("/api/generate-image", payload)
        if result and result.get("image"):
            return result["image"]
        return None

    async def regenerate_image(self, prompt: str) -> Optional[str]:
        """Regenerate with new seed - just call generate_image again"""
        return await self.generate_image(prompt)


def get_native_proxy_service(db: Session) -> NativeProxyService:
    return NativeProxyService(db)
