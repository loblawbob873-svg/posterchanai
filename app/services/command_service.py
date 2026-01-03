import base64
import logging
from typing import Optional, Tuple, Callable, TYPE_CHECKING
from sqlalchemy.orm import Session
from app.services.search_service import SearchService
from app.services.image_factory import get_image_backend, get_image_backend_for_user, prepare_vram_for_image
from app.services.chat_service import ChatService
from app.services.locks import image_generation_lock

if TYPE_CHECKING:
    from app.models import User

logger = logging.getLogger(__name__)


class CommandService:
    COMMANDS = {
        "search": "Search the web and get AI-summarized results",
        "images": "Search for images",
        "geni": "Generate an AI image from your prompt",
        "img2img": "Transform an uploaded image with your prompt",
        "regen": "Regenerate the last image with a new seed",
    }

    def __init__(self, db: Session, user: Optional["User"] = None):
        self.db = db
        self.user = user
        self.search_service = SearchService(db)
        # Use user's custom ComfyUI if enabled, otherwise use default
        self.image_service = get_image_backend_for_user(db, user)
        self.chat_service = ChatService(db, user=user)

    def parse_command(self, message: str) -> Tuple[Optional[str], str]:
        """Parse message for commands, return (command, argument)"""
        lower = message.lower().strip()

        for cmd in self.COMMANDS:
            if lower.startswith(f"{cmd} "):
                return cmd, message[len(cmd)+1:].strip()
            if lower == cmd:
                return cmd, ""

        return None, message

    async def execute_command(self, command: str, arg: str, last_prompt: Optional[str] = None,
                              image_data: Optional[str] = None,
                              stop_check: Optional[Callable[[], bool]] = None) -> dict:
        """Execute a command and return the result"""
        if command == "search":
            return await self._search_command(arg)
        elif command == "images":
            return await self._images_command(arg)
        elif command == "geni":
            return await self._geni_command(arg, stop_check)
        elif command == "img2img":
            return await self._img2img_command(arg, image_data, stop_check)
        elif command == "regen":
            return await self._regen_command(last_prompt, stop_check)
        else:
            return {"type": "text", "content": f"Unknown command: {command}"}

    async def _search_command(self, query: str) -> dict:
        if not query:
            return {"type": "text", "content": "Please provide a search query. Example: `search latest AI news`"}

        results = await self.search_service.web_search(query, limit=5)
        if not results:
            return {"type": "text", "content": f"No results found for: {query}"}

        # Format results for AI summarization
        context = f"Search results for '{query}':\n\n"
        for i, r in enumerate(results, 1):
            context += f"{i}. **{r['title']}**\n{r['url']}\n{r['content']}\n\n"

        # Get AI summary
        messages = [
            {"role": "system", "content": "You are a helpful assistant. Summarize the search results concisely and highlight key information."},
            {"role": "user", "content": context}
        ]
        summary = await self.chat_service.chat(messages)

        return {
            "type": "search",
            "content": summary,
            "results": results
        }

    async def _images_command(self, query: str) -> dict:
        if not query:
            return {"type": "text", "content": "Please provide a search query. Example: `images cute cats`"}

        results = await self.search_service.image_search(query, limit=10)
        if not results:
            return {"type": "text", "content": f"No images found for: {query}"}

        return {
            "type": "images",
            "content": f"Found {len(results)} images for: {query}",
            "images": results
        }

    async def _geni_command(self, prompt: str, stop_check: Optional[callable] = None) -> dict:
        if not prompt:
            return {"type": "text", "content": "Please provide a prompt. Example: `geni a beautiful sunset over mountains`"}

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

        # Use lock to prevent concurrent image generation
        async with image_generation_lock:
            # Prepare VRAM for image generation (swap models if needed)
            prepare_vram_for_image(self.db)

            if stop_check and stop_check():
                return {"type": "text", "content": "Generation cancelled."}

            image_data = await self.image_service.generate_image(prompt)
            if not image_data:
                return {"type": "text", "content": "Failed to generate image. Please try again or check if ComfyUI is configured."}

            return {
                "type": "generated_image",
                "content": f"Generated image for: {prompt}",
                "image": image_data,
                "prompt": prompt
            }

    async def _img2img_command(self, prompt: str, image_data: Optional[str], stop_check: Optional[callable] = None) -> dict:
        """Edit image using img2img + face swap to preserve identity"""
        if not prompt or not image_data:
            return {"type": "text", "content": "Need both prompt and image."}

        if stop_check and stop_check():
            return {"type": "text", "content": "Cancelled."}

        async with image_generation_lock:
            prepare_vram_for_image(self.db)

            try:
                image_bytes = base64.b64decode(image_data)
            except Exception as e:
                logger.info(f"[IMG2IMG] Failed to decode image: {e}")
                return {"type": "text", "content": "Invalid image."}

            # Get tags and detect style
            from app.services.wd14_service import tag_image
            tags = tag_image(image_bytes, threshold=0.35) or ""
            logger.info(f"[IMG2IMG] WD14 tags: {tags[:200]}...")
            is_anime = 'anime' in prompt.lower() or 'anime' in tags.lower()
            style = "anime" if is_anime else "realistic"

            # Build prompt with identity preservation tags
            extra_tags = []
            neg_extra = []
            tags_lower = tags.lower()

            # Hair color detection
            has_light_hair = any(h in tags_lower for h in ['blonde', 'yellow_hair', 'orange_hair', 'red_hair', 'redhead', 'pink_hair', 'white_hair', 'silver_hair'])
            has_dark_hair = any(h in tags_lower for h in ['black_hair', 'brown_hair'])
            has_dark_skin_tag = 'dark_skin' in tags_lower or 'dark-skinned' in tags_lower

            # Skin tone - be smart about it!
            # Light-colored hair = pale skin, ignore dark_skin tag (probably from background)
            if has_light_hair:
                extra_tags.append("pale skin, white skin, fair skin, caucasian")
                neg_extra.append("dark skin, brown skin, black skin")
                logger.info(f"[IMG2IMG] Light hair detected - forcing pale skin")
            elif has_dark_skin_tag and has_dark_hair:
                # Only use dark skin if hair is also dark (consistent)
                extra_tags.append("dark brown skin")
            else:
                # Default to natural/pale
                extra_tags.append("natural skin tone, fair skin")
                neg_extra.append("dark skin, brown skin")

            if any(t in tags_lower for t in ["fat", "chubby", "plump", "overweight", "plus-size", "bbw"]):
                extra_tags.append("fat, obese, bbw, plus-size body")
            if any(t in tags_lower for t in ["large_breasts", "huge_breasts"]):
                extra_tags.append("large breasts")
            extra_str = ", ".join(extra_tags)
            logger.info(f"[IMG2IMG] Identity tags: {extra_str}")

            # Check if prompt contains NSFW keywords - add trigger phrase to use NSFW model
            nsfw_keywords = ["nude", "naked", "topless", "bare breasts", "nipples", "nsfw", "undress"]
            is_nsfw = any(kw in prompt.lower() for kw in nsfw_keywords)
            # Add strong NSFW keywords to ensure clothes removal - emphasize bare/exposed skin
            # Use NSFW keywords that don't trigger anime model selection
            nsfw_trigger = "nude, nipples, topless, exposed breasts, bare skin, naked body, no clothing at all, " if is_nsfw else ""

            # Add composition guidance - preserve pose, ensure face visible
            composition = "same pose, same composition, face visible, full body"
            style_suffix = "anime illustration" if is_anime else "realistic photography"
            final_prompt = f"{nsfw_trigger}{prompt}, {composition}, {extra_str}, {style_suffix}".strip(', ')
            # Add more negatives to prevent latex/shiny material and unwanted compositions
            neg_identity = ", ".join(neg_extra) if neg_extra else ""
            neg_prompt = f"frame, framed, picture frame, window frame, door frame, bars, cage, border, furniture, close-up, cropped, headless, no face, clothing, clothes, shirt, top, bra, pants, shorts, fabric, dressed, wearing, covered, mesh, sheer, latex, rubber, spandex, shiny, glossy, wet look, bodysuit, catsuit, thin, slim, skinny, {'realistic' if is_anime else 'anime'}, deformed, {neg_identity}".strip(', ')
            logger.debug(f"[IMG2IMG] Final prompt: {final_prompt}")
            logger.debug(f"[IMG2IMG] Negative prompt: {neg_prompt[:100]}...")

            # Import face detection for retry logic
            from app.services.face_swap_service import swap_face_bytes, detect_face
            from PIL import Image
            import io

            # Denoise: anime 0.70 (needs more change), realistic 0.50 (preserve composition)
            denoise_value = 0.70 if is_anime else 0.50

            # Retry up to 3 times if generated image has no face
            max_retries = 3
            for attempt in range(max_retries):
                logger.info(f"[IMG2IMG] Generation attempt {attempt + 1}/{max_retries} (denoise={denoise_value})")

                result_b64 = await self.image_service.generate_img2img(
                    prompt=final_prompt,
                    image_bytes=image_bytes,
                    denoise=denoise_value,
                    negative_prompt=neg_prompt
                )

                if not result_b64:
                    continue

                # Check if generated image has a face
                generated_bytes = base64.b64decode(result_b64)
                gen_image = Image.open(io.BytesIO(generated_bytes)).convert('RGB')
                gen_face = detect_face(gen_image)

                if gen_face is not None:
                    logger.info(f"[IMG2IMG] Face detected in generated image at bbox: {gen_face.bbox}")
                    break
                else:
                    logger.info(f"[IMG2IMG] No face in generated image, retrying...")
            else:
                print("[IMG2IMG] All retries failed to generate face, using last result")

            if not result_b64:
                return {"type": "text", "content": "Edit failed."}

            # Face swap: paste original face onto generated image
            # Skip for anime - InsightFace doesn't work with anime faces
            if not is_anime:
                try:
                    print("[IMG2IMG] Attempting face swap...")
                    generated_bytes = base64.b64decode(result_b64)
                    swapped_bytes = swap_face_bytes(image_bytes, generated_bytes)
                    if swapped_bytes:
                        result_b64 = base64.b64encode(swapped_bytes).decode()
                        print("[IMG2IMG] Face swap successful")
                    else:
                        print("[IMG2IMG] Face swap returned None (no face detected)")
                except Exception as e:
                    logger.info(f"[IMG2IMG] Face swap error: {e}")
            else:
                print("[IMG2IMG] Skipped face swap for anime")

            return {"type": "generated_image", "content": f"Edited: {prompt}", "image": result_b64, "prompt": prompt}

    async def _regen_command(self, last_prompt: Optional[str], stop_check: Optional[callable] = None) -> dict:
        if not last_prompt:
            return {"type": "text", "content": "No previous image prompt found. Use `geni <prompt>` first."}

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

        # Use lock to prevent concurrent image generation
        async with image_generation_lock:
            # Prepare VRAM for image generation (swap models if needed)
            prepare_vram_for_image(self.db)

            if stop_check and stop_check():
                return {"type": "text", "content": "Generation cancelled."}

            image_data = await self.image_service.regenerate_image(last_prompt)
            if not image_data:
                return {"type": "text", "content": "Failed to regenerate image. Please try again."}

            return {
                "type": "generated_image",
                "content": f"Regenerated image for: {last_prompt}",
                "image": image_data,
                "prompt": last_prompt
            }


def get_command_service(db: Session) -> CommandService:
    return CommandService(db)
