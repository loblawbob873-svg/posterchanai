import logging
import json
from typing import Optional, Tuple, Callable, TYPE_CHECKING
from sqlalchemy.orm import Session
from app.services.search_service import SearchService
from app.services.image_factory import generate_image_for_user
from app.services.chat_service import ChatService
from app.services.plugin_service import PluginService
from app.services.youtube_service import is_youtube_url, extract_youtube_urls, summarize_youtube
from app.services.torrent_service import scrape_torrents, format_torrent_results, TorrentResult
# Lock now handled inside image_factory for fine-grained control

if TYPE_CHECKING:
    from app.models import User

logger = logging.getLogger(__name__)

# Cache for last torrent search results (per user session - simple in-memory cache)
_torrent_cache: dict[int, list[TorrentResult]] = {}


class CommandService:
    COMMANDS = {
        "help": "Show available commands and plugins",
        "search": "Search the web and get AI-summarized results",
        "images": "Search for images",
        "geni": "Generate an AI image from your prompt",
        "yt": "Summarize a YouTube video: yt <url>",
        "torrents": "Browse torrents: torrents [movies|tv|music|anime] | torrents download <#>",
        "flood": "Torrent manager: flood list | flood add <url> | flood start/stop/delete <hash>",
        "budget": "Budget manager: budget | budget bills | budget add <name> <amount> | budget pay <name>",
        "firewall": "Firewall: firewall | firewall search <ip> [date] | firewall analyze <ip>",
    }

    def __init__(self, db: Session, user: Optional["User"] = None):
        self.db = db
        self.user = user
        self.search_service = SearchService(db)
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
                              stop_check: Optional[Callable[[], bool]] = None) -> dict:
        """Execute a command and return the result"""
        if command == "help":
            return await self._help_command()
        elif command == "search":
            return await self._search_command(arg)
        elif command == "images":
            return await self._images_command(arg)
        elif command == "geni":
            return await self._geni_command(arg, stop_check)
        elif command == "flood":
            return await self._flood_command(arg)
        elif command == "budget":
            return await self._budget_command(arg)
        elif command == "firewall":
            return await self._firewall_command(arg)
        elif command == "yt":
            return await self._youtube_command(arg)
        elif command == "torrents":
            return await self._torrents_command(arg)
        else:
            return {"type": "text", "content": f"Unknown command: {command}"}

    async def _help_command(self) -> dict:
        """Show available commands and plugins"""
        help_text = "## Available Commands\n\n"

        # Built-in commands
        for cmd, desc in self.COMMANDS.items():
            help_text += f"**{cmd}** - {desc}\n"

        # Get user's plugins
        if self.user:
            plugin_service = PluginService(self.db)
            plugins = plugin_service.get_plugins_for_user(self.user.id)

            if plugins:
                help_text += "\n## AI Plugins\n\n"
                help_text += "These plugins are used automatically by the AI when relevant to your request.\n\n"

                for plugin in plugins:
                    help_text += f"### {plugin.name}\n"
                    help_text += f"{plugin.description}\n\n"

                    try:
                        actions = json.loads(plugin.actions)
                        help_text += "**Actions:**\n"
                        for action in actions:
                            help_text += f"- `{action['name']}` - {action['description']}\n"
                    except:
                        pass

                    help_text += "\n"

        help_text += "\n---\n*Plugins are invoked automatically based on your chat message.*"

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

        # Generate image with load balancing support
        # Lock is handled inside image_factory for local generation only
        # Remote requests (load balanced or custom user endpoint) run in parallel
        image_data = await generate_image_for_user(
            db=self.db,
            user=self.user,
            prompt=prompt,
        )

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

        if not image_data:
            return {"type": "text", "content": "Failed to generate image. Please try again or check image generation settings."}

        return {
            "type": "generated_image",
            "content": f"Generated image for: {prompt}",
            "image": image_data,
            "prompt": prompt
        }

    def _format_size(self, size_bytes: int) -> str:
        """Format bytes to human readable size"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} PB"

    def _format_torrent_list(self, result: dict) -> str:
        """Format Flood torrent list as readable text"""
        if not result or isinstance(result, list) and len(result) == 0:
            return "No torrents found."

        # Flood returns {torrents: {...}, id: [...]}
        torrents = result.get('torrents', {})
        if not torrents:
            return "No torrents found."

        lines = ["## Torrents\n"]
        for hash_id, t in torrents.items():
            # Status emoji
            status = t.get('status', [])
            if 'seeding' in status:
                icon = "🌱"
            elif 'downloading' in status:
                icon = "⬇️"
            elif 'stopped' in status or 'paused' in status:
                icon = "⏸️"
            elif 'error' in status:
                icon = "❌"
            else:
                icon = "📦"

            name = t.get('name', 'Unknown')[:50]
            percent = t.get('percentComplete', 0)
            size = self._format_size(t.get('sizeBytes', 0))
            down_rate = self._format_size(t.get('downRate', 0)) + "/s"
            up_rate = self._format_size(t.get('upRate', 0)) + "/s"

            # Progress bar
            filled = int(percent / 10)
            bar = "█" * filled + "░" * (10 - filled)

            lines.append(f"{icon} **{name}**")
            lines.append(f"   [{bar}] {percent:.1f}% | {size}")
            lines.append(f"   ↓ {down_rate} | ↑ {up_rate}")
            lines.append(f"   `{hash_id}`\n")

        return "\n".join(lines)

    async def _flood_command(self, arg: str) -> dict:
        """Direct Flood torrent manager commands"""
        if not self.user:
            return {"type": "text", "content": "Please log in to use Flood commands."}

        plugin_service = PluginService(self.db)
        parts = arg.strip().split(maxsplit=1)
        subcommand = parts[0].lower() if parts else "list"
        param = parts[1] if len(parts) > 1 else ""

        # Sanitize URL/magnet - remove any trailing non-URL characters (emojis, etc.)
        if param and (param.startswith("magnet:") or param.startswith("http")):
            import re
            # Keep only valid URL characters
            param = re.match(r'^[a-zA-Z0-9:/?#\[\]@!$&\'()*+,;=._~%-]+', param)
            param = param.group(0) if param else ""

        try:
            if subcommand in ("list", "ls", ""):
                result = await plugin_service.execute_tool_call("flood", "list", {}, self.user.id)
                if "error" in result:
                    return {"type": "text", "content": f"Flood error: {result['error']}"}
                return {"type": "text", "content": self._format_torrent_list(result)}

            elif subcommand == "add" and param:
                result = await plugin_service.execute_tool_call("flood", "add", {"url": param}, self.user.id)
                if "error" in result:
                    return {"type": "text", "content": f"Flood error: {result['error']}"}
                return {"type": "text", "content": "✅ Torrent added successfully!"}

            elif subcommand in ("del", "delete", "rm") and param:
                result = await plugin_service.execute_tool_call("flood", "delete", {"hashes": param}, self.user.id)
                if "error" in result:
                    return {"type": "text", "content": f"Flood error: {result['error']}"}
                return {"type": "text", "content": "🗑️ Torrent deleted."}

            elif subcommand in ("start", "resume") and param:
                result = await plugin_service.execute_tool_call("flood", "start", {"hashes": param}, self.user.id)
                if "error" in result:
                    return {"type": "text", "content": f"Flood error: {result['error']}"}
                return {"type": "text", "content": "▶️ Torrent started."}

            elif subcommand in ("stop", "pause") and param:
                result = await plugin_service.execute_tool_call("flood", "stop", {"hashes": param}, self.user.id)
                if "error" in result:
                    return {"type": "text", "content": f"Flood error: {result['error']}"}
                return {"type": "text", "content": "⏸️ Torrent stopped."}

            else:
                return {"type": "text", "content": "Usage: `flood list` | `flood add <url>` | `flood start <hash>` | `flood stop <hash>` | `flood delete <hash>`"}

        except Exception as e:
            return {"type": "text", "content": f"Flood error: {str(e)}"}

    async def _budget_command(self, arg: str) -> dict:
        """Direct Budget manager commands"""
        if not self.user:
            return {"type": "text", "content": "Please log in to use Budget commands."}

        plugin_service = PluginService(self.db)
        parts = arg.strip().split()
        subcommand = parts[0].lower() if parts else "summary"

        try:
            if subcommand in ("summary", ""):
                result = await plugin_service.execute_tool_call("budget", "summary", {}, self.user.id)
                action = "summary"
            elif subcommand == "bills":
                result = await plugin_service.execute_tool_call("budget", "bills", {}, self.user.id)
                action = "bills"
            elif subcommand == "add" and len(parts) >= 3:
                name = parts[1]
                amount = parts[2]
                result = await plugin_service.execute_tool_call("budget", "add", {"name": name, "amount": amount}, self.user.id)
                if "error" not in result:
                    return {"type": "text", "content": f"✅ Bill added: {name} - ${float(amount):,.2f}"}
                action = "add"
            elif subcommand in ("pay", "paid") and len(parts) >= 2:
                name = parts[1]
                result = await plugin_service.execute_tool_call("budget", "pay", {"name": name}, self.user.id)
                if "error" not in result:
                    return {"type": "text", "content": f"✅ Bill paid: {name}"}
                action = "pay"
            else:
                return {"type": "text", "content": "Usage: `budget` | `budget bills` | `budget add <name> <amount>` | `budget pay <name>`"}

            formatted = plugin_service.format_result_for_display("budget", action, result)
            return {"type": "text", "content": formatted}
        except Exception as e:
            return {"type": "text", "content": f"Budget error: {str(e)}"}

    async def _firewall_command(self, arg: str) -> dict:
        """Direct Firewall status commands"""
        if not self.user:
            return {"type": "text", "content": "Please log in to use Firewall commands."}

        plugin_service = PluginService(self.db)
        parts = arg.strip().split()
        subcommand = parts[0].lower() if parts else "status"

        try:
            if subcommand in ("status", ""):
                result = await plugin_service.execute_tool_call("firewall", "status", {}, self.user.id)
            elif subcommand == "search" and len(parts) >= 2:
                ip = parts[1]
                date = parts[2] if len(parts) >= 3 else ""
                params = {"ip": ip}
                if date:
                    params["date"] = date
                result = await plugin_service.execute_tool_call("firewall", "search", params, self.user.id)
            elif subcommand in ("analyze", "ai") and len(parts) >= 2:
                ip = parts[1]
                result = await plugin_service.execute_tool_call("firewall", "analyze", {"ip": ip}, self.user.id)
            else:
                return {"type": "text", "content": "Usage: `firewall` | `firewall search <ip> [date]` | `firewall analyze <ip>`"}

            if "error" in result:
                return {"type": "text", "content": f"Firewall error: {result['error']}"}

            formatted = plugin_service.format_result_for_display("firewall", subcommand, result)
            return {"type": "text", "content": formatted}
        except Exception as e:
            return {"type": "text", "content": f"Firewall error: {str(e)}"}

    async def _youtube_command(self, url: str) -> dict:
        """Summarize a YouTube video transcript"""
        if not url:
            return {"type": "text", "content": "Please provide a YouTube URL. Example: `yt https://youtube.com/watch?v=...`"}

        # Extract URL if there's extra text
        urls = extract_youtube_urls(url)
        if not urls:
            return {"type": "text", "content": "Could not find a valid YouTube URL."}

        target_url = urls[0]
        success, result = await summarize_youtube(target_url, self.chat_service)
        return {"type": "text", "content": result}

    async def _torrents_command(self, arg: str) -> dict:
        """Browse torrents from configured torrent site"""
        global _torrent_cache

        parts = arg.strip().split()
        subcommand = parts[0].lower() if parts else "movies"

        # Handle download subcommand
        if subcommand in ("download", "dl", "get"):
            if len(parts) < 2:
                return {"type": "text", "content": "Usage: `torrents download <number>`\nFirst browse with `torrents movies` or another category."}

            try:
                num = int(parts[1])
            except ValueError:
                return {"type": "text", "content": "Please provide a valid number. Example: `torrents download 3`"}

            # Get cached results
            user_id = self.user.id if self.user else 0
            cached = _torrent_cache.get(user_id, [])

            if not cached:
                return {"type": "text", "content": "No torrent results cached. Browse first with `torrents movies`, `torrents tv`, etc."}

            if num < 1 or num > len(cached):
                return {"type": "text", "content": f"Invalid number. Choose between 1 and {len(cached)}."}

            torrent = cached[num - 1]
            magnet = torrent.magnet

            # Use flood command to add the torrent
            if not self.user:
                return {"type": "text", "content": f"**Selected:** {torrent.title}\n\n**Magnet:** `{magnet[:100]}...`\n\nLogin required to add to Flood."}

            # Execute flood add command
            result = await self._flood_command(f"add {magnet}")
            if "error" in result.get("content", "").lower():
                return result

            return {"type": "text", "content": f"**Adding to Flood:** {torrent.title}\n\n{result['content']}"}

        # Handle category browsing
        category = subcommand
        if category not in ("movies", "tv", "music", "anime"):
            category = "movies"

        try:
            results = await scrape_torrents(self.db, category, limit=15)

            if not results:
                return {"type": "text", "content": f"No {category} torrents found. The site may be unavailable or not configured.\n\nAdmin can set `torrent_site_url` in settings."}

            # Cache results for download command
            user_id = self.user.id if self.user else 0
            _torrent_cache[user_id] = results

            formatted = format_torrent_results(results, category)
            formatted += f"\n\n*Use `torrents download <#>` to add to Flood*"

            return {"type": "text", "content": formatted}

        except Exception as e:
            logger.error(f"Torrents command error: {e}")
            return {"type": "text", "content": f"Error fetching torrents: {str(e)}"}

    async def check_youtube_url(self, message: str) -> Optional[dict]:
        """Check if message contains a YouTube URL and summarize it"""
        if not is_youtube_url(message):
            return None

        urls = extract_youtube_urls(message)
        if not urls:
            return None

        # Summarize the first YouTube URL found
        success, result = await summarize_youtube(urls[0], self.chat_service)
        # Return result whether success or failure (so user sees error messages)
        return {"type": "text", "content": result}


def get_command_service(db: Session) -> CommandService:
    return CommandService(db)
