from typing import Optional, Tuple
from sqlalchemy.orm import Session
from app.services.search_service import SearchService
from app.services.image_service import ImageService
from app.services.chat_service import ChatService


class CommandService:
    COMMANDS = {
        "search": "Search the web and get AI-summarized results",
        "images": "Search for images",
        "geni": "Generate an AI image from your prompt",
        "regen": "Regenerate the last image with a new seed",
        "help": "Show available commands",
    }

    def __init__(self, db: Session):
        self.db = db
        self.search_service = SearchService(db)
        self.image_service = ImageService(db)
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

    async def execute_command(self, command: str, arg: str, last_prompt: Optional[str] = None) -> dict:
        """Execute a command and return the result"""
        if command == "help":
            return await self._help_command()
        elif command == "search":
            return await self._search_command(arg)
        elif command == "images":
            return await self._images_command(arg)
        elif command == "geni":
            return await self._geni_command(arg)
        elif command == "regen":
            return await self._regen_command(last_prompt)
        else:
            return {"type": "text", "content": f"Unknown command: {command}"}

    async def _help_command(self) -> dict:
        help_text = "**Available Commands:**\n\n"
        for cmd, desc in self.COMMANDS.items():
            help_text += f"**{cmd}** - {desc}\n"
        help_text += "\nJust type your message to chat normally!"
        return {"type": "text", "content": help_text}

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

        results = await self.search_service.image_search(query, limit=8)
        if not results:
            return {"type": "text", "content": f"No images found for: {query}"}

        return {
            "type": "images",
            "content": f"Found {len(results)} images for: {query}",
            "images": results
        }

    async def _geni_command(self, prompt: str) -> dict:
        if not prompt:
            return {"type": "text", "content": "Please provide a prompt. Example: `geni a beautiful sunset over mountains`"}

        image_data = await self.image_service.generate_image(prompt)
        if not image_data:
            return {"type": "text", "content": "Failed to generate image. Please try again or check if ComfyUI is configured."}

        return {
            "type": "generated_image",
            "content": f"Generated image for: {prompt}",
            "image": image_data,
            "prompt": prompt
        }

    async def _regen_command(self, last_prompt: Optional[str]) -> dict:
        if not last_prompt:
            return {"type": "text", "content": "No previous image prompt found. Use `geni <prompt>` first."}

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
