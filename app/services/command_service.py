import base64
from typing import Optional, Tuple
from sqlalchemy.orm import Session
from app.services.search_service import SearchService
from app.services.image_factory import get_image_backend, prepare_vram_for_image
from app.services.chat_service import ChatService


class CommandService:
    COMMANDS = {
        "search": "Search the web and get AI-summarized results",
        "images": "Search for images",
        "geni": "Generate an AI image from your prompt",
        "img2img": "Transform an uploaded image with your prompt",
        "regen": "Regenerate the last image with a new seed",
    }

    def __init__(self, db: Session):
        self.db = db
        self.search_service = SearchService(db)
        self.image_service = get_image_backend(db)
        self.chat_service = ChatService(db)

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
        print(f"[IMG2IMG] Starting - prompt: {prompt[:50] if prompt else 'None'}, has_image: {image_data is not None}")
        if not prompt:
            return {"type": "text", "content": "Please provide a prompt describing what you want."}

        if not image_data:
            return {"type": "text", "content": "Please upload an image to transform."}

        # Check for stop before starting
        if stop_check and stop_check():
            print("[IMG2IMG] Stopped before start")
            return {"type": "text", "content": "Generation cancelled."}

        # Prepare VRAM for image generation (swap models if needed)
        prepare_vram_for_image(self.db)

        try:
            # Decode base64 image
            print(f"[IMG2IMG] Decoding base64 image, length: {len(image_data)}")
            image_bytes = base64.b64decode(image_data)
            print(f"[IMG2IMG] Decoded to {len(image_bytes)} bytes")
        except Exception as e:
            print(f"[IMG2IMG] Failed to decode image: {e}")
            return {"type": "text", "content": f"Invalid image data: {e}"}

        # Check for stop before WD14
        if stop_check and stop_check():
            print("[IMG2IMG] Stopped before WD14")
            return {"type": "text", "content": "Generation cancelled."}

        # First, analyze the image to get original tags
        print("[IMG2IMG] Analyzing source image...")
        original_tags = await self.chat_service.analyze_image_tags(image_data)

        # Check for stop before LLM prompt generation
        if stop_check and stop_check():
            print("[IMG2IMG] Stopped before prompt generation")
            return {"type": "text", "content": "Generation cancelled."}

        # Use AI to optimize the prompt with original tags context
        optimized_prompt, denoise, negative_prompt = await self.chat_service.modify_prompt_for_img2img(prompt, original_tags)

        # Check for stop before image generation
        if stop_check and stop_check():
            print("[IMG2IMG] Stopped before image generation")
            return {"type": "text", "content": "Generation cancelled."}

        # Ensure style keywords from original prompt are preserved for model selection
        prompt_lower = prompt.lower()
        optimized_lower = optimized_prompt.lower()
        if 'anime' in prompt_lower and 'anime' not in optimized_lower:
            optimized_prompt = f"{optimized_prompt}, anime"
            print(f"[IMG2IMG] Added 'anime' to prompt for model selection")
        elif 'realistic' in prompt_lower and 'realistic' not in optimized_lower:
            optimized_prompt = f"{optimized_prompt}, realistic"
            print(f"[IMG2IMG] Added 'realistic' to prompt for model selection")

        # Generate with AI-determined parameters
        result_image = await self.image_service.generate_img2img(
            optimized_prompt, image_bytes,
            denoise=denoise,
            negative_prompt=negative_prompt
        )
        if not result_image:
            return {"type": "text", "content": "Failed to transform image. Please try again."}

        # Auto-log for training
        try:
            from regen_trainer import log_regen_request
            log_regen_request(image_bytes, prompt, source_tags=original_tags)
        except Exception as train_err:
            print(f"[TRAINER] Log failed: {train_err}")

        return {
            "type": "generated_image",
            "content": f"Transformed image: {prompt}",
            "image": result_image,
            "prompt": optimized_prompt  # Store optimized prompt for regen
        }

    async def _regen_command(self, last_prompt: Optional[str], stop_check: Optional[callable] = None) -> dict:
        if not last_prompt:
            return {"type": "text", "content": "No previous image prompt found. Use `geni <prompt>` first."}

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

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
