import base64
import asyncio
from typing import Optional, Tuple, TYPE_CHECKING
from sqlalchemy.orm import Session
from app.services.search_service import SearchService
from app.services.image_factory import get_image_backend, get_image_backend_for_user, prepare_vram_for_image
from app.services.chat_service import ChatService

if TYPE_CHECKING:
    from app.models import User

# Global lock to prevent concurrent image generation (prevents WD14/LLM response mixing)
_image_generation_lock = asyncio.Lock()


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
                              image_data: Optional[str] = None, file_content: Optional[str] = None,
                              stop_check: Optional[callable] = None) -> dict:
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
        async with _image_generation_lock:
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

        async with _image_generation_lock:
            prepare_vram_for_image(self.db)

            try:
                image_bytes = base64.b64decode(image_data)
                # Debug: Log input image info and save for comparison
                from PIL import Image
                import io
                debug_img = Image.open(io.BytesIO(image_bytes))
                print(f"[IMG2IMG-DEBUG] Input image: {len(image_bytes)} bytes, {debug_img.size[0]}x{debug_img.size[1]}, mode={debug_img.mode}")
                # Save debug copy of input
                with open("/tmp/webui_input.png", "wb") as f:
                    f.write(image_bytes)
                print(f"[IMG2IMG-DEBUG] Saved input to /tmp/webui_input.png")
            except Exception as e:
                print(f"[IMG2IMG-DEBUG] Failed to decode image: {e}")
                return {"type": "text", "content": "Invalid image."}

            # Get tags and detect style
            from app.services.wd14_service import tag_image
            tags = tag_image(image_bytes, threshold=0.35) or ""
            print(f"[IMG2IMG] WD14 tags: {tags[:200]}...")
            is_anime = 'anime' in prompt.lower() or 'anime' in tags.lower()
            style = "anime" if is_anime else "realistic"

            # Build prompt with identity preservation tags
            extra_tags = []
            if "dark_skin" in tags or "dark-skinned" in tags:
                extra_tags.append("dark brown skin")
            if any(t in tags.lower() for t in ["fat", "chubby", "plump", "overweight", "plus-size", "bbw"]):
                extra_tags.append("fat, obese, bbw, plus-size body")
            if any(t in tags.lower() for t in ["large_breasts", "huge_breasts"]):
                extra_tags.append("large breasts")
            extra_str = ", ".join(extra_tags)
            print(f"[IMG2IMG] Identity tags: {extra_str}")

            # Check if prompt contains NSFW keywords - add trigger phrase to use NSFW model
            nsfw_keywords = ["nude", "naked", "topless", "bare breasts", "nipples", "nsfw", "undress"]
            is_nsfw = any(kw in prompt.lower() for kw in nsfw_keywords)
            # Add strong NSFW keywords to ensure clothes removal - emphasize bare/exposed skin
            nsfw_trigger = "ecchi, hentai, nude, nipples, topless, exposed breasts, bare skin, naked body, no clothing at all, " if is_nsfw and "ecchi" not in prompt.lower() and "hentai" not in prompt.lower() else ""

            # Add composition guidance - preserve original framing, just ensure face is visible
            composition = "same pose, same framing, face visible"
            final_prompt = f"{nsfw_trigger}{prompt}, {composition}, {extra_str}, {style}, realistic photography".strip(', ')
            # Add more negatives to prevent latex/shiny material transformation
            neg_prompt = f"close-up, cropped, headless, no face, clothing, clothes, shirt, top, bra, pants, shorts, fabric, dressed, wearing, covered, mesh, sheer, latex, rubber, spandex, shiny, glossy, wet look, bodysuit, catsuit, thin, slim, skinny, {'realistic' if is_anime else 'anime'}, deformed"
            print(f"[IMG2IMG-DEBUG] Final prompt: {final_prompt}")
            print(f"[IMG2IMG-DEBUG] Negative prompt: {neg_prompt[:100]}...")

            # Import face detection for retry logic
            from app.services.face_swap_service import swap_face_bytes, detect_face
            from PIL import Image
            import io

            # Retry up to 3 times if generated image has no face
            max_retries = 3
            for attempt in range(max_retries):
                print(f"[IMG2IMG] Generation attempt {attempt + 1}/{max_retries}")

                # Use 0.92 denoise - high value needed to fully remove clothes (lower values create latex effect)
                result_b64 = await self.image_service.generate_img2img(
                    prompt=final_prompt,
                    image_bytes=image_bytes,
                    denoise=0.92,
                    negative_prompt=neg_prompt
                )

                if not result_b64:
                    continue

                # Check if generated image has a face
                generated_bytes = base64.b64decode(result_b64)
                gen_image = Image.open(io.BytesIO(generated_bytes)).convert('RGB')
                print(f"[IMG2IMG-DEBUG] Output image: {len(generated_bytes)} bytes, {gen_image.size[0]}x{gen_image.size[1]}")
                # Save debug copy of output
                with open(f"/tmp/webui_output_{attempt}.png", "wb") as f:
                    f.write(generated_bytes)
                print(f"[IMG2IMG-DEBUG] Saved output to /tmp/webui_output_{attempt}.png")
                gen_face = detect_face(gen_image)

                if gen_face is not None:
                    print(f"[IMG2IMG] Face detected in generated image at bbox: {gen_face.bbox}")
                    break
                else:
                    print(f"[IMG2IMG] No face in generated image, retrying...")
            else:
                print("[IMG2IMG] All retries failed to generate face, using last result")

            if not result_b64:
                return {"type": "text", "content": "Edit failed."}

            # Face swap: paste original face onto generated image
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
                print(f"[IMG2IMG] Face swap error: {e}")

            return {"type": "generated_image", "content": f"Edited: {prompt}", "image": result_b64, "prompt": prompt}

    async def _regen_command(self, last_prompt: Optional[str], stop_check: Optional[callable] = None) -> dict:
        if not last_prompt:
            return {"type": "text", "content": "No previous image prompt found. Use `geni <prompt>` first."}

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

        # Use lock to prevent concurrent image generation
        async with _image_generation_lock:
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
