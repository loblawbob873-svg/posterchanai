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
        """Inpaint body area while preserving face"""
        if not prompt or not image_data:
            return {"type": "text", "content": "Need both prompt and image."}

        if stop_check and stop_check():
            return {"type": "text", "content": "Cancelled."}

        async with _image_generation_lock:
            prepare_vram_for_image(self.db)

            try:
                image_bytes = base64.b64decode(image_data)
            except:
                return {"type": "text", "content": "Invalid image."}

            # Body mask - preserves face
            from app.services.mask_service import generate_body_mask
            mask_bytes = generate_body_mask(image_bytes)
            if not mask_bytes:
                return {"type": "text", "content": "Could not generate mask."}

            # Just use prompt directly + detect anime style
            from app.services.wd14_service import tag_image
            tags = tag_image(image_bytes, threshold=0.35) or ""
            is_anime = 'anime' in prompt.lower() or 'anime' in tags.lower()
            style = "anime" if is_anime else "realistic"

            result = await self.image_service.generate_inpaint(
                prompt=f"{prompt}, {style}, high quality",
                image_bytes=image_bytes,
                mask_bytes=mask_bytes,
                denoise=0.85,
                negative_prompt=f"{'realistic' if is_anime else 'anime'}, deformed, bad anatomy"
            )

            if not result:
                return {"type": "text", "content": "Inpaint failed."}

            return {"type": "generated_image", "content": f"Inpainted: {prompt}", "image": result, "prompt": prompt}

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
