import httpx
import asyncio
import base64
import random
import uuid
import re
from typing import Optional, Tuple
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

    def _sanitize_prompt(self, text: str) -> str:
        """Sanitize prompt for JSON embedding"""
        text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', text)
        text = text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')
        return text

    async def _upload_image(self, image_bytes: bytes, filename: str = "input.png") -> Optional[str]:
        """Upload an image to ComfyUI's input directory for img2img"""
        unique_filename = f"{uuid.uuid4().hex}_{filename}"

        boundary = uuid.uuid4().hex
        body_parts = [
            f'--{boundary}'.encode(),
            f'Content-Disposition: form-data; name="image"; filename="{unique_filename}"'.encode(),
            b'Content-Type: image/png',
            b'',
            image_bytes,
            f'--{boundary}--'.encode()
        ]
        body_bytes = b'\r\n'.join(body_parts)

        async with httpx.AsyncClient(timeout=60) as client:
            try:
                response = await client.post(
                    f"{self.comfyui_url}/upload/image",
                    content=body_bytes,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
                )
                response.raise_for_status()
                result = response.json()
                uploaded_name = result.get("name")
                if uploaded_name:
                    print(f"Image uploaded to ComfyUI: {uploaded_name}")
                    return uploaded_name
                return None
            except Exception as e:
                print(f"Error uploading image to ComfyUI: {e}")
                return None

    def _build_img2img_workflow(self, prompt: str, model: str, uploaded_filename: str, denoise: float = 1.0) -> dict:
        """Build ComfyUI workflow for img2img generation"""
        clean_prompt = self._sanitize_prompt(prompt)
        base_negative = "distorted, warped, disfigured, deformed, mutated, extra fingers, ugly, missing fingers, censored, blury face, ugly face, low quality, blury, low res, low resolution, Cropped, Out of frame, Out of focus, watermark, banner, extra digits, Jpeg artifacts, Grainy, Bad anatomy, Bad proportions, Deformed, Disfigured, Extra arms, Extra limbs, Extra hands, Fused fingers, Gross proportions, Long neck, Malformed limbs, Mutated, Mutated hands, Mutated limbs, Missing arms, bad quality hands, Poorly drawn hands, Poorly drawn face"

        return {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": random.randint(0, 2**32 - 1),
                    "steps": 25,
                    "cfg": 7,
                    "sampler_name": "dpmpp_2m",
                    "scheduler": "karras",
                    "denoise": denoise,
                    "model": ["4", 0],
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "latent_image": ["12", 0]
                }
            },
            "4": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {
                    "ckpt_name": model
                }
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": clean_prompt,
                    "clip": ["4", 1]
                }
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": base_negative,
                    "clip": ["4", 1]
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
                    "filename_prefix": "posterchanai_img2img",
                    "images": ["8", 0]
                }
            },
            "10": {
                "class_type": "LoadImage",
                "inputs": {
                    "image": uploaded_filename,
                    "upload": "image"
                }
            },
            "11": {
                "class_type": "ImageScale",
                "inputs": {
                    "upscale_method": "lanczos",
                    "width": 1024,
                    "height": 1024,
                    "crop": "center",
                    "image": ["10", 0]
                }
            },
            "12": {
                "class_type": "VAEEncode",
                "inputs": {
                    "pixels": ["11", 0],
                    "vae": ["4", 2]
                }
            }
        }

    async def generate_img2img(self, prompt: str, image_bytes: bytes, denoise: float = 1.0) -> Optional[str]:
        """
        Generate image from prompt using source image as base.
        denoise=1.0 means full denoising (prompt controls everything)
        denoise=0.5 means keep 50% of original image structure
        Returns base64 encoded image or None
        """
        if not self.comfyui_url:
            return None

        # Upload source image
        uploaded_filename = await self._upload_image(image_bytes)
        if not uploaded_filename:
            print("Failed to upload source image for img2img")
            return None

        # Select model based on prompt
        model = self.anime_model if self._is_anime_prompt(prompt) else self.default_model
        if not model:
            model = self.default_model or self.anime_model

        if not model:
            return None

        workflow = self._build_img2img_workflow(prompt, model, uploaded_filename, denoise)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                # Submit workflow
                response = await client.post(
                    f"{self.comfyui_url}/prompt",
                    json={"prompt": workflow}
                )
                response.raise_for_status()
                prompt_id = response.json()["prompt_id"]

                print(f"img2img submitted, prompt_id: {prompt_id}, denoise: {denoise}")

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
                            print(f"ComfyUI img2img error: {status.get('messages', [])}")
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
                print(f"img2img generation error: {e}")
                return None


def get_image_service(db: Session) -> ImageService:
    return ImageService(db)
