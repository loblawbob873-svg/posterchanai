import logging
import json
import threading
from typing import Optional, Tuple, Callable, TYPE_CHECKING
from sqlalchemy.orm import Session
from app.services.search_service import SearchService
from app.services.image_factory import generate_image_for_user
from app.services.chat_service import ChatService
from app.services.plugin_service import PluginService
from app.services.youtube_service import is_youtube_url, extract_youtube_urls, summarize_youtube
from app.services.torrent_service import scrape_torrents, format_torrent_results, TorrentResult, scrape_all_categories, format_all_categories
from app.services.nyaa_service import search_nyaa, format_nyaa_results, NyaaResult
from app.services.miniflux_service import MinifluxService
from app.routers.news import fetch_news_from_source, get_user_news_sources
from app.services.caldav_service import (
    get_all_user_events, get_user_calendars, get_user_contacts,
    format_events_for_display, format_contacts_for_display,
    add_event_to_calendar
)
from app.services.mail_service import (
    fetch_all_accounts, get_message_by_id, delete_message, archive_message,
    reply_to_message, send_email, get_user_mail_accounts,
    format_message_list, format_message_detail
)
# Lock now handled inside image_factory for fine-grained control

if TYPE_CHECKING:
    from app.models import User

logger = logging.getLogger(__name__)

# Cache for torrent results (per user, per category) - thread-safe with locks
_torrent_cache: dict[int, dict[str, list[TorrentResult]]] = {}
_nyaa_cache: dict[int, list[NyaaResult]] = {}
# Cache for flood torrent number-to-hash mapping (per user)
_flood_hash_map: dict[int, dict[int, str]] = {}
# Locks for thread-safe cache access
_cache_lock = threading.Lock()


