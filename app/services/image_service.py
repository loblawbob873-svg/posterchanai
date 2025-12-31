import httpx
import asyncio
import base64
import random
from typing import Optional
from sqlalchemy.orm import Session
from app.models import Setting


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

    def _build_workflow(self, prompt: str, model: str) -> dict:
        """Build ComfyUI workflow for image generation"""
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
                    "height": 1024,
                    "width": 1024
                }
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "clip": ["4", 1],
                    "text": prompt
                }
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "clip": ["4", 1],
                    "text": "bad quality, blurry, distorted, ugly, deformed"
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
                    "filename_prefix": "posterchanai",
                    "images": ["8", 0]
                }
            }
        }

    async def generate_image(self, prompt: str) -> Optional[str]:
        """Generate image from prompt, returns base64 encoded image or None"""
        if not self.comfyui_url:
            return None

        # Select model based on prompt
        model = self.anime_model if self._is_anime_prompt(prompt) else self.default_model
        if not model:
            model = self.default_model or self.anime_model

        if not model:
            return None

        workflow = self._build_workflow(prompt, model)

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
                            print(f"ComfyUI error: {status.get('messages', [])}")
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
                print(f"Image generation error: {e}")
                return None

    async def regenerate_image(self, prompt: str) -> Optional[str]:
        """Regenerate image with same prompt (uses different seed)"""
        return await self.generate_image(prompt)


def get_image_service(db: Session) -> ImageService:
    return ImageService(db)
