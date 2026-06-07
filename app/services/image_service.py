import httpx
import asyncio
import base64
import random
import re
import logging
from typing import Optional
from sqlalchemy.orm import Session
from app.models import Setting

logger = logging.getLogger(__name__)

_DEFAULT_NEGATIVE = (
    "bad quality, blurry, distorted, ugly, deformed, "
    "cropped, cut off, missing feet, missing legs, headshot, bust, close-up, "
    "picture frame, border, frame, vignette, letterbox, pillarbox, "
    "painted background, flat background, illustrated background, studio backdrop, gradient background"
)


class ImageService:
    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        settings = {s.key: s.value for s in self.db.query(Setting).all()}
        self.comfyui_url = settings.get("comfyui_url", "")
        self.default_model = settings.get("comfyui_default_model", "")
        self.anime_model = settings.get("comfyui_anime_model", "")
        self.timeout = int(settings.get("comfyui_timeout", "300000")) / 1000  # Convert to seconds

    def _is_anime_prompt(self, prompt: str) -> bool:
        """Check if prompt is for anime-style image"""
        anime_keywords = [
            "anime", "manga", "waifu", "chibi", "kawaii",
            "moe", "otaku", "hentai", "ecchi", "shoujo",
            "seinen", "shonen", "isekai", "2d", "cel-shaded"
        ]
        prompt_lower = prompt.lower()
        return any(keyword in prompt_lower for keyword in anime_keywords)

    def _process_wildcards(self, prompt: str) -> str:
        """Resolve {option1|option2|...} wildcard syntax by randomly picking one option per group."""
        def pick(match):
            options = match.group(1).split('|')
            return random.choice(options).strip()
        return re.sub(r'\{([^}]+)\}', pick, prompt)

    def _build_workflow(self, prompt: str, model: str, negative: str = "", width: int = 832, height: int = 1216) -> dict:
        """Build ComfyUI workflow for image generation"""
        clean_prompt = self._sanitize_prompt(prompt)
        # `negative` already carries the default + caller negative (merged in generate_image);
        # only fall back to the default here if a caller invoked this directly with none.
        neg_text = self._sanitize_prompt(negative) if negative else _DEFAULT_NEGATIVE
        return {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "cfg": 7,
                    "denoise": 1,
                    "latent_image": ["5", 0],
                    "model": ["4", 0],
                    "negative": ["7", 0],
                    "positive": ["6", 0],
                    "sampler_name": "euler_ancestral",
                    "scheduler": "normal",
                    "seed": random.randint(0, 2**32 - 1),
                    "steps": 25
                }
            },
            "4": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {
                    "ckpt_name": model
                }
            },
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {
                    "batch_size": 1,
                    "height": height,
                    "width": width
                }
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "clip": ["4", 1],
                    "text": clean_prompt
                }
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "clip": ["4", 1],
                    "text": neg_text
                }
            },
            "8": {
                "class_type": "VAEDecode",
                "inputs": {
                    "samples": ["3", 0],
                    "vae": ["4", 2]
                }
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": "temp/posterchanai",
                    "images": ["8", 0]
                }
            }
        }

    async def generate_image(self, prompt: str, negative_prompt: str = "", **kwargs) -> Optional[str]:
        """Generate image from prompt, returns base64 encoded image or None"""
        if not self.comfyui_url:
            return None

        prompt = self._process_wildcards(prompt)
        width = kwargs.get("width") or 832
        height = kwargs.get("height") or 1216

        # NOTE: frame-suppression negative + random scene are applied centrally in
        # generate_image_with_load_balancing on the image server (so every caller — chat,
        # Telegram, bots, local or remote — gets them once). Don't duplicate here.
        # Try posterchanai REST API first (simpler, faster)
        result = await self._try_posterchanai_api(prompt, negative_prompt, width=width, height=height)
        if result:
            return result

        # Fall back to ComfyUI workflow API
        return await self._try_comfyui_workflow(prompt, negative_prompt=negative_prompt, width=width, height=height)

    async def _try_posterchanai_api(self, prompt: str, negative_prompt: str = "", width: int = 832, height: int = 1216) -> Optional[str]:
        """
        Try posterchanai's simple REST API.
        Returns image on success, raises Exception on server error (don't fallback),
        returns None only on connection error (try ComfyUI fallback).
        """
        try:
            api_url = self.comfyui_url.rstrip('/') + '/api/generate-image'
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    api_url,
                    json={"prompt": prompt, "negative_prompt": negative_prompt, "width": width, "height": height}
                )
                if response.status_code == 200:
                    data = response.json()
                    if data.get("image"):
                        return data["image"]
                    if data.get("error"):
                        # Server responded with error - don't fall back to ComfyUI
                        raise Exception(f"Posterchanai error: {data['error']}")
                return None
        except httpx.ConnectError:
            # Connection failed - not a posterchanai instance, try ComfyUI
            return None
        except httpx.TimeoutException:
            # Timeout - try ComfyUI fallback
            return None

    async def _try_comfyui_workflow(self, prompt: str, negative_prompt: str = "", width: int = 832, height: int = 1216) -> Optional[str]:
        """Try ComfyUI workflow API"""
        # Select model based on prompt
        model = self.anime_model if self._is_anime_prompt(prompt) else self.default_model
        if not model:
            model = self.default_model or self.anime_model

        if not model:
            return None

        workflow = self._build_workflow(prompt, model, negative=negative_prompt, width=width, height=height)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                # Submit workflow
                response = await client.post(
                    f"{self.comfyui_url}/prompt",
                    json={"prompt": workflow}
                )
                response.raise_for_status()
                prompt_id = response.json()["prompt_id"]

                # Poll for completion
                start_time = asyncio.get_event_loop().time()
                while asyncio.get_event_loop().time() - start_time < self.timeout:
                    await asyncio.sleep(2)

                    history_response = await client.get(
                        f"{self.comfyui_url}/history/{prompt_id}"
                    )
                    history_data = history_response.json()

                    if prompt_id in history_data:
                        prompt_data = history_data[prompt_id]
                        status = prompt_data.get("status", {})

                        if status.get("status_str") == "error":
                            logger.error(f"ComfyUI error: {status.get('messages', [])}")
                            return None

                        outputs = prompt_data.get("outputs", {})
                        if "9" in outputs and "images" in outputs["9"]:
                            images = outputs["9"]["images"]
                            if images:
                                img_info = images[0]
                                img_response = await client.get(
                                    f"{self.comfyui_url}/view",
                                    params={
                                        "filename": img_info["filename"],
                                        "subfolder": img_info.get("subfolder", ""),
                                        "type": img_info.get("type", "output")
                                    }
                                )
                                img_response.raise_for_status()
                                return base64.b64encode(img_response.content).decode()

                return None
            except Exception as e:
                logger.error(f"Image generation error: {e}")
                return None

    def _sanitize_prompt(self, text: str) -> str:
        """Sanitize prompt for JSON embedding"""
        text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', text)
        text = text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')
        return text


def get_image_service(db: Session) -> ImageService:
    return ImageService(db)