class CommandService:
    COMMANDS = {
        "help": "Show available commands and plugins",
        "search": "Search the web and get AI-summarized results",
        "images": "Search for images",
        "geni": "Generate an AI image from your prompt",
        "yt": "Summarize a YouTube video: yt <url>",
        "torrents": "Torrents: torrents | torrents <category> | torrents dl <#> | torrents list | torrents add <url>",
        "nyaa": "Search nyaa.si: nyaa <query> | nyaa download <#>",
        "budget": "Budget manager: budget | budget bills | budget add <name> <amount> | budget pay <name>",
        "firewall": "Firewall: firewall | firewall search <ip> [date] | firewall analyze <ip>",
        "news": "Get unread news from Miniflux: news | news refresh",
        "dailynews": "Get news from configured sources: dailynews",
        "logs": "System logs analysis (admin only): logs",
        "miniflux": "Fetch Miniflux articles now: miniflux",
        "cal": "Calendar: cal | cal today | cal week | cal add <event> <time>",
        "contacts": "Search contacts: contacts <query>",
        "mail": "Email: mail | mail <contact> <msg> | mail read/reply/delete/archive <account> <id>",
    }

    # Command aliases (alias -> canonical command)
    COMMAND_ALIASES = {
        "schedule": "cal",
        "sched": "cal",
        "flood": "torrents",  # Combine flood into torrents command
    }

    def __init__(self, db: Session, user: Optional["User"] = None):
        self.db = db
        self.user = user
        self.search_service = SearchService(db)
        self.chat_service = ChatService(db, user=user)

    def parse_command(self, message: str) -> Tuple[Optional[str], str]:
        """Parse message for commands, return (command, argument)"""
        lower = message.lower().strip()

        # Check canonical commands first
        for cmd in self.COMMANDS:
            if lower.startswith(f"{cmd} "):
                return cmd, message[len(cmd)+1:].strip()
            if lower == cmd:
                return cmd, ""

        # Check aliases
        for alias, canonical in self.COMMAND_ALIASES.items():
            if lower.startswith(f"{alias} "):
                return canonical, message[len(alias)+1:].strip()
            if lower == alias:
                return canonical, ""

        return None, message

    async def execute_command(self, command: str, arg: str, last_prompt: Optional[str] = None,
                              stop_check: Optional[Callable[[], bool]] = None,
                              attachments: Optional[list] = None) -> dict:
        """Execute a command and return the result.

        Args:
            command: The command name
            arg: Command arguments
            last_prompt: Last image generation prompt (for regeneration)
            stop_check: Callable to check if execution should stop
            attachments: List of (filename, data_bytes, content_type) tuples for mail
        """
        if command == "help":
            return await self._help_command()
        elif command == "search":
            return await self._search_command(arg)
        elif command == "images":
            return await self._images_command(arg)
        elif command == "geni":
            return await self._geni_command(arg, stop_check)
        elif command == "budget":
            return await self._budget_command(arg)
        elif command == "firewall":
            return await self._firewall_command(arg)
        elif command == "yt":
            return await self._youtube_command(arg)
        elif command == "torrents":
            return await self._torrents_command(arg)
        elif command == "nyaa":
            return await self._nyaa_command(arg)
        elif command == "news":
            return await self._news_command(arg)
        elif command == "dailynews":
            return await self._dailynews_command(arg)
        elif command == "logs":
            return await self._logs_command(arg)
        elif command == "miniflux":
            return await self._miniflux_command(arg)
        elif command == "cal":
            return await self._schedule_command(arg)
        elif command == "contacts":
            return await self._contacts_command(arg)
        elif command == "mail":
            return await self._mail_command(arg, attachments=attachments)
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
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass  # Skip malformed plugin actions

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
        """Format Flood torrent list as readable text with numbered entries"""
        global _flood_hash_map

        if not result or isinstance(result, list) and len(result) == 0:
            return "No torrents found."

        # Flood returns {torrents: {...}, id: [...]}
        torrents = result.get('torrents', {})
        if not torrents:
            return "No torrents found."

        # Build number-to-hash mapping for this user
        user_id = self.user.id if self.user else 0
        _flood_hash_map[user_id] = {}

        lines = ["## Torrents\n"]
        for num, (hash_id, t) in enumerate(torrents.items(), 1):
            # Store mapping
            _flood_hash_map[user_id][num] = hash_id

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

            lines.append(f"**{num}.** {icon} **{name}**")
            lines.append(f"   [{bar}] {percent:.1f}% | {size}")
            lines.append(f"   ↓ {down_rate} | ↑ {up_rate}\n")

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

        # Resolve number to hash if param is a number
        if param and param.isdigit():
            num = int(param)
            user_map = _flood_hash_map.get(self.user.id, {})
            if num in user_map:
                param = user_map[num]
            else:
                return {"type": "text", "content": f"Invalid torrent number: {num}. Run `flood list` first."}

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
                return {"type": "text", "content": "Usage: `flood list` | `flood add <url>` | `flood start <#>` | `flood stop <#>` | `flood delete <#>`"}

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
        """Browse torrents and manage Flood client"""
        global _torrent_cache

        parts = arg.strip().split()
        subcommand = parts[0].lower() if parts else ""
        categories = ("movies", "tv", "music", "anime")

        # Flood client management subcommands
        if subcommand in ("list", "ls"):
            return await self._flood_command("list")
        elif subcommand == "add" and len(parts) > 1:
            url = parts[1]
            return await self._flood_command(f"add {url}")
        elif subcommand in ("start", "resume") and len(parts) > 1:
            num = parts[1]
            return await self._flood_command(f"start {num}")
        elif subcommand in ("stop", "pause") and len(parts) > 1:
            num = parts[1]
            return await self._flood_command(f"stop {num}")
        elif subcommand in ("del", "delete", "rm") and len(parts) > 1:
            num = parts[1]
            return await self._flood_command(f"delete {num}")

        # Handle download subcommand: torrents download <category> <number>
        if subcommand in ("download", "dl", "get"):
            if len(parts) < 3:
                return {"type": "text", "content": "Usage: `torrents download <category> <number>`\n\nExample: `torrents download anime 5`"}

            category = parts[1].lower()
            if category not in categories:
                return {"type": "text", "content": f"Unknown category: `{category}`\n\nAvailable: movies, tv, music, anime"}

            try:
                num = int(parts[2])
            except ValueError:
                return {"type": "text", "content": "Please provide a valid number. Example: `torrents download anime 5`"}

            # Get cached results for this category
            user_id = self.user.id if self.user else 0
            user_cache = _torrent_cache.get(user_id, {})
            cached = user_cache.get(category, [])

            if not cached:
                return {"type": "text", "content": f"No {category} results cached. Run `torrents` first to load results."}

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

        # No subcommand - show all categories overview
        if not subcommand:
            try:
                all_results = await scrape_all_categories(self.db, limit_per_category=10)

                # Cache all results by category
                user_id = self.user.id if self.user else 0
                _torrent_cache[user_id] = all_results

                formatted = format_all_categories(all_results)
                return {"type": "text", "content": formatted}
            except Exception as e:
                logger.error(f"Torrents command error: {e}")
                return {"type": "text", "content": f"Error fetching torrents: {str(e)}"}

        # Handle category browsing
        category = subcommand
        if category not in categories:
            return {"type": "text", "content": f"Unknown category: `{subcommand}`\n\nAvailable: `torrents movies`, `torrents tv`, `torrents music`, `torrents anime`"}

        try:
            results = await scrape_torrents(self.db, category, limit=15)

            if not results:
                return {"type": "text", "content": f"No {category} torrents found. The site may be unavailable or not configured.\n\nAdmin can set `torrent_site_url` in settings."}

            # Cache results for download command
            user_id = self.user.id if self.user else 0
            if user_id not in _torrent_cache:
                _torrent_cache[user_id] = {}
            _torrent_cache[user_id][category] = results

            formatted = format_torrent_results(results, category)
            formatted += f"\n\n*Use `torrents download {category} <#>` to add to Flood*"

            return {"type": "text", "content": formatted}

        except Exception as e:
            logger.error(f"Torrents command error: {e}")
            return {"type": "text", "content": f"Error fetching torrents: {str(e)}"}

    async def _nyaa_command(self, arg: str) -> dict:
        """Search nyaa.si for anime torrents"""
        global _nyaa_cache

        parts = arg.strip().split()
        if not parts:
            return {"type": "text", "content": "Usage: `nyaa <search query>`\n\nExample: `nyaa one piece 1080p`"}

        subcommand = parts[0].lower()

        # Handle download subcommand
        if subcommand in ("download", "dl", "get"):
            if len(parts) < 2:
                return {"type": "text", "content": "Usage: `nyaa download <number>`\nFirst search with `nyaa <query>`."}

            try:
                num = int(parts[1])
            except ValueError:
                return {"type": "text", "content": "Please provide a valid number. Example: `nyaa download 3`"}

            # Get cached results
            user_id = self.user.id if self.user else 0
            cached = _nyaa_cache.get(user_id, [])

            if not cached:
                return {"type": "text", "content": "No nyaa results cached. Search first with `nyaa <query>`."}

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

        # Search query
        query = arg.strip()

        try:
            results = await search_nyaa(query, limit=15)

            if not results:
                return {"type": "text", "content": f"No results found for '{query}' on nyaa.si"}

            # Cache results for download command
            user_id = self.user.id if self.user else 0
            _nyaa_cache[user_id] = results

            formatted = format_nyaa_results(results, query)

            return {"type": "text", "content": formatted}

        except Exception as e:
            logger.error(f"Nyaa command error: {e}")
            return {"type": "text", "content": f"Error searching nyaa.si: {str(e)}"}

    async def _news_command(self, arg: str) -> dict:
        """Get unread news from Miniflux with AI summaries"""
        import re
        from datetime import datetime

        if not self.user:
            return {"type": "text", "content": "Please log in to use Miniflux news."}

        # Check if user has Miniflux enabled
        if not self.user.miniflux_enabled:
            return {"type": "text", "content": "Miniflux is disabled for your account. Enable it in settings."}

        # Get Miniflux service
        miniflux = MinifluxService.from_settings(self.db, self.user)
        if not miniflux:
            return {"type": "text", "content": "Miniflux is not configured. Ask your admin to set it up in the admin panel."}

        parts = arg.strip().split()
        subcommand = parts[0].lower() if parts else ""

        # Handle refresh - force fetch new articles
        if subcommand in ("refresh", "fetch", "update"):
            pass  # Same as default, just fetch

        try:
            # Fetch unread entries
            entries = await miniflux.get_unread_entries(limit=20)

            if not entries:
                return {"type": "text", "content": "No unread articles in Miniflux."}

            # Build response with summaries
            summaries = []
            entry_ids = []

            for entry in entries:
                entry_id = entry.get("id")
                title = entry.get("title", "Untitled")
                url = entry.get("url", "")
                content = entry.get("content", "")
                feed_title = entry.get("feed", {}).get("title", "Unknown Feed")

                # Convert HTML to text
                text_content = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL | re.IGNORECASE)
                text_content = re.sub(r'<style[^>]*>.*?</style>', '', text_content, flags=re.DOTALL | re.IGNORECASE)
                text_content = re.sub(r'<[^>]+>', ' ', text_content)
                text_content = re.sub(r'\s+', ' ', text_content).strip()

                # Truncate for summarization
                if len(text_content) > 6000:
                    text_content = text_content[:6000] + "..."

                # Generate AI summary
                messages = [
                    {"role": "system", "content": "You are a news summarizer. Provide a concise 2-3 sentence summary of this article. Focus on the key facts."},
                    {"role": "user", "content": f"Title: {title}\n\nContent:\n{text_content}"}
                ]

                try:
                    summary = await self.chat_service.chat(messages)
                except Exception as e:
                    summary = f"(Error summarizing: {str(e)[:50]})"

                summaries.append(f"**{title}**\n*{feed_title}*\n{url}\n\n{summary}")
                entry_ids.append(entry_id)

            # Mark all as read
            await miniflux.mark_entries_as_read(entry_ids)

            # Format response
            timestamp = datetime.now().strftime("%B %d, %Y %H:%M")
            result = f"## News Update - {timestamp}\n\n" + "\n\n---\n\n".join(summaries)
            result += f"\n\n---\n*Marked {len(entry_ids)} articles as read*"

            return {"type": "text", "content": result}

        except Exception as e:
            logger.error(f"News command error: {e}")
            return {"type": "text", "content": f"Error fetching news: {str(e)}"}

    async def _dailynews_command(self, arg: str) -> dict:
        """Get news from configured web sources (CNN, NPR, etc.)"""
        from datetime import datetime

        if not self.user:
            return {"type": "text", "content": "Please log in to use Daily News."}

        try:
            # Get news sources (user's custom sources or admin defaults)
            sources = get_user_news_sources(self.user, self.db)

            if not sources:
                return {"type": "text", "content": "No news sources configured. Add sources in User Settings."}

            # Fetch news from all sources
            results = []
            for source in sources:
                try:
                    markdown = await fetch_news_from_source(source["url"], source["name"], self.db)
                    results.append(markdown)
                except Exception as e:
                    logger.error(f"Error fetching news from {source['name']}: {e}")
                    results.append(f"**{source['name']}:** Error fetching headlines")

            # Format response
            today = datetime.now().strftime("%B %d, %Y %H:%M")
            content = f"## Daily News - {today}\n\n" + "\n\n---\n\n".join(results)

            return {"type": "text", "content": content}

        except Exception as e:
            logger.error(f"Daily news command error: {e}")
            return {"type": "text", "content": f"Error fetching daily news: {str(e)}"}

    async def _logs_command(self, arg: str) -> dict:
        """Collect system logs and generate AI summary (admin only)"""
        if not self.user:
            return {"type": "text", "content": "Please log in to use the logs command."}

        # Admin only (user ID 1)
        if self.user.id != 1:
            return {"type": "text", "content": "The logs command is only available to administrators."}

        try:
            from app.services.logs_scheduler import collect_system_logs, generate_log_summary, get_or_create_logs_chat
            from app.models import Message
            from datetime import datetime
            import socket

            # Collect logs (pass db for settings)
            log_data = collect_system_logs(self.db)
            if not log_data:
                return {"type": "text", "content": "No log data collected."}

            # Generate AI summary
            summary = await generate_log_summary(self.db, self.user, log_data)

            # Store in Logs conversation
            logs_chat = get_or_create_logs_chat(self.db, self.user.id)

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            hostname = socket.gethostname()
            message_text = f"## System Log Report - {hostname}\n*{timestamp}*\n\n{summary}"

            log_msg = Message(
                conversation_id=logs_chat.id,
                role="assistant",
                content=message_text
            )
            self.db.add(log_msg)
            logs_chat.updated_at = datetime.utcnow()
            self.db.commit()

            return {"type": "text", "content": message_text}

        except Exception as e:
            logger.error(f"Logs command error: {e}")
            return {"type": "text", "content": f"Error collecting logs: {str(e)}"}

    async def _miniflux_command(self, arg: str) -> dict:
        """Manually trigger Miniflux article fetch"""
        if not self.user:
            return {"type": "text", "content": "Please log in to use the miniflux command."}

        # Check if user has Miniflux enabled
        if not self.user.miniflux_enabled:
            return {"type": "text", "content": "Miniflux is disabled for your account. Enable it in settings."}

        try:
            from app.services.miniflux_scheduler import process_miniflux_news_for_user

            await process_miniflux_news_for_user(self.user.id)
            return {"type": "text", "content": "Miniflux articles fetched and added to your Miniflux conversation."}

        except Exception as e:
            logger.error(f"Miniflux command error: {e}")
            return {"type": "text", "content": f"Error fetching Miniflux articles: {str(e)}"}

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

    async def _schedule_command(self, arg: str) -> dict:
        """Calendar/Schedule commands"""
        from datetime import datetime, timedelta
        from dateutil import parser as date_parser

        if not self.user:
            return {"type": "text", "content": "Please log in to use the cal command."}

        # Check if user has calendars configured
        calendars = get_user_calendars(self.user.id, self.db)
        if not calendars:
            return {"type": "text", "content": "No calendars configured. Add calendars in User Settings."}

        parts = arg.strip().split(maxsplit=1)
        subcommand = parts[0].lower() if parts else "today"
        param = parts[1] if len(parts) > 1 else ""

        try:
            if subcommand in ("today", ""):
                # Get today's events
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                tomorrow = today + timedelta(days=1)
                events = get_all_user_events(self.user.id, today, tomorrow, self.db)
                events_text = format_events_for_display(events, include_description=True)

                date_str = today.strftime("%A, %B %d, %Y")
                return {"type": "text", "content": f"## Schedule for {date_str}\n\n{events_text}"}

            elif subcommand == "week":
                # Get this week's events
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                week_end = today + timedelta(days=7)
                events = get_all_user_events(self.user.id, today, week_end, self.db)
                events_text = format_events_for_display(events, include_description=True)

                return {"type": "text", "content": f"## Schedule for the Week\n\n{events_text}"}

            elif subcommand == "add":
                if not param:
                    return {"type": "text", "content": "Usage: `cal add <event name> <time>`\n\nExample: `cal add Meeting with John tomorrow at 3pm`"}

                # Use AI to parse the event
                messages = [
                    {"role": "system", "content": """Parse this event and return JSON with:
- summary: event name
- description: any details mentioned
- start_time: ISO format datetime (assume today's date if not specified)
- end_time: ISO format datetime (default 1 hour after start)
- location: place if mentioned

Return ONLY valid JSON, no other text."""},
                    {"role": "user", "content": f"Parse this event: {param}"}
                ]

                try:
                    import json
                    import re
                    parsed = await self.chat_service.chat(messages)
                    logger.debug(f"AI response for event parsing: {parsed[:200]}")

                    # Clean up markdown code blocks if present
                    parsed = parsed.strip()
                    # Handle ```json or ``` blocks
                    code_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', parsed)
                    if code_block_match:
                        parsed = code_block_match.group(1).strip()
                    else:
                        # Try to extract JSON object directly
                        json_match = re.search(r'\{[\s\S]*\}', parsed)
                        if json_match:
                            parsed = json_match.group(0)

                    logger.debug(f"Cleaned JSON: {parsed[:200]}")
                    event_data = json.loads(parsed)

                    summary = event_data.get("summary", param)
                    description = event_data.get("description", "")
                    start_str = event_data.get("start_time", "")
                    end_str = event_data.get("end_time", "")
                    location = event_data.get("location")

                    logger.info(f"Parsed event: summary={summary}, start={start_str}, end={end_str}")

                    # Parse dates
                    start_time = date_parser.parse(start_str) if start_str else datetime.now() + timedelta(hours=1)
                    end_time = date_parser.parse(end_str) if end_str else start_time + timedelta(hours=1)

                    logger.info(f"Event times: {start_time} - {end_time}")

                    # Add to first calendar
                    cal = calendars[0]
                    logger.info(f"Adding to calendar: {cal['name']} ({cal['url']})")
                    success = add_event_to_calendar(
                        cal['url'], cal['username'], cal['password'],
                        summary, description, start_time, end_time, location
                    )

                    if success:
                        time_str = start_time.strftime("%A, %B %d at %I:%M %p")
                        logger.info(f"Event added successfully: {summary} at {time_str}")
                        return {"type": "text", "content": f"✅ Event added: **{summary}**\n\n📅 {time_str}"}
                    else:
                        logger.error(f"Failed to add event: {summary}")
                        return {"type": "text", "content": "❌ Failed to add event to calendar."}

                except json.JSONDecodeError:
                    return {"type": "text", "content": "Could not parse event details. Try: `cal add Meeting tomorrow at 3pm`"}
                except Exception as e:
                    logger.error(f"Error adding event: {e}")
                    return {"type": "text", "content": f"Error adding event: {str(e)}"}

            else:
                return {"type": "text", "content": "Usage:\n- `cal` or `cal today` - Today's events\n- `cal week` - This week's events\n- `cal add <event> <time>` - Add an event"}

        except Exception as e:
            logger.error(f"Schedule command error: {e}")
            return {"type": "text", "content": f"Error: {str(e)}"}

    async def _contacts_command(self, arg: str) -> dict:
        """Search contacts"""
        if not self.user:
            return {"type": "text", "content": "Please log in to use the contacts command."}

        if not arg.strip():
            return {"type": "text", "content": "Usage: `contacts <search query>`\n\nExample: `contacts John` or `contacts @company.com`"}

        query = arg.strip()

        try:
            contacts = get_user_contacts(self.user.id, query, self.db)
            if not contacts:
                return {"type": "text", "content": f"No contacts found matching '{query}'."}

            contacts_text = format_contacts_for_display(contacts)
            return {"type": "text", "content": f"## Contacts matching '{query}'\n{contacts_text}"}

        except Exception as e:
            logger.error(f"Contacts command error: {e}")
            return {"type": "text", "content": f"Error searching contacts: {str(e)}"}

    async def _mail_command(self, arg: str, attachments: Optional[list] = None) -> dict:
        """Email commands - inbox, read, reply, delete, send"""
        if not self.user:
            return {"type": "text", "content": "Please log in to use the mail command."}

        accounts = get_user_mail_accounts(self.user.id, self.db)
        if not accounts:
            return {"type": "text", "content": "No email accounts configured. Add accounts in User Settings > Mail."}

        parts = arg.strip().split(maxsplit=3)
        subcommand = parts[0].lower() if parts else "inbox"

        try:
            if subcommand in ("inbox", ""):
                # List recent messages from all accounts
                messages = fetch_all_accounts(self.user.id, self.db, limit_per_account=10)
                return {"type": "text", "content": format_message_list(messages)}

            elif subcommand == "unread":
                # List unread messages only
                messages = fetch_all_accounts(self.user.id, self.db, limit_per_account=20, unread_only=True)
                if not messages:
                    return {"type": "text", "content": "No unread messages."}
                return {"type": "text", "content": format_message_list(messages)}

            elif subcommand == "read":
                if len(parts) < 3:
                    return {"type": "text", "content": "Usage: `mail read <account> <id>`\n\nExample: `mail read verita84 123`"}

                account_hint = parts[1]
                uid = parts[2]

                # Find matching account
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                msg = get_message_by_id(self.user.id, self.db, account_email, uid)
                if not msg:
                    return {"type": "text", "content": f"Message {uid} not found."}

                return {"type": "text", "content": format_message_detail(msg)}

            elif subcommand == "reply":
                if len(parts) < 4:
                    return {"type": "text", "content": "Usage: `mail reply <account> <id> <message>`\n\nExample: `mail reply verita84 123 Thanks for the info!`"}

                account_hint = parts[1]
                uid = parts[2]
                reply_body = parts[3]

                # Find matching account
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                success = reply_to_message(self.user.id, self.db, account_email, uid, reply_body)
                if success:
                    return {"type": "text", "content": "Reply sent successfully."}
                else:
                    return {"type": "text", "content": "Failed to send reply."}

            elif subcommand == "delete":
                if len(parts) < 3:
                    return {"type": "text", "content": "Usage: `mail delete <account> <id>`\n\nExample: `mail delete verita84 123`"}

                account_hint = parts[1]
                uid = parts[2]

                # Find matching account
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                success = delete_message(self.user.id, self.db, account_email, uid)
                if success:
                    return {"type": "text", "content": f"Message {uid} deleted."}
                else:
                    return {"type": "text", "content": f"Failed to delete message {uid}."}

            elif subcommand == "archive":
                if len(parts) < 3:
                    return {"type": "text", "content": "Usage: `mail archive <account> <id>`\n\nExample: `mail archive verita84 123`"}

                account_hint = parts[1]
                uid = parts[2]

                # Find matching account
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                success = archive_message(self.user.id, self.db, account_email, uid)
                if success:
                    return {"type": "text", "content": f"📦 Message {uid} archived."}
                else:
                    return {"type": "text", "content": f"Failed to archive message {uid}."}

            elif subcommand == "send":
                # Explicit send: mail send <recipient> <message>
                if len(parts) < 3:
                    return {"type": "text", "content": "Usage: `mail send <recipient> <message>`\n\nExample: `mail send linda Hey, how are you?`"}
                recipient = parts[1]
                message_body = parts[2] if len(parts) > 2 else ""
                # Re-split to get full message after recipient
                full_parts = arg.strip().split(maxsplit=2)
                if len(full_parts) > 2:
                    message_body = full_parts[2]
                return await self._send_new_mail(accounts, recipient, message_body, attachments)

            else:
                # Check if this is a shorthand send: mail <recipient> <message>
                # First word is not a known subcommand, treat as recipient
                if len(parts) >= 2:
                    recipient = parts[0]
                    # Get the full message after the recipient
                    full_parts = arg.strip().split(maxsplit=1)
                    message_body = full_parts[1] if len(full_parts) > 1 else ""
                    return await self._send_new_mail(accounts, recipient, message_body, attachments)

                return {"type": "text", "content": "Usage:\n- `mail` - Recent messages\n- `mail <contact> <message>` - Send new email (with attachments)\n- `mail read <account> <id>` - Read message\n- `mail reply <account> <id> <message>` - Reply\n- `mail archive <account> <id>` - Archive message\n- `mail delete <account> <id>` - Delete message"}

        except Exception as e:
            logger.error(f"Mail command error: {e}")
            return {"type": "text", "content": f"Error: {str(e)}"}

    async def _send_new_mail(self, accounts: list, recipient: str, message_body: str,
                              attachments: Optional[list] = None) -> dict:
        """Send a new email, resolving contact name to email if needed."""
        import re

        if not message_body:
            return {"type": "text", "content": "Please provide a message. Example: `mail linda Hey, how are you?`"}

        # Determine if recipient is an email or a contact name
        to_email = None
        contact_name = None

        if '@' in recipient:
            # It's already an email address
            to_email = recipient
        else:
            # Search contacts for matching name
            contacts = get_user_contacts(self.user.id, recipient, self.db)
            if not contacts:
                return {"type": "text", "content": f"No contact found matching '{recipient}'. Try:\n- `mail linda hello` (contact name)\n- `mail linda@example.com hello` (email address)"}

            # Find first contact with an email
            for contact in contacts:
                if contact.email:
                    to_email = contact.email
                    contact_name = contact.name
                    break

            if not to_email:
                return {"type": "text", "content": f"Contact '{recipient}' found but has no email address."}

        # Use first configured account to send
        from_account = accounts[0]

        # Generate subject from first part of message (up to 50 chars or first sentence)
        subject_text = message_body[:50].split('.')[0].split('!')[0].split('?')[0]
        if len(subject_text) < len(message_body):
            subject_text = subject_text.strip() + "..."
        else:
            subject_text = subject_text.strip()

        success = send_email(from_account, to_email, subject_text, message_body,
                             attachments=attachments)

        if success:
            attachment_note = f" with {len(attachments)} attachment(s)" if attachments else ""
            if contact_name:
                return {"type": "text", "content": f"✅ Email sent to **{contact_name}** ({to_email}){attachment_note}"}
            else:
                return {"type": "text", "content": f"✅ Email sent to {to_email}{attachment_note}"}
        else:
            return {"type": "text", "content": f"❌ Failed to send email to {to_email}"}


def get_command_service(db: Session) -> CommandService:
    return CommandService(db)
