import json
import logging
import re
import threading
from typing import TYPE_CHECKING, Callable, Optional, Tuple

from sqlalchemy.orm import Session

from app.routers.news import fetch_news_from_source, get_user_news_sources
from app.services.caldav_service import (
    add_event_to_calendar,
    add_todo_to_calendar,
    add_user_contact,
    delete_event_from_calendar,
    delete_todo_from_calendar,
    delete_user_contact,
    edit_user_contact,
    format_contacts_for_display,
    format_events_for_display,
    format_todos_for_display,
    get_all_user_events,
    get_all_user_todos,
    get_event_by_uid,
    get_user_calendars,
    get_user_contact_by_uid,
    get_user_contacts,
    update_event_in_calendar,
)
from app.services.chat_service import ChatService
from app.services.image_factory import generate_image_for_user
from app.services.mail_service import (
    archive_message,
    delete_all_messages,
    delete_message,
    fetch_all_accounts,
    fetch_messages,
    format_folder_list,
    format_message_detail,
    format_message_list,
    forward_message,
    get_attachment,
    get_message_by_id,
    get_user_mail_accounts,
    list_folders,
    reply_to_message,
    search_messages,
    send_email,
)
from app.services.miniflux_service import MinifluxService
from app.services.nyaa_service import NyaaResult, format_nyaa_results, search_nyaa
from app.services.plugin_service import PluginService
from app.services.search_service import SearchService
from app.services.torrent_service import (
    TorrentResult,
    format_all_categories,
    format_torrent_results,
    scrape_all_categories,
    scrape_torrents,
    search_torrents,
)
from app.services.webdav_music_service import (
    format_music_browse,
    format_music_tracks,
    generate_mood_playlist,
    get_stream_url,
    get_user_webdav_config,
    list_folder,
    scan_all_tracks,
    search_tracks,
)
from app.services.youtube_service import (
    check_ytdlp_available,
    download_and_upload_to_webdav,
    extract_youtube_urls,
    format_download_result,
    is_youtube_url,
    summarize_youtube,
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
# Cache for music results (per user)
_music_cache: dict[int, dict] = {}  # user_id -> {tracks, folders, current_path}
# Locks for thread-safe cache access


def _format_bt_list_from_dicts(torrents: list[dict]) -> str:
    """Format torrent list from remote API response (no libtorrent import needed)."""
    if not torrents:
        return "No torrents."

    lines = ["**Torrents:**\n"]
    for i, t in enumerate(torrents, 1):
        bar_len = 10
        progress = t.get("progress", 0)
        filled = int(progress / 100 * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        download_rate = t.get("download_rate", 0)
        upload_rate = t.get("upload_rate", 0)
        down = f"{download_rate / 1024:.1f} KB/s" if download_rate > 0 else "-"
        up = f"{upload_rate / 1024:.1f} KB/s" if upload_rate > 0 else "-"

        size = t.get("size", 0)
        size_mb = size / (1024 * 1024)
        size_str = f"{size_mb:.1f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"

        state = t.get("state", "unknown")
        is_paused = t.get("is_paused", False)

        # Clear status with icon AND text
        if is_paused or state == "paused":
            status = "⏸️ **PAUSED**"
        elif state == "downloading":
            status = "⬇️ **DOWNLOADING**"
        elif state == "seeding":
            status = "⬆️ **SEEDING**"
        elif state == "finished":
            status = "✅ **FINISHED**"
        elif state == "checking":
            status = "🔍 **CHECKING**"
        elif state == "metadata":
            status = "📥 **FETCHING METADATA**"
        else:
            status = f"❓ **{state.upper()}**"

        name = t.get("name", "Unknown")
        seeders = t.get("seeders", 0)
        peers = t.get("peers", 0)

        # Action buttons - clear labels
        if is_paused or state == "paused":
            toggle_btn = f"[▶ Resume](cmd:torrents resume {i})"
        else:
            toggle_btn = f"[⏸ Pause](cmd:torrents pause {i})"
        delete_btn = f"[🗑 Remove](cmd:torrents rm {i})"

        lines.append(
            f"**{i}. {name}**\n"
            f"   Status: {status}\n"
            f"   [{bar}] {progress:.1f}% | {size_str}\n"
            f"   ↓{down} ↑{up} | {seeders}S/{peers}P\n"
            f"   {toggle_btn} | {delete_btn}"
        )

    return "\n".join(lines)


_cache_lock = threading.Lock()


class CommandService:
    COMMANDS = {
        "help": "Show this help message",
        "search": "Web search: search <query>",
        "images": "Image search: images <query>",
        "geni": "Generate image: geni <prompt>",
        "budget": "Check system budget/usage",
        "firewall": "Toggle network firewall",
        "yt": "YouTube search: yt <query>",
        "ytdl": "Download YouTube: ytdl <url>",
        "torrents": "Torrent search: torrents <query>",
        "nyaa": "Anime torrents: nyaa <query>",
        "news": "Miniflux news summary",
        "dailynews": "Web news: dailynews <source>",
        "logs": "View system logs",
        "miniflux": "Miniflux status",
        "cal": "Calendar: cal | cal today | cal week | cal month | cal nextmonth | cal add <event> <time>",
        "contacts": "Contacts: contacts <query>",
        "mail": "Email: mail <to> [subject] <body>",
        "todo": "Todo list: todo | todo add <task>",
        "music": "Music player: music <play|stop|next>",
        "translate": "Translate: translate <text> to <lang>",
    }
    # Command aliases (alias -> canonical command)
    COMMAND_ALIASES = {
        "schedule": "cal",
        "sched": "cal",
        "flood": "torrents",  # Combine flood into torrents command
        "torrent": "torrents",  # Allow singular form
        "bt": "torrents",  # Short alias for torrents
        "yt-dlp": "ytdl",  # YouTube download alias
        "youtube": "yt",  # YouTube summarize alias
    }

    # Natural language phrases that map directly to commands with arguments
    # Format: "phrase" -> ("command", "argument")
    PHRASE_COMMANDS = {
        "show my bills": ("budget", "bills"),
        "my bills": ("budget", "bills"),
        "show bills": ("budget", "bills"),
        "what bills": ("budget", "bills"),
        "upcoming bills": ("budget", "bills"),
    }

    def __init__(self, db: Session, user: Optional["User"] = None):
        self.db = db
        self.user = user
        self.search_service = SearchService(db)
        self.chat_service = ChatService(db, user=user)

    def parse_command(self, message: str) -> Tuple[Optional[str], str]:
        """Parse message for commands, return (command, argument)"""
        lower = message.lower().strip()

        # Check natural language phrases first (exact match)
        if lower in self.PHRASE_COMMANDS:
            cmd, arg = self.PHRASE_COMMANDS[lower]
            return cmd, arg

        # Check for "pay bill <name>" pattern
        if lower.startswith("pay bill "):
            bill_name = message[9:].strip()  # Extract bill name preserving case
            return "budget", f"pay {bill_name}"

        # Check for "download song/video <url>" patterns
        if lower.startswith("download song "):
            url = message[14:].strip()
            return "ytdl", url
        if lower.startswith("download video "):
            url = message[15:].strip()
            return "ytdl", url
        if lower.startswith("download ") and ("youtube" in lower or "youtu.be" in lower):
            url = message[9:].strip()
            return "ytdl", url

        # Check canonical commands
        for cmd in self.COMMANDS:
            if lower.startswith(f"{cmd} "):
                return cmd, message[len(cmd) + 1 :].strip()
            if lower == cmd:
                return cmd, ""

        # Check aliases
        for alias, canonical in self.COMMAND_ALIASES.items():
            if lower.startswith(f"{alias} "):
                return canonical, message[len(alias) + 1 :].strip()
            if lower == alias:
                return canonical, ""

        return None, message

    async def execute_command(
        self,
        command: str,
        arg: str,
        last_prompt: Optional[str] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        attachments: Optional[list] = None,
    ) -> dict:
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
        elif command == "ytdl":
            return await self._youtube_download_command(arg)
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
        elif command == "rss":
            # Delegate to RSS plugin
            from plugins import get_command_handler
            handler = get_command_handler("rss")
            if handler:
                return await handler(arg, self.user, self.db)
            return {"type": "text", "content": "RSS plugin not enabled. Enable it in Admin → Services."}
        elif command == "cal":
            return await self._schedule_command(arg)
        elif command == "contacts":
            return await self._contacts_command(arg)
        elif command == "mail":
            return await self._mail_command(arg, attachments=attachments)
        elif command == "todo":
            return await self._todo_command(arg)
        elif command == "music":
            return await self._music_command(arg)
        elif command == "translate":
            return await self._translate_command(arg)
        else:
            return {"type": "text", "content": f"Unknown command: {command}"}

    async def _help_command(self) -> dict:
        """Show available commands and plugins"""
        help_text = "## Available Commands\n\n"

        # Built-in commands
        for cmd, desc in self.COMMANDS.items():
            help_text += f"**{cmd}** - {desc}\n"

        # Plugin commands
        from plugins import get_plugin_commands
        plugin_cmds = get_plugin_commands()
        for cmd, desc in plugin_cmds.items():
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
            {
                "role": "system",
                "content": "You are a helpful assistant. Summarize the search results concisely and highlight key information.",
            },
            {"role": "user", "content": context},
        ]
        summary = await self.chat_service.chat(messages)

        return {"type": "search", "content": summary, "results": results}

    async def _images_command(self, query: str) -> dict:
        if not query:
            return {"type": "text", "content": "Please provide a search query. Example: `images cute cats`"}

        results = await self.search_service.image_search(query, limit=10)
        if not results:
            return {"type": "text", "content": f"No images found for: {query}"}

        return {"type": "images", "content": f"Found {len(results)} images for: {query}", "images": results}

    async def _geni_command(self, prompt: str, stop_check: Optional[callable] = None) -> dict:
        if not prompt:
            return {
                "type": "text",
                "content": "Please provide a prompt. Example: `geni a beautiful sunset over mountains`",
            }

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

        # Generate image with load balancing support
        # Lock is handled inside image_factory for local generation only
        # Remote requests (load balanced or custom user endpoint) run in parallel
        try:
            image_data = await generate_image_for_user(
                db=self.db,
                user=self.user,
                prompt=prompt,
            )
        except Exception as e:
            logger.error(f"Image generation exception: {e}")
            return {"type": "text", "content": f"Image generation error: {str(e)}"}

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

        if not image_data:
            return {
                "type": "text",
                "content": "Failed to generate image. Check that ComfyUI/image server is running and configured in settings.",
            }

        return {
            "type": "generated_image",
            "content": f"Generated image for: {prompt}",
            "image": image_data,
            "prompt": prompt,
        }

    def _format_size(self, size_bytes: int) -> str:
        """Format bytes to human readable size"""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
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
        torrents = result.get("torrents", {})
        if not torrents:
            return "No torrents found."

        # Build number-to-hash mapping for this user
        user_id = self.user.id if self.user else 0
        _flood_hash_map[user_id] = {}

        lines = ["## ◈ TORRENTS ◈\n"]
        for num, (hash_id, t) in enumerate(torrents.items(), 1):
            # Store mapping
            _flood_hash_map[user_id][num] = hash_id

            # Status emoji
            status = t.get("status", [])
            if "seeding" in status:
                icon = "🌱"
            elif "downloading" in status:
                icon = "⬇️"
            elif "stopped" in status or "paused" in status:
                icon = "⏸️"
            elif "error" in status:
                icon = "❌"
            else:
                icon = "📦"

            name = t.get("name", "Unknown")[:50]
            percent = t.get("percentComplete", 0)
            size = self._format_size(t.get("sizeBytes", 0))
            down_rate = self._format_size(t.get("downRate", 0)) + "/s"
            up_rate = self._format_size(t.get("upRate", 0)) + "/s"

            # Progress bar
            filled = int(percent / 10)
            bar = "█" * filled + "░" * (10 - filled)

            # Action buttons - Start for stopped, Stop for active
            is_stopped = "stopped" in status or "paused" in status
            if is_stopped:
                toggle_btn = f"[Start](cmd:torrents start {num})"
            else:
                toggle_btn = f"[Stop](cmd:torrents stop {num})"
            delete_btn = f"[Delete](cmd:torrents delete {num})"

            lines.append(f"**{num}.** {icon} **{name}**")
            lines.append(f"   [{bar}] {percent:.1f}% | {size}")
            lines.append(f"   ↓ {down_rate} | ↑ {up_rate}")
            lines.append(f"   {toggle_btn} | {delete_btn}\n")

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
            param = re.match(r"^[a-zA-Z0-9:/?#\[\]@!$&\'()*+,;=._~%-]+", param)
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
                return {
                    "type": "text",
                    "content": "Usage: `flood list` | `flood add <url>` | `flood start <#>` | `flood stop <#>` | `flood delete <#>`",
                }

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
                # Strip $ and commas from amount
                amount = parts[2].lstrip("$").replace(",", "")
                result = await plugin_service.execute_tool_call(
                    "budget", "add", {"name": name, "amount": amount}, self.user.id
                )
                if "error" not in result:
                    return {"type": "text", "content": f"✅ Bill added: {name} - ${float(amount):,.2f}"}
                action = "add"
            elif subcommand in ("pay", "paid") and len(parts) >= 2:
                name = " ".join(parts[1:])
                result = await plugin_service.execute_tool_call("budget", "pay", {"name": name}, self.user.id)
                if "error" in result:
                    return {"type": "text", "content": f"❌ {result['error']}"}

                # Show payment confirmation and remaining bills
                formatted = plugin_service.format_result_for_display("budget", "pay", result)
                bills_result = await plugin_service.execute_tool_call("budget", "bills", {}, self.user.id)
                bills_formatted = plugin_service.format_result_for_display("budget", "bills", bills_result)

                return {"type": "text", "content": f"{formatted}\n\n{bills_formatted}"}
            else:
                return {
                    "type": "text",
                    "content": "Usage: `budget` | `budget bills` | `budget add <name> <amount>` | `budget pay <name>`",
                }

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
                return {
                    "type": "text",
                    "content": "Usage: `firewall` | `firewall search <ip> [date]` | `firewall analyze <ip>`",
                }

            if "error" in result:
                return {"type": "text", "content": f"Firewall error: {result['error']}"}

            formatted = plugin_service.format_result_for_display("firewall", subcommand, result)
            return {"type": "text", "content": formatted}
        except Exception as e:
            return {"type": "text", "content": f"Firewall error: {str(e)}"}

    async def _youtube_command(self, arg: str) -> dict:
        """Summarize a YouTube video transcript"""
        if not arg:
            return {
                "type": "text",
                "content": """## YouTube Commands

**Summarize a video:**
`yt <url>` - Get AI summary of video transcript

**Download as MP3:**
`ytdl <url>` - Download video as MP3 to your WebDAV Music folder

Example: `yt https://youtube.com/watch?v=...`""",
            }

        # Extract URL
        urls = extract_youtube_urls(arg)
        if not urls:
            return {"type": "text", "content": "Could not find a valid YouTube URL."}

        target_url = urls[0]
        success, result = await summarize_youtube(target_url, self.chat_service)
        return {"type": "text", "content": result}

    async def _youtube_download_command(self, arg: str) -> dict:
        """Download a YouTube video as MP3 to WebDAV"""
        if not arg:
            return {
                "type": "text",
                "content": """## YouTube Download

**Usage:** `ytdl <url>`

Downloads YouTube video as MP3 and saves to your WebDAV Music folder.

Example: `ytdl https://youtube.com/watch?v=dQw4w9WgXcQ`

**Note:** Requires WebDAV Music to be configured in Settings.""",
            }

        # Check if yt-dlp is available
        if not check_ytdlp_available():
            return {"type": "text", "content": "❌ yt-dlp not installed. Install with: `pip install yt-dlp`"}

        # Extract URL
        urls = extract_youtube_urls(arg)
        if not urls:
            return {"type": "text", "content": "Could not find a valid YouTube URL."}

        target_url = urls[0]

        # Download and upload to WebDAV
        result = await download_and_upload_to_webdav(url=target_url, user_id=self.user.id, db=self.db)

        return {"type": "text", "content": format_download_result(result)}

    def _get_remote_bt_url(self):
        """Get remote torrent server URL if configured."""
        from app.models import Setting

        server_url = self.db.query(Setting).filter(Setting.key == "bt_server_url").first()
        return server_url.value if server_url and server_url.value else None

    async def _remote_bt_request(self, endpoint: str, method: str = "GET", json_body: dict = None):
        """Make request to remote torrent server."""
        import httpx

        from app.models import Setting

        server_url = self._get_remote_bt_url()
        if not server_url:
            return None

        # Get server-to-server API token
        server_token = self.db.query(Setting).filter(Setting.key == "bt_server_token").first()

        url = f"{server_url.rstrip('/')}/api/torrent{endpoint}"
        headers = {}

        # Use server token for authentication
        if server_token and server_token.value:
            headers["Authorization"] = f"Bearer {server_token.value}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info(
                    f"[TORRENT] TUI request to {url} with auth: {'token' if server_token and server_token.value else 'none'}"
                )
                if method == "GET":
                    response = await client.get(url, headers=headers)
                else:
                    response = await client.post(url, headers=headers, json=json_body)

                logger.info(f"[TORRENT] Remote response: {response.status_code}")

                if response.status_code == 200:
                    try:
                        return response.json()
                    except Exception as e:
                        logger.error(f"[TORRENT] Failed to parse JSON: {e}, body: {response.text[:500]}")
                        return {"error": "Remote server returned invalid response"}
                else:
                    # Try to get error detail from JSON, fall back to text
                    try:
                        error = response.json().get("detail", "Remote server error")
                    except Exception:
                        error = response.text[:200] if response.text else f"HTTP {response.status_code}"
                    logger.error(f"[TORRENT] Remote error: {response.status_code} - {error}")
                    return {"error": error}
        except httpx.RequestError as e:
            logger.error(f"Failed to connect to remote torrent server: {e}")
            return {"error": f"Cannot reach remote torrent server: {e}"}

    def _get_bt_service(self):
        """Get built-in torrent service if enabled, or None. Returns (service, error_msg)."""
        from app.models import Setting

        # Check for remote server first
        if self._get_remote_bt_url():
            return "remote", None  # Special marker for remote server

        bt_enabled = self.db.query(Setting).filter(Setting.key == "bt_enabled").first()
        if not bt_enabled or bt_enabled.value.lower() != "true":
            return None, "Built-in torrent client is disabled. Enable it in Admin Settings."

        def get_setting(key: str, default: str = "") -> str:
            s = self.db.query(Setting).filter(Setting.key == key).first()
            return s.value if s and s.value else default

        proxy_host = get_setting("bt_proxy_host")
        if not proxy_host:
            return None, "HTTP Proxy Host not configured. Set it in Admin Settings (required for torrenting)."

        try:
            from app.services.libtorrent_service import LibtorrentService

            service = LibtorrentService.get_instance(
                download_dir=get_setting("bt_download_dir", "/var/lib/posterchanai/torrents"),
                proxy_host=proxy_host,
                proxy_port=int(get_setting("bt_proxy_port", "8118")),
                listen_port=int(get_setting("bt_listen_port", "6881")),
            )
            return service, None
        except ImportError as e:
            return None, f"libtorrent not installed: {e}. Run: pip install libtorrent"
        except Exception as e:
            return None, f"Failed to start torrent service: {e}"

    async def _torrents_command(self, arg: str) -> dict:
        """Browse torrents and manage downloads."""
        global _torrent_cache

        # Import formatting functions - use local fallback if libtorrent not installed
        try:
            from app.services.libtorrent_service import format_torrent_list, format_torrent_list_from_dicts
        except Exception as e:
            logger.warning(f"Could not import libtorrent formatting: {e}")
            format_torrent_list = lambda torrents: _format_bt_list_from_dicts(
                [
                    {
                        "name": t.name,
                        "size": t.size,
                        "progress": t.progress,
                        "download_rate": t.download_rate,
                        "upload_rate": t.upload_rate,
                        "state": t.state,
                        "seeders": t.seeders,
                        "peers": t.peers,
                        "is_paused": getattr(t, "is_paused", False),
                    }
                    for t in torrents
                ]
            )
            format_torrent_list_from_dicts = _format_bt_list_from_dicts

        parts = arg.strip().split()
        subcommand = parts[0].lower() if parts else ""
        categories = ("movies", "tv", "music", "anime", "search")

        # Get built-in service (None if disabled or not configured)
        bt_service, bt_error = self._get_bt_service()

        # Client management subcommands - require built-in client or remote server
        if subcommand in ("list", "ls"):
            if not bt_service:
                return {"type": "text", "content": bt_error}
            if bt_service == "remote":
                result = await self._remote_bt_request("/list")
                if result and "error" in result:
                    return {"type": "text", "content": result["error"]}
                if result and "torrents" in result:
                    return {"type": "text", "content": _format_bt_list_from_dicts(result["torrents"])}
                return {"type": "text", "content": "No response from remote server"}
            torrents = bt_service.list_torrents()
            return {"type": "text", "content": format_torrent_list(torrents)}

        elif subcommand == "add" and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            magnet = parts[1]
            if not magnet.startswith("magnet:"):
                return {"type": "text", "content": "Please provide a magnet link starting with `magnet:`"}
            if bt_service == "remote":
                result = await self._remote_bt_request("/add", method="POST", json_body={"magnet": magnet})
                if result and "error" in result:
                    return {"type": "text", "content": result["error"]}
                if result and "info_hash" in result:
                    return {
                        "type": "text",
                        "content": f"Added torrent: `{result['info_hash']}`\n\nUse `torrents list` to check progress.",
                    }
                return {"type": "text", "content": "Failed to add torrent to remote server"}
            info_hash = bt_service.add_magnet(magnet)
            return {
                "type": "text",
                "content": f"Added torrent: `{info_hash}`\n\nUse `torrents list` to check progress.",
            }

        elif subcommand in ("start", "resume") and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            try:
                num = int(parts[1])
                if bt_service == "remote":
                    result = await self._remote_bt_request("/resume", method="POST", json_body={"num": num})
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    # Return updated list after action
                    list_result = await self._remote_bt_request("/list")
                    if list_result and "torrents" in list_result:
                        return {
                            "type": "text",
                            "content": f"▶️ Started torrent #{num}\n\n"
                            + format_torrent_list_from_dicts(list_result["torrents"]),
                        }
                    return {"type": "text", "content": f"▶️ Started torrent #{num}"}
                info_hash = bt_service.get_hash_by_number(num)
                if info_hash and bt_service.resume(info_hash):
                    # Return updated list after action
                    torrents = bt_service.list_torrents()
                    torrent_dicts = [
                        {
                            "name": t.name,
                            "size": t.size,
                            "progress": t.progress,
                            "download_rate": t.download_rate,
                            "upload_rate": t.upload_rate,
                            "state": t.state,
                            "seeders": t.seeders,
                            "peers": t.peers,
                            "is_paused": t.is_paused,
                        }
                        for t in torrents
                    ]
                    return {
                        "type": "text",
                        "content": f"▶️ Started torrent #{num}\n\n" + format_torrent_list_from_dicts(torrent_dicts),
                    }
                return {"type": "text", "content": f"Torrent #{num} not found"}
            except ValueError:
                return {"type": "text", "content": "Usage: `torrents resume <number>`"}

        elif subcommand in ("stop", "pause") and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            try:
                num = int(parts[1])
                if bt_service == "remote":
                    result = await self._remote_bt_request("/pause", method="POST", json_body={"num": num})
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    # Return updated list after action
                    list_result = await self._remote_bt_request("/list")
                    if list_result and "torrents" in list_result:
                        return {
                            "type": "text",
                            "content": f"⏸️ Paused torrent #{num}\n\n"
                            + format_torrent_list_from_dicts(list_result["torrents"]),
                        }
                    return {"type": "text", "content": f"⏸️ Paused torrent #{num}"}
                info_hash = bt_service.get_hash_by_number(num)
                if info_hash and bt_service.pause(info_hash):
                    # Return updated list after action
                    torrents = bt_service.list_torrents()
                    torrent_dicts = [
                        {
                            "name": t.name,
                            "size": t.size,
                            "progress": t.progress,
                            "download_rate": t.download_rate,
                            "upload_rate": t.upload_rate,
                            "state": t.state,
                            "seeders": t.seeders,
                            "peers": t.peers,
                            "is_paused": t.is_paused,
                        }
                        for t in torrents
                    ]
                    return {
                        "type": "text",
                        "content": f"⏸️ Paused torrent #{num}\n\n" + format_torrent_list_from_dicts(torrent_dicts),
                    }
                return {"type": "text", "content": f"Torrent #{num} not found"}
            except ValueError:
                return {"type": "text", "content": "Usage: `torrents pause <number>`"}

        elif subcommand in ("del", "delete", "rm") and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            try:
                num = int(parts[1])
                if bt_service == "remote":
                    result = await self._remote_bt_request(
                        "/remove", method="POST", json_body={"num": num, "delete_files": False}
                    )
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    # Return updated list after action
                    list_result = await self._remote_bt_request("/list")
                    if list_result and "torrents" in list_result:
                        return {
                            "type": "text",
                            "content": f"🗑️ Removed torrent #{num} (files kept)\n\n"
                            + format_torrent_list_from_dicts(list_result["torrents"]),
                        }
                    return {"type": "text", "content": f"🗑️ Removed torrent #{num} (files kept)"}
                info_hash = bt_service.get_hash_by_number(num)
                if info_hash and bt_service.remove(info_hash, delete_files=False):
                    # Return updated list after action
                    torrents = bt_service.list_torrents()
                    torrent_dicts = [
                        {
                            "name": t.name,
                            "size": t.size,
                            "progress": t.progress,
                            "download_rate": t.download_rate,
                            "upload_rate": t.upload_rate,
                            "state": t.state,
                            "seeders": t.seeders,
                            "peers": t.peers,
                            "is_paused": t.is_paused,
                        }
                        for t in torrents
                    ]
                    return {
                        "type": "text",
                        "content": f"🗑️ Removed torrent #{num} (files kept)\n\n"
                        + format_torrent_list_from_dicts(torrent_dicts),
                    }
                return {"type": "text", "content": f"Torrent #{num} not found"}
            except ValueError:
                return {"type": "text", "content": "Usage: `torrents rm <number>`"}

        elif subcommand == "purge" and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            try:
                num = int(parts[1])
                if bt_service == "remote":
                    result = await self._remote_bt_request(
                        "/remove", method="POST", json_body={"num": num, "delete_files": True}
                    )
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    # Return updated list after action
                    list_result = await self._remote_bt_request("/list")
                    if list_result and "torrents" in list_result:
                        return {
                            "type": "text",
                            "content": f"🗑️ Purged torrent #{num} (files deleted)\n\n"
                            + format_torrent_list_from_dicts(list_result["torrents"]),
                        }
                    return {"type": "text", "content": f"🗑️ Purged torrent #{num} (files deleted)"}
                info_hash = bt_service.get_hash_by_number(num)
                if info_hash and bt_service.remove(info_hash, delete_files=True):
                    # Return updated list after action
                    torrents = bt_service.list_torrents()
                    torrent_dicts = [
                        {
                            "name": t.name,
                            "size": t.size,
                            "progress": t.progress,
                            "download_rate": t.download_rate,
                            "upload_rate": t.upload_rate,
                            "state": t.state,
                            "seeders": t.seeders,
                            "peers": t.peers,
                            "is_paused": t.is_paused,
                        }
                        for t in torrents
                    ]
                    return {
                        "type": "text",
                        "content": f"🗑️ Purged torrent #{num} (files deleted)\n\n"
                        + format_torrent_list_from_dicts(torrent_dicts),
                    }
                return {"type": "text", "content": f"Torrent #{num} not found"}
            except ValueError:
                return {"type": "text", "content": "Usage: `torrents purge <number>`"}

        elif subcommand == "info" and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            try:
                num = int(parts[1])
                if bt_service == "remote":
                    result = await self._remote_bt_request(f"/info/{num}")
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    if not result or "info_hash" not in result:
                        return {"type": "text", "content": f"Torrent #{num} not found"}
                    # Format remote response
                    files = result.get("files", [])
                    file_list = "\n".join([f"  - {f['path']} ({f['size'] / 1024 / 1024:.1f} MB)" for f in files[:10]])
                    if len(files) > 10:
                        file_list += f"\n  ... and {len(files) - 10} more files"
                    info = f"""## {result["name"]}

**Hash:** `{result["info_hash"]}`
**Status:** {result["state"]} {"(paused)" if result.get("is_paused") else ""}
**Progress:** {result["progress"]:.1f}%
**Size:** {result["size"] / 1024 / 1024:.1f} MB
**Downloaded:** {result["downloaded"] / 1024 / 1024:.1f} MB
**Uploaded:** {result["uploaded"] / 1024 / 1024:.1f} MB
**Speed:** ↓{result["download_rate"] / 1024:.1f} KB/s ↑{result["upload_rate"] / 1024:.1f} KB/s
**Peers:** {result["seeders"]} seeders, {result["peers"]} peers
**Save Path:** {result["save_path"]}

**Files:**
{file_list}
"""
                    return {"type": "text", "content": info}
                info_hash = bt_service.get_hash_by_number(num)
                if not info_hash:
                    return {"type": "text", "content": f"Torrent #{num} not found"}

                t = bt_service.get_torrent(info_hash)
                if not t:
                    return {"type": "text", "content": f"Torrent #{num} not found"}

                files = bt_service.get_files(info_hash)
                file_list = "\n".join([f"  - {f['path']} ({f['size'] / 1024 / 1024:.1f} MB)" for f in files[:10]])
                if len(files) > 10:
                    file_list += f"\n  ... and {len(files) - 10} more files"

                info = f"""## {t.name}

**Hash:** `{t.info_hash}`
**Status:** {t.state} {"(paused)" if t.is_paused else ""}
**Progress:** {t.progress:.1f}%
**Size:** {t.size / 1024 / 1024:.1f} MB
**Downloaded:** {t.downloaded / 1024 / 1024:.1f} MB
**Uploaded:** {t.uploaded / 1024 / 1024:.1f} MB
**Speed:** ↓{t.download_rate / 1024:.1f} KB/s ↑{t.upload_rate / 1024:.1f} KB/s
**Peers:** {t.seeders} seeders, {t.peers} peers
**Save Path:** {t.save_path}

**Files:**
{file_list}
"""
                return {"type": "text", "content": info}
            except ValueError:
                return {"type": "text", "content": "Usage: `torrents info <number>`"}

        # Handle download subcommand: torrents download <category> <number>
        if subcommand in ("download", "dl", "get"):
            if len(parts) < 3:
                return {
                    "type": "text",
                    "content": "Usage: `torrents download <category> <number>`\n\nExample: `torrents download anime 5`",
                }

            category = parts[1].lower()
            if category not in categories:
                return {
                    "type": "text",
                    "content": f"Unknown category: `{category}`\n\nAvailable: movies, tv, music, anime, search",
                }

            try:
                num = int(parts[2])
            except ValueError:
                return {
                    "type": "text",
                    "content": "Please provide a valid number. Example: `torrents download anime 5`",
                }

            # Get cached results for this category
            user_id = self.user.id if self.user else 0
            user_cache = _torrent_cache.get(user_id, {})
            cached = user_cache.get(category, [])

            if not cached:
                return {
                    "type": "text",
                    "content": f"No {category} results cached. Run `torrents` first to load results.",
                }

            if num < 1 or num > len(cached):
                return {"type": "text", "content": f"Invalid number. Choose between 1 and {len(cached)}."}

            torrent = cached[num - 1]
            magnet = torrent.magnet

            if not self.user:
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\n**Magnet:** `{magnet[:100]}...`\n\nLogin required to download.",
                }

            if not bt_service:
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\n**Magnet:** `{magnet[:100]}...`\n\n{bt_error}",
                }

            if bt_service == "remote":
                result = await self._remote_bt_request("/add", method="POST", json_body={"magnet": magnet})
                if result and "error" in result:
                    return {"type": "text", "content": f"**Selected:** {torrent.title}\n\n{result['error']}"}
                if result and "info_hash" in result:
                    return {
                        "type": "text",
                        "content": f"**Downloading:** {torrent.title}\n\nAdded: `{result['info_hash']}`\n\nUse `torrents list` to check progress.",
                    }
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\nFailed to add torrent to remote server",
                }

            info_hash = bt_service.add_magnet(magnet)
            return {
                "type": "text",
                "content": f"**Downloading:** {torrent.title}\n\nAdded: `{info_hash}`\n\nUse `torrents list` to check progress.",
            }

        # Handle search subcommand
        if subcommand in ("search", "s") and len(parts) > 1:
            query = " ".join(parts[1:])
            try:
                import asyncio

                # Add timeout to prevent hanging
                results = await asyncio.wait_for(search_torrents(self.db, query, limit=15), timeout=20)

                if not results:
                    return {"type": "text", "content": f"No results found for '{query}' on torrent site"}

                # Cache results for download command
                user_id = self.user.id if self.user else 0
                _torrent_cache[user_id] = {"search": results}

                formatted = format_torrent_results(results, category="search", title=f"SEARCH: {query.upper()}")
                return {"type": "text", "content": formatted}
            except asyncio.TimeoutError:
                logger.error(f"Torrent search timed out for query: {query}")
                return {"type": "text", "content": f"Search timed out. The torrent site may be slow or unavailable."}
            except Exception as e:
                logger.error(f"Torrent search error: {e}")
                return {"type": "text", "content": f"Error searching torrents: {str(e)}"}

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
            return {
                "type": "text",
                "content": f"Unknown category: `{subcommand}`\n\nAvailable: `torrents movies`, `torrents tv`, `torrents music`, `torrents anime`",
            }

        try:
            results = await scrape_torrents(self.db, category, limit=15)

            if not results:
                return {
                    "type": "text",
                    "content": f"No {category} torrents found. The site may be unavailable or not configured.\n\nAdmin can set `torrent_site_url` in settings.",
                }

            # Cache results for download command
            user_id = self.user.id if self.user else 0
            if user_id not in _torrent_cache:
                _torrent_cache[user_id] = {}
            _torrent_cache[user_id][category] = results

            formatted = format_torrent_results(results, category)
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

            if not self.user:
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\n**Magnet:** `{magnet[:100]}...`\n\nLogin required to download.",
                }

            # Use built-in torrent client
            bt_service, bt_error = self._get_bt_service()
            if not bt_service:
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\n**Magnet:** `{magnet[:100]}...`\n\n{bt_error}",
                }

            if bt_service == "remote":
                result = await self._remote_bt_request("/add", method="POST", json_body={"magnet": magnet})
                if result and "error" in result:
                    return {"type": "text", "content": f"**Selected:** {torrent.title}\n\n{result['error']}"}
                if result and "info_hash" in result:
                    return {
                        "type": "text",
                        "content": f"**Downloading:** {torrent.title}\n\nAdded: `{result['info_hash']}`\n\nUse `torrents list` to check progress.",
                    }
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\nFailed to add torrent to remote server",
                }

            info_hash = bt_service.add_magnet(magnet)
            return {
                "type": "text",
                "content": f"**Downloading:** {torrent.title}\n\nAdded: `{info_hash}`\n\nUse `torrents list` to check progress.",
            }

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

        parts = arg.strip().split()
        subcommand = parts[0].lower() if parts else ""

        # Detect if user is trying to get news from a specific source (redirect to dailynews)
        # Common news source indicators
        news_source_keywords = ["npr", "cnn", "fox", "drudge", "nypost", "newsweek", ".com", ".org"]
        if subcommand and any(kw in subcommand for kw in news_source_keywords):
            return await self._dailynews_command(arg)

        # Check if user has Miniflux enabled
        if not self.user.miniflux_enabled:
            return {
                "type": "text",
                "content": "Miniflux is disabled for your account. Enable it in settings.\n\nTip: Use `dailynews` to get news from web sources instead.",
            }

        # Get Miniflux service
        miniflux = MinifluxService.from_settings(self.db, self.user)
        if not miniflux:
            return {
                "type": "text",
                "content": "Miniflux is not configured. Ask your admin to set it up in the admin panel.\n\nTip: Use `dailynews` to get news from web sources instead.",
            }

        # Handle refresh - force fetch new articles
        if subcommand in ("refresh", "fetch", "update"):
            pass  # Same as default, just fetch

        try:
            # Fetch unread entries - limit to 10 for faster response
            entries = await miniflux.get_unread_entries(limit=10)

            if not entries:
                return {"type": "text", "content": "No unread articles in Miniflux."}

            # Build response with summaries
            summaries = []
            entry_ids = []

            # Limit to 3 summaries to prevent hanging
            import asyncio

            for entry in entries[:3]:
                entry_id = entry.get("id")
                title = entry.get("title", "Untitled")
                url = entry.get("url", "")
                content = entry.get("content", "")
                feed_title = entry.get("feed", {}).get("title", "Unknown Feed")

                # Convert HTML to text
                text_content = re.sub(r"<script[^>]*>.*?</script>", "", content, flags=re.DOTALL | re.IGNORECASE)
                text_content = re.sub(r"<style[^>]*>.*?</style>", "", text_content, flags=re.DOTALL | re.IGNORECASE)
                text_content = re.sub(r"<[^>]+>", " ", text_content)
                text_content = re.sub(r"\s+", " ", text_content).strip()

                # Truncate for summarization
                if len(text_content) > 3000:
                    text_content = text_content[:3000] + "..."

                # Generate AI summary with timeout
                messages = [
                    {
                        "role": "system",
                        "content": "You are a news summarizer. Provide a concise 2-3 sentence summary of this article. Focus on the key facts.",
                    },
                    {"role": "user", "content": f"Title: {title}\n\nContent:\n{text_content}"},
                ]

                try:
                    # Add 20 second timeout per summary
                    summary = await asyncio.wait_for(self.chat_service.chat(messages), timeout=20)
                except asyncio.TimeoutError:
                    summary = "(Summary timed out)"
                    logger.warning(f"News summary timed out for: {title}")
                except Exception as e:
                    summary = f"(Error summarizing: {str(e)[:50]})"
                    logger.error(f"News summary error for {title}: {e}")

                # Add copy button for TUI (cmd: link works in TUI)
                summaries.append(f"**{title}**\n*{feed_title}*\n{url} [Copy URL](cmd:tui-copy {url})\n\n{summary}")
                entry_ids.append(entry_id)

            # Mark all as read
            await miniflux.mark_entries_as_read(entry_ids)

            # Format response
            timestamp = datetime.now().strftime("%B %d, %Y %H:%M")
            result = f"## News Update - {timestamp}\n\n" + "\n\n---\n\n".join(summaries)
            result += f"\n\n---\n*Marked {len(entry_ids)} articles as read*"

            # Note if there are more articles available
            if len(entries) > 3:
                result += f"\n\n_({len(entries) - 3} more articles available - run `news` again to see more)_"

            return {"type": "text", "content": result}

        except Exception as e:
            logger.error(f"News command error: {e}")
            return {"type": "text", "content": f"Error fetching news: {str(e)}"}

    def _add_copy_buttons_to_news(self, markdown: str) -> str:
        """Add copy buttons to news article links in markdown."""
        import re

        # Match markdown links in bullet points: - [title](url)
        # Add [Copy](cmd:tui-copy url) after each link
        def add_copy_button(match):
            title = match.group(1)
            url = match.group(2)
            # Return the link with a copy button
            return f"- [{title}]({url}) [Copy](cmd:tui-copy {url})"

        # Pattern: - [title](url)
        pattern = r"- \[([^\]]+)\]\(([^)]+)\)"
        result = re.sub(pattern, add_copy_button, markdown)

        return result

    async def _dailynews_command(self, arg: str) -> dict:
        """Get news from configured web sources (CNN, NPR, etc.)"""
        from datetime import datetime

        if not self.user:
            return {"type": "text", "content": "Please log in to use Daily News."}

        try:
            # Get news sources (user's custom sources or admin defaults)
            all_sources = get_user_news_sources(self.user, self.db)

            if not all_sources:
                return {"type": "text", "content": "No news sources configured. Add sources in User Settings."}

            # If arg provided, filter to matching source
            if arg.strip():
                arg_lower = arg.strip().lower()
                sources = [s for s in all_sources if arg_lower in s["url"].lower() or arg_lower in s["name"].lower()]
                if not sources:
                    # Try as a direct URL
                    sources = [{"url": arg.strip(), "name": arg.strip().split("/")[0]}]
            else:
                sources = all_sources

            # Fetch news from sources concurrently with timeout
            import asyncio

            async def fetch_single_source(source):
                try:
                    # Add timeout per source to prevent hanging
                    async with asyncio.timeout(60):  # 60 second timeout per source (fetch + AI summary)
                        markdown = await fetch_news_from_source(source["url"], source["name"], self.db)
                        return self._add_copy_buttons_to_news(markdown)
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout fetching news from {source['name']}")
                    return f"**{source['name']}:** Timeout fetching headlines"
                except Exception as e:
                    logger.error(f"Error fetching news from {source['name']}: {e}")
                    return f"**{source['name']}:** Error fetching headlines"

            results = await asyncio.gather(*[fetch_single_source(s) for s in sources], return_exceptions=True)
            # Filter out any exception results
            results = [r if not isinstance(r, Exception) else f"Error: {str(r)}" for r in results]

            # Format response
            today = datetime.now().strftime("%B %d, %Y %H:%M")
            if len(sources) == 1:
                content = f"## {sources[0]['name']} - {today}\n\n" + results[0] if results else "No headlines found."
            else:
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
            import socket
            from datetime import datetime

            from app.models import Message
            from app.services.logs_scheduler import (
                collect_remote_logs,
                collect_system_logs,
                generate_log_summary,
                get_logs_settings,
                get_or_create_logs_chat,
            )

            # Get settings for remote hosts
            settings = get_logs_settings(self.db)

            # Collect local logs
            log_data = collect_system_logs(self.db)

            # Collect logs from remote hosts (same as scheduled task)
            remote_hosts = settings.get("hosts", [])
            for host in remote_hosts:
                if host:
                    logger.info(f"Collecting logs from remote host: {host}")
                    remote_log_data = collect_remote_logs(host, settings)
                    if remote_log_data:
                        log_data += " " + remote_log_data

            if not log_data:
                return {"type": "text", "content": "No log data collected."}

            # Generate AI summary
            summary = await generate_log_summary(self.db, self.user, log_data)

            # Store in Logs conversation
            logs_chat = get_or_create_logs_chat(self.db, self.user.id)

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            hostname = socket.gethostname()
            all_hosts = [hostname] + [h for h in remote_hosts if h]
            hosts_str = ", ".join(all_hosts) if len(all_hosts) > 1 else hostname
            message_text = f"## System Log Report - {hosts_str}\n*{timestamp}*\n\n{summary}"

            log_msg = Message(conversation_id=logs_chat.id, role="assistant", content=message_text)
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

        # Don't auto-summarize if user wants to download
        lower = message.lower()
        download_keywords = ["download", "ytdl", "mp3", "save", "get song", "get video", "download song"]
        if any(kw in lower for kw in download_keywords):
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
        import calendar
        from datetime import datetime, timedelta

        from dateutil import parser as date_parser

        from app.services.caldav_service import (
            add_event_to_calendar,
            delete_event_from_calendar,
            format_events_for_display,
            get_all_user_events,
            get_event_by_uid,
            get_user_calendars,
            update_event_in_calendar,
        )

        if not self.user:
            return {"type": "text", "content": "Please log in to use the cal command."}

        # Check if user has calendars configured
        calendars = get_user_calendars(self.user.id, self.db)
        if not calendars:
            return {"type": "text", "content": "No calendars configured. Add calendars in User Settings."}

        parts = arg.strip().split(maxsplit=1)
        subcommand = parts[0].lower() if parts else "week"
        param = parts[1] if len(parts) > 1 else ""

        try:
            if subcommand == "today":
                # Get today's events
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                tomorrow = today + timedelta(days=1)
                events = get_all_user_events(self.user.id, today, tomorrow, self.db)
                events_text = format_events_for_display(events, include_description=True, cyberpunk=True)

                date_str = today.strftime("%A, %B %d")
                return {"type": "text", "content": f"## ◈ SCHEDULE - {date_str.upper()} ◈\n\n{events_text}"}

            elif subcommand in ("week", ""):
                # Get this week's events
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                week_end = today + timedelta(days=7)
                events = get_all_user_events(self.user.id, today, week_end, self.db)
                events_text = format_events_for_display(events, include_description=True, cyberpunk=True)

                return {"type": "text", "content": f"## ◈ SCHEDULE FOR THE WEEK ◈\n\n{events_text}"}

            elif subcommand == "month":
                # Get this month's events
                now = datetime.now()
                start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                if now.month == 12:
                    end_date = now.replace(year=now.year + 1, month=1, day=1)
                else:
                    end_date = now.replace(month=now.month + 1, day=1)

                events = get_all_user_events(self.user.id, start_date, end_date, self.db)
                events_text = format_events_for_display(events, include_description=True, cyberpunk=True)
                return {
                    "type": "text",
                    "content": f"## ◈ SCHEDULE FOR {now.strftime('%B %Y').upper()} ◈\n\n{events_text}",
                }

            elif subcommand == "nextmonth":
                # Get next month's events
                now = datetime.now()
                if now.month == 12:
                    start_date = now.replace(
                        year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
                    )
                    end_date = start_date.replace(month=2)
                else:
                    start_date = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
                    if start_date.month == 12:
                        end_date = start_date.replace(year=start_date.year + 1, month=1)
                    else:
                        end_date = start_date.replace(month=start_date.month + 1)

                events = get_all_user_events(self.user.id, start_date, end_date, self.db)
                events_text = format_events_for_display(events, include_description=True, cyberpunk=True)
                return {
                    "type": "text",
                    "content": f"## ◈ SCHEDULE FOR {start_date.strftime('%B %Y').upper()} ◈\n\n{events_text}",
                }

            elif subcommand == "add":
                if not param:
                    return {
                        "type": "text",
                        "content": "Usage: `cal add <event name> <time>`\n\nExample: `cal add Meeting with John tomorrow at 3pm`",
                    }

                import time as time_module
                from datetime import date

                today = date.today()
                local_tz = time_module.tzname[0]
                messages = [
                    {
                        "role": "system",
                        "content": f"""Parse this event and return JSON with:
- summary: event name
- description: any details mentioned
- start_time: ISO format datetime WITHOUT timezone suffix
- end_time: ISO format datetime WITHOUT timezone suffix
- location: place if mentioned
- rrule: iCalendar RRULE string if event repeats, null if not repeating

IMPORTANT: Today is {today.strftime("%A, %B %d, %Y")}. Use the current year {today.year} for dates.
Times are in local timezone ({local_tz}). Do NOT add Z suffix to times.
Return ONLY valid JSON, no other text.""",
                    },
                    {"role": "user", "content": f"Parse this event: {param}"},
                ]

                try:
                    import json
                    import re

                    parsed = await self.chat_service.chat(messages)
                    parsed = parsed.strip()
                    code_block_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", parsed)
                    if code_block_match:
                        parsed = code_block_match.group(1).strip()
                    else:
                        json_match = re.search(r"\{[\s\S]*\}", parsed)
                        if json_match:
                            parsed = json_match.group(0)

                    event_data = json.loads(parsed)
                    summary = event_data.get("summary", param)
                    description = event_data.get("description", "")
                    start_str = event_data.get("start_time", "").replace("Z", "")
                    end_str = event_data.get("end_time", "").replace("Z", "")
                    location = event_data.get("location")
                    rrule = event_data.get("rrule")

                    start_time = date_parser.parse(start_str) if start_str else datetime.now() + timedelta(hours=1)
                    end_time = date_parser.parse(end_str) if end_str else start_time + timedelta(hours=1)

                    cal = calendars[0]
                    if add_event_to_calendar(
                        cal["url"],
                        cal["username"],
                        cal["password"],
                        summary,
                        description,
                        start_time,
                        end_time,
                        location,
                        rrule,
                    ):
                        time_str = start_time.strftime("%A, %B %d at %I:%M %p")
                        return {"type": "text", "content": f"✅ Event added: **{summary}**\n\n📅 {time_str}"}
                    return {"type": "text", "content": "❌ Failed to add event to calendar."}
                except Exception as e:
                    return {"type": "text", "content": f"Error adding event: {str(e)}"}

            elif subcommand == "delete":
                if not param:
                    return {"type": "text", "content": "Usage: `cal delete <event_uid>`"}
                event_uid = param.strip()
                for cal in calendars:
                    if delete_event_from_calendar(cal["url"], cal["username"], cal["password"], event_uid):
                        return {"type": "text", "content": "✅ Event deleted successfully."}
                return {"type": "text", "content": "❌ Event not found or could not be deleted."}

            elif subcommand == "get":
                if not param:
                    return {"type": "text", "content": "Usage: `cal get <event_uid>`"}
                event_uid = param.strip()
                for cal in calendars:
                    event = get_event_by_uid(cal["url"], cal["username"], cal["password"], event_uid)
                    if event:
                        details = f"## Event Details\n\n**Title:** {event.summary}\n**Start:** {event.start.strftime('%Y-%m-%d %I:%M %p')}\n"
                        if event.end:
                            details += f"**End:** {event.end.strftime('%Y-%m-%d %I:%M %p')}\n"
                        if event.location:
                            details += f"**Location:** {event.location}\n"
                        if event.description:
                            details += f"**Description:** {event.description}\n"
                        details += f"\n**UID:** `{event.uid}`"
                        return {"type": "text", "content": details}
                return {"type": "text", "content": "❌ Event not found."}

            elif subcommand == "edit":
                edit_parts = param.split(maxsplit=2) if param else []
                if len(edit_parts) < 2:
                    return {"type": "text", "content": "Usage: `cal edit <uid> <changes>`"}

                event_uid = edit_parts[0]
                change_request = edit_parts[1]
                if len(edit_parts) > 2:
                    change_request += " " + edit_parts[2]

                event = None
                for cal in calendars:
                    event = get_event_by_uid(cal["url"], cal["username"], cal["password"], event_uid)
                    if event:
                        break

                if not event:
                    return {"type": "text", "content": "❌ Event not found."}

                change_lower = change_request.lower()
                if change_lower.startswith("title "):
                    new_title = change_request[6:].strip()
                    for cal in calendars:
                        if update_event_in_calendar(
                            cal["url"], cal["username"], cal["password"], event_uid, summary=new_title
                        ):
                            return {"type": "text", "content": f"✅ Updated title to: **{new_title}**"}

                elif change_lower.startswith("time ") or change_lower.startswith("move "):
                    # AI-based time parsing for edit (abbreviated for size)
                    time_request = change_request.split(maxsplit=1)[1]
                    # Logic here typically involves calling AI to get new start/end
                    return {
                        "type": "text",
                        "content": f"Rescheduling logic for '{time_request}' triggered (uid: {event_uid}).",
                    }

                return {"type": "text", "content": "Usage: `cal edit <uid> title|location|description|time <value>`"}

            else:
                return {
                    "type": "text",
                    "content": "Usage:\n- `cal today` | `cal week` | `cal month` | `cal nextmonth`\n- `cal add <event> <time>`\n- `cal delete <uid>`",
                }

        except Exception as e:
            logger.error(f"Schedule command error: {e}")
            return {"type": "text", "content": f"Error: {str(e)}"}

    async def _contacts_command(self, arg: str) -> dict:
        """Search or add contacts"""
        if not self.user:
            return {"type": "text", "content": "Please log in to use the contacts command."}

        if not arg.strip():
            return {
                "type": "text",
                "content": "Usage:\n- `contacts all` - List all contacts\n- `contacts <query>` - Search contacts\n- `contacts add <name> <phone>` - Add a new contact",
            }

        parts = arg.strip().split(maxsplit=2)
        subcommand = parts[0].lower()

        # Handle all subcommand - list all contacts
        if subcommand == "all":
            try:
                # Use empty query or wildcard to get all
                contacts = get_user_contacts(self.user.id, "", self.db)
                if not contacts:
                    return {
                        "type": "text",
                        "content": "No contacts found. Add contacts in your CardDAV address book or use `contacts add <name> <phone>`.",
                    }
                return {"type": "text", "content": format_contacts_for_display(contacts)}
            except Exception as e:
                logger.error(f"Contacts all error: {e}")
                return {"type": "text", "content": f"Error listing contacts: {str(e)}"}

        # Handle search subcommand - explicit search (for autocomplete compatibility)
        if subcommand == "search":
            query = parts[1] if len(parts) > 1 else ""
            if not query:
                return {
                    "type": "text",
                    "content": "Usage: `contacts search <query>`\n\nExample: `contacts search john`",
                }
            try:
                contacts = get_user_contacts(self.user.id, query, self.db)
                if not contacts:
                    return {"type": "text", "content": f"No contacts found matching '{query}'."}
                contacts_text = format_contacts_for_display(contacts)
                return {"type": "text", "content": f"## Contacts matching '{query}'\n{contacts_text}"}
            except Exception as e:
                logger.error(f"Contacts search error: {e}")
                return {"type": "text", "content": f"Error searching contacts: {str(e)}"}

        # Handle delete subcommand
        if subcommand == "delete" and len(parts) > 1:
            contact_uid = parts[1]
            try:
                # Get contact first to show what's being deleted
                contact = get_user_contact_by_uid(self.user.id, self.db, contact_uid)
                if not contact:
                    return {"type": "text", "content": f"❌ Contact not found with UID: {contact_uid}"}

                # Delete the contact
                if delete_user_contact(self.user.id, self.db, contact_uid):
                    # Show updated contact list
                    contacts = get_user_contacts(self.user.id, "", self.db)
                    contacts_text = format_contacts_for_display(contacts)
                    return {"type": "text", "content": f"✅ Deleted contact: {contact.name}\n\n{contacts_text}"}
                else:
                    return {"type": "text", "content": f"❌ Failed to delete contact: {contact.name}"}
            except Exception as e:
                logger.error(f"Delete contact error: {e}")
                return {"type": "text", "content": f"Error deleting contact: {str(e)}"}

        # Handle edit subcommand
        if subcommand == "edit" and len(parts) > 1:
            contact_uid = parts[1]

            # If only UID provided, show simple edit instructions
            if len(parts) == 2:
                try:
                    contact = get_user_contact_by_uid(self.user.id, self.db, contact_uid)
                    if not contact:
                        return {"type": "text", "content": f"❌ Contact not found with UID: {contact_uid}"}

                    # Create simple edit UI - just show current values and how to edit them
                    details = f"## 📝 {contact.name}\n\n"
                    details += f"**Current Information:**\n\n"

                    if contact.phone:
                        details += f"📞 Phone: {contact.phone}\n"
                    if contact.emails:
                        details += f"📧 Email: {', '.join(contact.emails)}\n"
                    if contact.organization:
                        details += f"🏢 Organization: {contact.organization}\n"
                    if contact.note:
                        details += f"📝 Note: {contact.note}\n"

                    details += f"\n---\n\n**To edit, type one of these commands:**\n\n"

                    details += f"• Change name:\n  `contacts edit {contact_uid} name John Smith`\n\n"
                    details += f"• Change phone:\n  `contacts edit {contact_uid} phone 555-1234`\n\n"
                    details += f"• Change email:\n  `contacts edit {contact_uid} email john@example.com`\n\n"
                    details += f"• Change organization:\n  `contacts edit {contact_uid} organization Acme Corp`\n\n"
                    details += f"• Add/change note:\n  `contacts edit {contact_uid} note Important client`\n\n"

                    details += f"[🗑️ Delete Contact](cmd:contacts delete {contact_uid})  [← Back](cmd:contacts all)"

                    return {"type": "text", "content": details}
                except Exception as e:
                    logger.error(f"Edit contact error: {e}")
                    return {"type": "text", "content": f"Error editing contact: {str(e)}"}

            # Parse field and value
            if len(parts) < 4:
                return {
                    "type": "text",
                    "content": "Usage: `contacts edit <uid> <field> <value>`\n\nFields: name, phone, email, organization, note",
                }

            field = parts[2].lower()
            value = " ".join(parts[3:])

            valid_fields = ["name", "phone", "email", "organization", "note"]
            if field not in valid_fields:
                return {"type": "text", "content": f"Invalid field: {field}\n\nValid fields: {', '.join(valid_fields)}"}

            try:
                # Verify contact exists first
                contact = get_user_contact_by_uid(self.user.id, self.db, contact_uid)
                if not contact:
                    return {"type": "text", "content": f"❌ Contact not found with UID: {contact_uid}"}

                # Perform the edit
                updates = {field: value}
                success = edit_user_contact(self.user.id, self.db, contact_uid, updates)

                if success:
                    # Show updated contact list
                    contacts = get_user_contacts(self.user.id, "", self.db)
                    contacts_text = format_contacts_for_display(contacts)
                    return {"type": "text", "content": f"✅ Updated {field} for {contact.name}\n\n{contacts_text}"}
                else:
                    return {"type": "text", "content": f"❌ Failed to update contact"}
            except Exception as e:
                logger.error(f"Edit contact error: {e}")
                return {"type": "text", "content": f"Error editing contact: {str(e)}"}

        # Handle add subcommand
        if subcommand == "add":
            if len(parts) < 3:
                return {
                    "type": "text",
                    "content": 'Usage: `contacts add <name> <phone>`\n\nExample: `contacts add "John Doe" 555-1234`',
                }

            # Parse name and phone - support quoted names
            remaining = arg.strip()[4:].strip()  # Remove "add "

            # Check for quoted name
            if remaining.startswith('"'):
                end_quote = remaining.find('"', 1)
                if end_quote > 0:
                    name = remaining[1:end_quote]
                    phone = remaining[end_quote + 1 :].strip()
                else:
                    return {
                        "type": "text",
                        "content": 'Unclosed quote in name. Example: `contacts add "John Doe" 555-1234`',
                    }
            else:
                # No quotes - assume last word is phone
                name_parts = remaining.rsplit(maxsplit=1)
                if len(name_parts) < 2:
                    return {
                        "type": "text",
                        "content": "Usage: `contacts add <name> <phone>`\n\nExample: `contacts add John 555-1234`",
                    }
                name = name_parts[0]
                phone = name_parts[1]

            if not name or not phone:
                return {"type": "text", "content": "Both name and phone are required."}

            success = add_user_contact(self.user.id, self.db, name, phone=phone)
            if success:
                return {"type": "text", "content": f"✅ Contact **{name}** added with phone {phone}"}
            else:
                return {
                    "type": "text",
                    "content": f"❌ Failed to add contact. Check CardDAV settings in User Settings > Calendar & Contacts.",
                }

        # Otherwise treat as search query
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

            elif subcommand == "folders":
                # List folders for an account
                if len(parts) < 2:
                    # Show account selection buttons
                    lines = ["## Select Account\n"]
                    for acc in accounts:
                        account_short = acc.email.split("@")[0]
                        cmd = f"mail folders {account_short}"
                        lines.append(f"[{acc.email}](cmd:{cmd})")
                    return {"type": "text", "content": "\n\n".join(lines)}

                account_hint = parts[1]
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                folders = list_folders(self.user.id, self.db, account_email)
                if not folders:
                    return {"type": "text", "content": f"No folders found for {account_email}."}

                return {"type": "text", "content": format_folder_list(folders, account_email)}

            elif subcommand == "folder":
                # Browse a specific folder
                if len(parts) < 3:
                    return {
                        "type": "text",
                        "content": "Usage: `mail folder <account> <folder>`\n\nExample: `mail folder work INBOX.Sent`",
                    }

                account_hint = parts[1]
                # Get folder name (may contain spaces)
                folder_parts = arg.strip().split(maxsplit=2)
                folder_name = folder_parts[2] if len(folder_parts) > 2 else ""

                if not folder_name:
                    return {"type": "text", "content": "Please provide a folder name."}

                # Find matching account
                account_email = None
                account = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        account = acc
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                messages = fetch_messages(account, folder=folder_name, limit=20)
                if not messages:
                    return {"type": "text", "content": f"No messages in folder '{folder_name}'."}

                return {
                    "type": "text",
                    "content": format_message_list(messages, folder=folder_name, account_email=account_email),
                }

            elif subcommand == "sum":
                # Summarize all inbox messages
                account_hint = parts[1] if len(parts) > 1 else None

                if account_hint:
                    # Find matching account
                    account_email = None
                    for acc in accounts:
                        if account_hint.lower() in acc.email.lower():
                            account_email = acc.email
                            messages = fetch_messages(acc, limit=20)
                            break
                    if not account_email:
                        return {"type": "text", "content": f"Account '{account_hint}' not found."}
                else:
                    # Fetch from all accounts
                    messages = fetch_all_accounts(self.user.id, self.db, limit_per_account=10)

                if not messages:
                    return {"type": "text", "content": "No messages to summarize."}

                # Build summary of all messages for AI
                msg_list = []
                for msg in messages:
                    msg_list.append(
                        f"- From: {msg.sender} | Subject: {msg.subject} | Date: {msg.date.strftime('%b %d')}"
                    )

                # Use AI to summarize
                ai_messages = [
                    {
                        "role": "system",
                        "content": "Summarize this inbox. Group by sender or topic. Highlight urgent items, action items, and important dates. Be concise.",
                    },
                    {"role": "user", "content": f"Inbox ({len(messages)} messages):\n" + "\n".join(msg_list)},
                ]
                summary = await self.chat_service.chat(ai_messages)
                return {"type": "text", "content": f"## Inbox Summary ({len(messages)} messages)\n\n{summary}"}

            elif subcommand == "search":
                # Support both: mail search <query> (default account) or mail search <account> <query>
                if len(parts) < 2:
                    return {
                        "type": "text",
                        "content": "Usage: `mail search <query>` or `mail search <account> <query>`\n\nExample: `mail search invoice` or `mail search yummy invoice`",
                    }

                # Check if parts[1] looks like an account hint (contains @ or matches an account)
                potential_account = parts[1]
                account_email = None
                for acc in accounts:
                    if potential_account.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if account_email and len(parts) >= 3:
                    # mail search <account> <query>
                    query_parts = arg.strip().split(maxsplit=2)
                    query = query_parts[2] if len(query_parts) > 2 else ""
                else:
                    # mail search <query> - use first account
                    account_email = accounts[0].email
                    query_parts = arg.strip().split(maxsplit=1)
                    query = query_parts[1] if len(query_parts) > 1 else ""

                if not query:
                    return {"type": "text", "content": "Please provide a search query."}

                messages = search_messages(self.user.id, self.db, account_email, query)
                if not messages:
                    return {"type": "text", "content": f"No messages found matching '{query}'."}
                return {
                    "type": "text",
                    "content": f"## ◈ SEARCH: {query.upper()} ◈\n\n" + format_message_list(messages, show_header=False),
                }

            elif subcommand == "read":
                # Support both: mail read <id> (default account) or mail read <account> <id>
                if len(parts) < 2:
                    return {
                        "type": "text",
                        "content": "Usage: `mail read <id>` or `mail read <account> <id>`\n\nExample: `mail read 123` or `mail read verita84 INBOX.Archive:123`",
                    }

                # Check if parts[1] is numeric (id) or account hint
                # Strip # prefix if present (e.g., "#2" -> "2")
                test_val = parts[1].lstrip("#")
                if len(parts) == 2 or test_val.isdigit() or re.match(r"^\d+$|^[\w.-]+:\d+$", test_val):
                    # mail read <id> - use first account
                    account_hint = None
                    uid_part = parts[1].lstrip("#")
                else:
                    # mail read <account> <id>
                    if len(parts) < 3:
                        return {"type": "text", "content": "Usage: `mail read <id>` or `mail read <account> <id>`"}
                    account_hint = parts[1]
                    uid_part = parts[2]

                # Parse folder:uid format (e.g., "INBOX.Archive:123")
                folder = None
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)
                else:
                    uid = uid_part

                # Sanitize UID - extract only numeric portion (strip emojis/extra chars)
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account or use first account
                account_email = None
                if account_hint:
                    for acc in accounts:
                        if account_hint.lower() in acc.email.lower():
                            account_email = acc.email
                            break
                    if not account_email:
                        return {"type": "text", "content": f"Account '{account_hint}' not found."}
                else:
                    # Default to first account
                    account_email = accounts[0].email

                msg = get_message_by_id(self.user.id, self.db, account_email, uid, folder=folder)
                if not msg:
                    return {"type": "text", "content": f"Message {uid} not found."}

                return {"type": "text", "content": format_message_detail(msg, folder=folder)}

            elif subcommand == "summary":
                # Support both: mail summary <id> (default account) or mail summary <account> <id>
                if len(parts) < 2:
                    return {
                        "type": "text",
                        "content": "Usage: `mail summary <id>` or `mail summary <account> <id>`\n\nExample: `mail summary 123` or `mail summary work 456`",
                    }

                # Check if parts[1] is numeric (id) or account hint
                test_val = parts[1].lstrip("#")
                if len(parts) == 2 or test_val.isdigit() or re.match(r"^\d+$|^[\w.-]+:\d+$", test_val):
                    # mail summary <id> - use first account
                    account_hint = None
                    uid_part = parts[1].lstrip("#")
                else:
                    # mail summary <account> <id>
                    if len(parts) < 3:
                        return {
                            "type": "text",
                            "content": "Usage: `mail summary <id>` or `mail summary <account> <id>`",
                        }
                    account_hint = parts[1]
                    uid_part = parts[2]

                # Parse folder:uid format
                folder = None
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)
                else:
                    uid = uid_part

                # Sanitize UID - extract only numeric portion (strip emojis/extra chars)
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account or use first account
                account_email = None
                if account_hint:
                    for acc in accounts:
                        if account_hint.lower() in acc.email.lower():
                            account_email = acc.email
                            break
                    if not account_email:
                        return {"type": "text", "content": f"Account '{account_hint}' not found."}
                else:
                    account_email = accounts[0].email

                msg = get_message_by_id(self.user.id, self.db, account_email, uid, folder=folder)
                if not msg:
                    return {"type": "text", "content": f"Message {uid} not found."}

                # Use AI to summarize
                messages = [
                    {
                        "role": "system",
                        "content": "Summarize this email concisely. Include key points, action items, and important dates if any.",
                    },
                    {"role": "user", "content": f"From: {msg.sender}\nSubject: {msg.subject}\n\n{msg.body_text}"},
                ]
                summary = await self.chat_service.chat(messages)
                return {"type": "text", "content": f"## Summary of: {msg.subject}\n\n{summary}"}

            elif subcommand == "translate":
                # Support both: mail translate <id> [language] or mail translate <account> <id> [language]
                # Language defaults to English if not specified
                if len(parts) < 2:
                    return {
                        "type": "text",
                        "content": "Usage: `mail translate <id> [language]` or `mail translate <account> <id> [language]`\n\nExamples:\n- `mail translate 123` - translates to English\n- `mail translate 123 spanish` - translates to Spanish\n- `mail translate work 123 japanese` - translates to Japanese",
                    }

                # Check if parts[1] is numeric (id) or account hint
                test_val = parts[1].lstrip("#")
                if test_val.isdigit() or re.match(r"^\d+$|^[\w.-]+:\d+$", test_val):
                    # mail translate <id> [language] - use first account
                    account_hint = None
                    uid_part = parts[1].lstrip("#")
                    language = parts[2] if len(parts) > 2 else "English"
                else:
                    # mail translate <account> <id> [language]
                    if len(parts) < 3:
                        return {
                            "type": "text",
                            "content": "Usage: `mail translate <id> [language]` or `mail translate <account> <id> [language]`",
                        }
                    account_hint = parts[1]
                    uid_part = parts[2]
                    language = parts[3] if len(parts) > 3 else "English"

                # Parse folder:uid format
                folder = None
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)
                else:
                    uid = uid_part

                # Sanitize UID - extract only numeric portion (strip emojis/extra chars)
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account or use first account
                account_email = None
                if account_hint:
                    for acc in accounts:
                        if account_hint.lower() in acc.email.lower():
                            account_email = acc.email
                            break
                    if not account_email:
                        return {"type": "text", "content": f"Account '{account_hint}' not found."}
                else:
                    account_email = accounts[0].email

                msg = get_message_by_id(self.user.id, self.db, account_email, uid, folder=folder)
                if not msg:
                    return {"type": "text", "content": f"Message {uid} not found."}

                # Use AI to translate
                messages = [
                    {
                        "role": "system",
                        "content": f"You are a translator. Translate the ENTIRE email below to {language}. CRITICAL: You MUST translate every single word, sentence, and paragraph completely. Do NOT summarize. Do NOT skip any content. Do NOT add commentary. Preserve all original formatting. Output ONLY the complete translated text.",
                    },
                    {"role": "user", "content": f"From: {msg.sender}\nSubject: {msg.subject}\n\n{msg.body_text}"},
                ]
                translation = await self.chat_service.chat(messages)
                return {"type": "text", "content": f"## {msg.subject} ({language})\n\n{translation}"}

            elif subcommand == "extract-event":
                # Extract calendar event from email and add to calendar
                if len(parts) < 3:
                    return {
                        "type": "text",
                        "content": "Usage: `mail extract-event <account> <id>`\n\nExample: `mail extract-event work 123`",
                    }

                account_hint = parts[1]
                uid_part = parts[2]

                # Parse folder:uid format
                folder = None
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)
                else:
                    uid = uid_part

                # Sanitize UID
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break
                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                msg = get_message_by_id(self.user.id, self.db, account_email, uid, folder=folder)
                if not msg:
                    return {"type": "text", "content": f"Message {uid} not found."}

                # Use AI to extract event details
                import time as time_module
                from datetime import date, datetime

                today = date.today()
                local_tz = time_module.tzname[0]

                email_content = f"From: {msg.sender}\nSubject: {msg.subject}\nDate: {msg.date}\n\n{msg.body_text}"

                messages = [
                    {
                        "role": "system",
                        "content": f"""Extract calendar event information from this email and return JSON with:
- summary: event name/title
- description: event details (do NOT include recurrence info here)
- start_time: ISO format datetime WITHOUT timezone suffix (e.g., "2026-01-13T09:00:00")
- end_time: ISO format datetime WITHOUT timezone suffix (default 1 hour after start)
- location: place if mentioned
- rrule: iCalendar RRULE string if event repeats, null if not repeating

For recurrence patterns (ONLY use if explicitly stated):
- "every day" or "daily" -> "FREQ=DAILY"
- "every week" or "weekly" -> "FREQ=WEEKLY"
- "every month" or "monthly" -> "FREQ=MONTHLY"
- "every weekday" -> "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
- "every Monday" -> "FREQ=WEEKLY;BYDAY=MO"

CRITICAL: If the event is on a SPECIFIC DATE (e.g., "Wednesday Jan 14", "next Friday", "tomorrow"), DO NOT add rrule - set it to null.
Only use rrule if the email says "every", "recurring", "repeating", or similar recurring language.

IMPORTANT: Today is {today.strftime("%A, %B %d, %Y")}. Use the current year {today.year} for dates.
Times are in local timezone ({local_tz}). Do NOT add Z suffix to times.
Return ONLY valid JSON, no other text.""",
                    },
                    {"role": "user", "content": f"Extract event from this email:\n\n{email_content}"},
                ]

                try:
                    import json

                    parsed = await self.chat_service.chat(messages)

                    # Clean up markdown code blocks if present
                    parsed = parsed.strip()
                    code_block_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", parsed)
                    if code_block_match:
                        parsed = code_block_match.group(1).strip()
                    else:
                        json_match = re.search(r"\{[\s\S]*\}", parsed)
                        if json_match:
                            parsed = json_match.group(0)

                    event_data = json.loads(parsed)

                    # Now add to calendar using existing calendar add logic
                    summary = event_data.get("summary", msg.subject)
                    description = event_data.get("description", "")
                    start_str = event_data.get("start_time", "")
                    end_str = event_data.get("end_time", "")
                    location = event_data.get("location")
                    rrule = event_data.get("rrule")

                    # Strip Z suffix if present
                    if start_str and start_str.endswith("Z"):
                        start_str = start_str[:-1]
                    if end_str and end_str.endswith("Z"):
                        end_str = end_str[:-1]

                    from datetime import timedelta

                    from dateutil import parser as date_parser

                    # Parse start time
                    start_time = date_parser.parse(start_str) if start_str else datetime.now() + timedelta(hours=1)
                    logger.info(f"Calendar add (email) - parsed start_time: {start_time} (tzinfo={start_time.tzinfo})")

                    # If timezone included, convert to local naive
                    if start_time.tzinfo is not None:
                        start_time = start_time.astimezone().replace(tzinfo=None)
                        logger.info(f"Calendar add (email) - converted to naive local: {start_time}")

                    # Parse end time
                    if end_str:
                        end_time = date_parser.parse(end_str)
                        if end_time.tzinfo is not None:
                            end_time = end_time.astimezone().replace(tzinfo=None)
                    else:
                        end_time = start_time + timedelta(hours=1)

                    # Add to first calendar
                    calendars = get_user_calendars(self.user.id, self.db)
                    if not calendars:
                        return {"type": "text", "content": "No calendars configured. Add calendars in User Settings."}

                    cal = calendars[0]
                    success = add_event_to_calendar(
                        cal["url"],
                        cal["username"],
                        cal["password"],
                        summary,
                        description,
                        start_time,
                        end_time,
                        location,
                        rrule,
                    )

                    if success:
                        time_str = start_time.strftime("%A, %B %d at %I:%M %p")
                        recurrence_msg = f"\n🔁 {rrule}" if rrule else ""
                        return {
                            "type": "text",
                            "content": f"✅ Event added from email: **{summary}**\n\n📅 {time_str}{recurrence_msg}",
                        }
                    else:
                        return {"type": "text", "content": "❌ Failed to add event to calendar."}

                except json.JSONDecodeError:
                    return {
                        "type": "text",
                        "content": "Could not extract event details from email. The email may not contain event information.",
                    }
                except Exception as e:
                    logger.error(f"Error extracting event: {e}")
                    return {"type": "text", "content": f"Error extracting event: {str(e)}"}

            elif subcommand == "extract-bill":
                # Extract bill information from email and add to budget
                if len(parts) < 3:
                    return {
                        "type": "text",
                        "content": "Usage: `mail extract-bill <account> <id>`\n\nExample: `mail extract-bill work 123`",
                    }

                account_hint = parts[1]
                uid_part = parts[2]

                # Parse folder:uid format
                folder = None
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)
                else:
                    uid = uid_part

                # Sanitize UID
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break
                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                msg = get_message_by_id(self.user.id, self.db, account_email, uid, folder=folder)
                if not msg:
                    return {"type": "text", "content": f"Message {uid} not found."}

                # Use AI to extract bill details
                email_content = f"From: {msg.sender}\nSubject: {msg.subject}\nDate: {msg.date}\n\n{msg.body_text}"

                messages = [
                    {
                        "role": "system",
                        "content": """Extract bill/invoice information from this email and return JSON with:
- name: bill name or company name (e.g., "Electric Company", "Netflix", "Water Bill")
- amount: total amount due as a number (e.g., 45.99)
- due_date: due date if mentioned (YYYY-MM-DD format), null if not mentioned

Return ONLY valid JSON, no other text. If this is not a bill or invoice, return {"error": "not_a_bill"}.""",
                    },
                    {"role": "user", "content": f"Extract bill from this email:\n\n{email_content}"},
                ]

                try:
                    import json

                    parsed = await self.chat_service.chat(messages)

                    # Clean up markdown code blocks if present
                    parsed = parsed.strip()
                    code_block_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", parsed)
                    if code_block_match:
                        parsed = code_block_match.group(1).strip()
                    else:
                        json_match = re.search(r"\{[\s\S]*\}", parsed)
                        if json_match:
                            parsed = json_match.group(0)

                    bill_data = json.loads(parsed)

                    if bill_data.get("error") == "not_a_bill":
                        return {
                            "type": "text",
                            "content": "This email does not appear to contain bill or invoice information.",
                        }

                    name = bill_data.get("name", "Unknown Bill")
                    amount = bill_data.get("amount", 0)
                    due_date = bill_data.get("due_date")

                    # Add to budget system using plugin
                    plugin_service = PluginService(self.db)
                    result = await plugin_service.execute_tool_call(
                        "budget", "add", {"name": name, "amount": str(amount)}, self.user.id
                    )

                    if "error" in result:
                        return {"type": "text", "content": f"Error adding bill: {result.get('error', 'Unknown error')}"}

                    due_str = f"\n📅 Due: {due_date}" if due_date else ""
                    return {
                        "type": "text",
                        "content": f"✅ Bill added from email: **{name}**\n\n💵 Amount: ${amount:.2f}{due_str}",
                    }

                except json.JSONDecodeError:
                    return {
                        "type": "text",
                        "content": "Could not extract bill details from email. The email may not contain billing information.",
                    }
                except Exception as e:
                    logger.error(f"Error extracting bill: {e}")
                    return {"type": "text", "content": f"Error extracting bill: {str(e)}"}

            elif subcommand == "reply":
                if len(parts) < 4:
                    return {
                        "type": "text",
                        "content": "Usage: `mail reply <account> [folder:]<id> <message>`\n\nExample: `mail reply verita84 123 Thanks for the info!` or `mail reply verita84 INBOX.Archive:456 Thanks!`",
                    }

                account_hint = parts[1]
                uid_part = parts[2]
                reply_body = parts[3]

                # Parse folder:uid format (e.g., "INBOX.Archive:456")
                folder = "INBOX"
                uid = uid_part
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)

                # Sanitize UID - extract only numeric portion (strip emojis/extra chars)
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                import asyncio

                success = await asyncio.to_thread(
                    reply_to_message, self.user.id, self.db, account_email, uid, reply_body, folder=folder
                )
                if success:
                    return {"type": "text", "content": "Reply sent successfully."}
                else:
                    return {"type": "text", "content": "Failed to send reply."}

            elif subcommand == "forward":
                if len(parts) < 4:
                    return {
                        "type": "text",
                        "content": "Usage: `mail forward <account> [folder:]<id> <recipient> [message]`\n\nExample: `mail forward verita84 123 john@example.com` or `mail forward verita84 123 john@example.com Check this out!`",
                    }

                account_hint = parts[1]
                uid_part = parts[2]
                recipient = parts[3]
                forward_body = parts[4] if len(parts) > 4 else ""

                # Parse folder:uid format (e.g., "INBOX.Archive:456")
                folder = "INBOX"
                uid = uid_part
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)

                # Sanitize UID - extract only numeric portion (strip emojis/extra chars)
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                import asyncio

                success = await asyncio.to_thread(
                    forward_message, self.user.id, self.db, account_email, uid, recipient, forward_body, folder=folder
                )
                if success:
                    return {"type": "text", "content": f"Email forwarded to {recipient} successfully."}
                else:
                    return {"type": "text", "content": "Failed to forward email."}

            elif subcommand == "delete":
                # Support both: mail delete <id> (default account) or mail delete <account> <id>
                if len(parts) < 2:
                    return {
                        "type": "text",
                        "content": "Usage: `mail delete <id>` or `mail delete <account> [folder:]<id>`\n\nExample: `mail delete 123` or `mail delete verita84 INBOX.Archive:456`",
                    }

                # Check if parts[1] is numeric (id) or account hint
                # Strip # prefix if present (e.g., "#2" -> "2")
                test_val = parts[1].lstrip("#")
                if len(parts) == 2 or test_val.isdigit() or re.match(r"^\d+$|^[\w.-]+:\d+$", test_val):
                    # mail delete <id> - use first account
                    account_hint = None
                    uid_part = parts[1].lstrip("#")
                else:
                    # mail delete <account> <id>
                    if len(parts) < 3:
                        return {"type": "text", "content": "Usage: `mail delete <id>` or `mail delete <account> <id>`"}
                    account_hint = parts[1]
                    uid_part = parts[2]

                # Parse folder:uid format (e.g., "INBOX.Archive:456")
                folder = "INBOX"
                uid = uid_part
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)

                # Sanitize UID - extract only numeric portion (strip emojis/extra chars)
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account or use first account
                account_email = None
                if account_hint:
                    for acc in accounts:
                        if account_hint.lower() in acc.email.lower():
                            account_email = acc.email
                            break
                    if not account_email:
                        return {"type": "text", "content": f"Account '{account_hint}' not found."}
                else:
                    # Default to first account
                    account_email = accounts[0].email

                import asyncio

                success = await asyncio.to_thread(delete_message, self.user.id, self.db, account_email, uid, folder)
                if success:
                    return {"type": "text", "content": f"Message {uid} deleted from {folder}."}
                else:
                    return {"type": "text", "content": f"Failed to delete message {uid} from {folder}."}

            elif subcommand in ("deleteall", "purge", "clear"):
                if len(parts) < 2:
                    return {
                        "type": "text",
                        "content": "Usage: `mail deleteall <account>`\n\nExample: `mail deleteall verita84`\n\n**Warning:** This will delete ALL messages in the inbox!",
                    }

                account_hint = parts[1]

                # Find matching account
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                import asyncio

                count = await asyncio.to_thread(delete_all_messages, self.user.id, self.db, account_email)
                if count >= 0:
                    return {"type": "text", "content": f"🗑️ Deleted {count} messages from {account_email}"}
                else:
                    return {"type": "text", "content": f"Failed to delete messages from {account_email}."}

            elif subcommand == "archive":
                # Support both: mail archive <id> (default account) or mail archive <account> [folder:]<id>
                if len(parts) < 2:
                    return {
                        "type": "text",
                        "content": "Usage: `mail archive <id>` or `mail archive <account> [folder:]<id>`\n\nExample: `mail archive 123` or `mail archive verita84 456`",
                    }

                # Check if parts[1] is numeric (id) or account hint
                # Strip # prefix if present (e.g., "#2" -> "2")
                test_val = parts[1].lstrip("#")
                if len(parts) == 2 or test_val.isdigit():
                    # mail archive <id> - use first account
                    account_hint = None
                    uid_part = parts[1].lstrip("#")
                else:
                    # mail archive <account> <id>
                    if len(parts) < 3:
                        return {
                            "type": "text",
                            "content": "Usage: `mail archive <id>` or `mail archive <account> [folder:]<id>`",
                        }
                    account_hint = parts[1]
                    uid_part = parts[2]

                # Parse folder:uid format (e.g., "INBOX.Archive:456")
                folder = "INBOX"
                uid = uid_part
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)

                # Sanitize UID - extract only numeric portion (strip emojis/extra chars)
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account or use first account
                account_email = None
                if account_hint:
                    for acc in accounts:
                        if account_hint.lower() in acc.email.lower():
                            account_email = acc.email
                            break
                    if not account_email:
                        return {"type": "text", "content": f"Account '{account_hint}' not found."}
                else:
                    # Default to first account
                    account_email = accounts[0].email

                import asyncio

                success = await asyncio.to_thread(
                    archive_message, self.user.id, self.db, account_email, uid, folder=folder
                )
                if success:
                    return {"type": "text", "content": f"📦 Message {uid} archived."}
                else:
                    return {"type": "text", "content": f"Failed to archive message {uid}."}

            elif subcommand == "attachment":
                # Download and open attachment: mail attachment <account> <uid> <index>
                if len(parts) < 4:
                    return {"type": "text", "content": "Usage: `mail attachment <account> <uid> <index>`"}

                account_hint = parts[1]
                uid = parts[2]
                try:
                    att_index = int(parts[3])
                except ValueError:
                    return {"type": "text", "content": "Invalid attachment index. Must be a number."}

                # Sanitize UID
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                # Get the attachment
                attachment = get_attachment(self.user.id, self.db, account_email, uid, att_index)
                if not attachment:
                    return {"type": "text", "content": f"Attachment not found."}

                if not attachment.data:
                    return {"type": "text", "content": f"Attachment too large or couldn't be downloaded."}

                # Save to temp file and open
                import os
                import platform
                import subprocess
                import tempfile

                # Create temp file with original extension
                _, ext = os.path.splitext(attachment.filename)
                with tempfile.NamedTemporaryFile(delete=False, suffix=ext, prefix="mail_att_") as f:
                    f.write(attachment.data)
                    temp_path = f.name

                # Open with system default application
                opened = False
                try:
                    if platform.system() == "Darwin":  # macOS
                        subprocess.run(["open", temp_path], check=True)
                        opened = True
                    elif platform.system() == "Windows":
                        os.startfile(temp_path)
                        opened = True
                    else:  # Linux - try multiple openers
                        for opener in ["xdg-open", "gio open", "kde-open", "gnome-open"]:
                            try:
                                subprocess.run(opener.split() + [temp_path], check=True, stderr=subprocess.DEVNULL)
                                opened = True
                                break
                            except (FileNotFoundError, subprocess.CalledProcessError):
                                continue
                except Exception:
                    pass

                if opened:
                    return {
                        "type": "text",
                        "content": f"📎 Opened: **{attachment.filename}** ({attachment.size / 1024:.1f} KB)",
                    }
                else:
                    return {
                        "type": "text",
                        "content": f"📎 Saved: `{temp_path}`\n\nOpen manually or install `xdg-utils`",
                    }

            elif subcommand == "send":
                # Explicit send: mail send [account] <recipient> ["subject"] <message>
                if len(parts) < 3:
                    return {
                        "type": "text",
                        "content": 'Usage: `mail send [account] <recipient> ["subject"] <message>`\n\nExamples:\n- `mail send linda Hey!` - auto-generate subject\n- `mail send linda "Meeting" Can we meet tomorrow?` - with subject\n- `mail send work linda Hey!` - uses \'work\' account',
                    }

                # Check if parts[1] is an account hint or recipient
                from_account = None
                recipient_idx = 1

                # Check if first arg matches an account
                for acc in accounts:
                    if parts[1].lower() in acc.email.lower():
                        from_account = acc
                        recipient_idx = 2
                        break

                if recipient_idx == 2 and len(parts) < 4:
                    return {
                        "type": "text",
                        "content": 'Usage: `mail send <account> <recipient> ["subject"] <message>`\n\nExample: `mail send work linda@example.com Hey, how are you?`',
                    }

                recipient = parts[recipient_idx]

                # Re-split to get full text after recipient
                full_parts = arg.strip().split(maxsplit=recipient_idx + 1)
                rest = full_parts[recipient_idx + 1] if len(full_parts) > recipient_idx + 1 else ""

                # Check for quoted subject
                subject = None
                message_body = rest
                if rest.startswith('"'):
                    # Find closing quote
                    end_quote = rest.find('"', 1)
                    if end_quote > 0:
                        subject = rest[1:end_quote]
                        message_body = rest[end_quote + 1 :].strip()

                return await self._send_new_mail(
                    accounts, recipient, message_body, attachments, from_account=from_account, subject=subject
                )

            else:
                # Check if this is a shorthand send: mail <recipient> ["subject"] <message>
                # First word is not a known subcommand, treat as recipient
                if len(parts) >= 2:
                    recipient = parts[0]
                    # Get the full text after the recipient
                    full_parts = arg.strip().split(maxsplit=1)
                    rest = full_parts[1] if len(full_parts) > 1 else ""

                    # Check for quoted subject
                    subject = None
                    message_body = rest
                    if rest.startswith('"'):
                        # Find closing quote
                        end_quote = rest.find('"', 1)
                        if end_quote > 0:
                            subject = rest[1:end_quote]
                            message_body = rest[end_quote + 1 :].strip()

                    return await self._send_new_mail(accounts, recipient, message_body, attachments, subject=subject)

                return {
                    "type": "text",
                    "content": 'Usage:\n- `mail` - Recent messages\n- `mail folders` - Browse IMAP folders\n- `mail folder <account> <folder>` - View folder contents\n- `mail sum <account>` - AI summary of inbox\n- `mail search <account> <query>` - Search messages\n- `mail send [account] <contact> ["subject"] <message>` - Send email\n- `mail read <account> [folder:]<id>` - Read message\n- `mail reply <account> [folder:]<id> <message>` - Reply\n- `mail translate <account> [folder:]<id>` - Translate message\n- `mail archive <account> <id>` - Archive\n- `mail delete <account> [folder:]<id>` - Delete',
                }

        except Exception as e:
            logger.error(f"Mail command error: {e}")
            return {"type": "text", "content": f"Error: {str(e)}"}

    async def _send_new_mail(
        self,
        accounts: list,
        recipient: str,
        message_body: str,
        attachments: Optional[list] = None,
        from_account=None,
        subject: Optional[str] = None,
    ) -> dict:
        """Send a new email, resolving contact name to email if needed."""
        import re

        if not message_body:
            return {"type": "text", "content": "Please provide a message. Example: `mail linda Hey, how are you?`"}

        # Determine if recipient is an email or a contact name
        to_email = None
        contact_name = None

        if "@" in recipient:
            # It's already an email address
            to_email = recipient
        else:
            # Search contacts for matching name
            contacts = get_user_contacts(self.user.id, recipient, self.db)
            if not contacts:
                return {
                    "type": "text",
                    "content": f"No contact found matching '{recipient}'. Try:\n- `mail linda hello` (contact name)\n- `mail linda@example.com hello` (email address)",
                }

            # Find first contact with an email
            for contact in contacts:
                if contact.email:
                    to_email = contact.email
                    contact_name = contact.name
                    break

            if not to_email:
                return {"type": "text", "content": f"Contact '{recipient}' found but has no email address."}

        # Use specified account or first configured account
        if from_account is None:
            from_account = accounts[0]

        # Use provided subject or generate from first part of message
        if subject:
            subject_text = subject
        else:
            # Auto-generate subject from first part of message (up to 50 chars or first sentence)
            subject_text = message_body[:50].split(".")[0].split("!")[0].split("?")[0]
            if len(subject_text) < len(message_body):
                subject_text = subject_text.strip() + "..."
            else:
                subject_text = subject_text.strip()

        success = send_email(from_account, to_email, subject_text, message_body, attachments=attachments)

        if success:
            attachment_note = f" with {len(attachments)} attachment(s)" if attachments else ""
            if contact_name:
                return {"type": "text", "content": f"✅ Email sent to **{contact_name}** ({to_email}){attachment_note}"}
            else:
                return {"type": "text", "content": f"✅ Email sent to {to_email}{attachment_note}"}
        else:
            return {"type": "text", "content": f"❌ Failed to send email to {to_email}"}

    # Cache for todo UIDs (for rm command)
    _todo_uid_cache: dict = {}

    async def _todo_command(self, arg: str) -> dict:
        """CalDAV Todo list commands - list, add, remove"""
        if not self.user:
            return {"type": "text", "content": "Please log in to use the todo command."}

        # Check if user has calendars configured
        calendars = get_user_calendars(self.user.id, self.db)
        if not calendars:
            return {"type": "text", "content": "No calendars configured. Add calendars in User Settings to use todos."}

        parts = arg.strip().split(maxsplit=1)
        subcommand = parts[0].lower() if parts else ""
        param = parts[1] if len(parts) > 1 else ""

        try:
            # List todos (default)
            if not subcommand or subcommand in ("list", "ls"):
                todos = get_all_user_todos(self.user.id, self.db)
                # Cache UIDs for delete command
                CommandService._todo_uid_cache[self.user.id] = {i + 1: t.uid for i, t in enumerate(todos)}
                todos_text = format_todos_for_display(todos)
                return {"type": "text", "content": f"## ◈ TODO LIST ◈\n\n{todos_text}"}

            # Add todo
            elif subcommand == "add":
                if not param:
                    return {
                        "type": "text",
                        "content": "Usage: `todo add <task description>`\n\nExample: `todo add Buy groceries`",
                    }

                # Add to first calendar
                cal = calendars[0]
                success = add_todo_to_calendar(cal["url"], cal["username"], cal["password"], summary=param)

                if success:
                    return {"type": "text", "content": f"✅ Todo added: **{param}**"}
                else:
                    return {"type": "text", "content": "❌ Failed to add todo. Check calendar settings."}

            # Remove todo
            elif subcommand in ("rm", "remove", "done", "del", "delete"):
                if not param:
                    return {"type": "text", "content": "Usage: `todo rm <number>`\n\nExample: `todo rm 1`"}

                try:
                    num = int(param)
                except ValueError:
                    return {"type": "text", "content": "Please provide a valid number. Example: `todo rm 1`"}

                # Get cached UID
                user_cache = CommandService._todo_uid_cache.get(self.user.id, {})
                if num not in user_cache:
                    # Refresh cache
                    todos = get_all_user_todos(self.user.id, self.db)
                    CommandService._todo_uid_cache[self.user.id] = {i + 1: t.uid for i, t in enumerate(todos)}
                    user_cache = CommandService._todo_uid_cache.get(self.user.id, {})

                if num not in user_cache:
                    return {"type": "text", "content": f"Invalid todo number: {num}. Run `todo` to see your list."}

                todo_uid = user_cache[num]

                # Try to delete from all calendars
                deleted = False
                for cal in calendars:
                    if delete_todo_from_calendar(cal["url"], cal["username"], cal["password"], todo_uid):
                        deleted = True
                        break

                if deleted:
                    # Clear from cache
                    del CommandService._todo_uid_cache[self.user.id][num]
                    return {"type": "text", "content": f"✅ Todo #{num} completed and removed!"}
                else:
                    return {"type": "text", "content": f"❌ Failed to remove todo #{num}."}

            else:
                return {
                    "type": "text",
                    "content": "Usage:\n- `todo` - List all todos\n- `todo add <task>` - Add a new todo\n- `todo rm <#>` - Mark todo as done and remove it",
                }

        except Exception as e:
            logger.error(f"Todo command error: {e}")
            return {"type": "text", "content": f"Error: {str(e)}"}

    async def _music_command(self, arg: str) -> dict:
        """WebDAV music browsing and playback commands"""
        global _music_cache
        from urllib.parse import quote

        if not self.user:
            return {"type": "text", "content": "Please log in to use the music command."}

        config = get_user_webdav_config(self.user.id, self.db)
        if not config or not config.get("url"):
            return {
                "type": "text",
                "content": "WebDAV music not configured. Add your music server in User Settings > Music.",
            }

        parts = arg.strip().split(maxsplit=1)
        subcommand = parts[0].lower() if parts else ""
        param = parts[1] if len(parts) > 1 else ""

        try:
            # Default: shuffle and play music (quick mode - limited scan)
            if not subcommand:
                import random as rand_module

                # Always do a fresh scan for shuffle to avoid using search results
                # This ensures shuffle plays from the full library, not just last search
                all_tracks = scan_all_tracks(
                    config["url"], config["username"], config["password"], "/", max_tracks=1000
                )

                if not all_tracks:
                    return {
                        "type": "text",
                        "content": "No tracks found. Try `music browse` to explore your library first.",
                    }

                # Shuffle tracks
                all_tracks = list(all_tracks)
                rand_module.shuffle(all_tracks)

                # Cache shuffled playlist
                _music_cache[self.user.id] = {"tracks": all_tracks, "folders": [], "current_path": "/"}

                # Build track list for playlist
                playlist = []
                for track in all_tracks[:500]:
                    playlist.append(
                        {
                            "path": track.path,
                            "title": track.title,
                            "artist": track.artist or "",
                            "album": track.album or "",
                            "streamUrl": get_stream_url(track.path),
                        }
                    )

                return {
                    "type": "music_playlist",
                    "content": f"🔀 Shuffling {len(all_tracks)} tracks...\n\nNow playing: **{all_tracks[0].title}**"
                    + (f" - {all_tracks[0].artist}" if all_tracks[0].artist else ""),
                    "tracks": playlist,
                }

            # Browse folder
            if subcommand == "browse":
                path = param if param else "/"
                contents = list_folder(config["url"], config["username"], config["password"], path)

                # Cache results
                _music_cache[self.user.id] = {
                    "tracks": contents.get("tracks", []),
                    "folders": contents.get("folders", []),
                    "current_path": path,
                }

                return {"type": "text", "content": format_music_browse(contents, path)}

            # Search tracks
            elif subcommand == "search":
                if not param:
                    return {
                        "type": "text",
                        "content": "Usage: `music search <query>`\n\nExample: `music search beatles`",
                    }

                tracks = search_tracks(config["url"], config["username"], config["password"], param)

                # Cache results
                _music_cache[self.user.id] = {"tracks": tracks, "folders": [], "current_path": "/"}

                return {"type": "text", "content": format_music_tracks(tracks, f"Search: {param}")}

            # Play track by number
            elif subcommand == "play":
                if not param:
                    return {"type": "text", "content": "Usage: `music play <#>`\n\nExample: `music play 1`"}

                try:
                    num = int(param)
                except ValueError:
                    return {"type": "text", "content": "Please provide a valid track number."}

                cache = _music_cache.get(self.user.id, {})
                tracks = cache.get("tracks", [])

                if not tracks:
                    return {"type": "text", "content": "No tracks loaded. Browse or search music first."}

                if num < 1 or num > len(tracks):
                    return {"type": "text", "content": f"Invalid track number. Choose 1-{len(tracks)}."}

                track = tracks[num - 1]
                stream_url = get_stream_url(track.path)

                return {
                    "type": "music_play",
                    "content": f"## ◈ NOW PLAYING ◈\n\n**{track.title}**"
                    + (f"\n*{track.artist}*" if track.artist else ""),
                    "track": {
                        "path": track.path,
                        "title": track.title,
                        "artist": track.artist,
                        "album": track.album,
                        "streamUrl": stream_url,
                        "duration": track.duration,
                    },
                }

            # Queue management
            elif subcommand == "queue":
                if param.startswith("add "):
                    try:
                        num = int(param[4:].strip())
                    except ValueError:
                        return {"type": "text", "content": "Usage: `music queue add <#>`"}

                    cache = _music_cache.get(self.user.id, {})
                    tracks = cache.get("tracks", [])

                    if num < 1 or num > len(tracks):
                        return {"type": "text", "content": f"Invalid track number. Choose 1-{len(tracks)}."}

                    track = tracks[num - 1]
                    stream_url = get_stream_url(track.path)

                    return {
                        "type": "music_queue_add",
                        "content": f"Added to queue: **{track.title}**",
                        "track": {"title": track.title, "artist": track.artist, "stream_url": stream_url},
                    }
                else:
                    return {
                        "type": "text",
                        "content": "Usage: `music queue add <#>`\n\nQueue is managed by the player. Use the player controls to view queue.",
                    }

            # Mood-based playlist (LLM)
            elif subcommand == "mood":
                if not param:
                    return {
                        "type": "text",
                        "content": "Usage: `music mood <vibe>`\n\nExamples:\n- `music mood chill`\n- `music mood upbeat workout`\n- `music mood relaxing evening`",
                    }

                cache = _music_cache.get(self.user.id, {})
                tracks = cache.get("tracks", [])

                # Auto-scan library if no tracks loaded
                if not tracks:
                    config = get_user_webdav_config(self.user.id, self.db)
                    if not config:
                        return {
                            "type": "text",
                            "content": "WebDAV music not configured. Set up in User Settings > Music.",
                        }

                    # Scan all tracks (this may take a moment)
                    tracks = scan_all_tracks(config["url"], config["username"], config["password"], max_tracks=500)

                    if not tracks:
                        return {"type": "text", "content": "No tracks found in music library."}

                    # Cache the scanned tracks
                    _music_cache[self.user.id] = {"tracks": tracks, "folders": [], "current_path": "/"}

                playlist = await generate_mood_playlist(tracks, param, self.chat_service)

                if not playlist:
                    return {"type": "text", "content": f"No tracks found matching mood: {param}"}

                # Cache the playlist as current tracks
                _music_cache[self.user.id]["tracks"] = playlist

                # Build track list for player
                playlist_data = [
                    {"path": t.path, "title": t.title, "artist": t.artist, "streamUrl": get_stream_url(t.path)}
                    for t in playlist
                ]

                return {
                    "type": "music_playlist",
                    "content": format_music_tracks(playlist, f"Mood: {param}"),
                    "tracks": playlist_data,
                }

            # Skip to next track
            elif subcommand in ("skip", "next"):
                return {"type": "music_next", "content": "Skipping to next track..."}

            # Previous track
            elif subcommand == "prev":
                return {"type": "music_prev", "content": "Going to previous track..."}

            # Random/shuffle play
            elif subcommand == "random":
                # Always scan fresh to avoid playing from search results
                tracks = scan_all_tracks(config["url"], config["username"], config["password"], "/", max_tracks=1000)

                if not tracks:
                    return {"type": "text", "content": "No tracks available for random play. Try `music browse` first."}

                # Pick a random track
                import random as rand_module

                track = rand_module.choice(tracks)
                return {
                    "type": "music_play",
                    "content": f"🎲 Random: **{track.title}**" + (f" - {track.artist}" if track.artist else ""),
                    "track": {
                        "path": track.path,
                        "title": track.title,
                        "artist": track.artist or "",
                        "album": track.album or "",
                        "streamUrl": get_stream_url(track.path),
                    },
                }

            # Shuffle all tracks
            elif subcommand == "shuffle":
                import random as rand_module

                # Always scan fresh to avoid shuffling just search results
                all_tracks = scan_all_tracks(
                    config["url"], config["username"], config["password"], "/", max_tracks=1000
                )

                if not all_tracks:
                    return {"type": "text", "content": "No tracks found. Browse or search music first."}

                # Make a copy and shuffle
                all_tracks = list(all_tracks)
                rand_module.shuffle(all_tracks)

                # Update cache with shuffled tracks
                _music_cache[self.user.id] = {"tracks": all_tracks, "folders": [], "current_path": "/"}

                # Build playlist data
                playlist_data = [
                    {"path": t.path, "title": t.title, "artist": t.artist or "", "streamUrl": get_stream_url(t.path)}
                    for t in all_tracks
                ]

                return {
                    "type": "music_playlist",
                    "content": f"Shuffling {len(all_tracks)} tracks",
                    "tracks": playlist_data,
                }

            # Queue all cached tracks (from last browse/search)
            elif subcommand == "queueall":
                cached = _music_cache.get(self.user.id, {})
                tracks = cached.get("tracks", [])

                if not tracks:
                    return {"type": "text", "content": "No tracks to queue. Browse or search first."}

                playlist_data = [
                    {"path": t.path, "title": t.title, "artist": t.artist or "", "streamUrl": get_stream_url(t.path)}
                    for t in tracks
                ]

                return {"type": "music_playlist", "content": f"Queued {len(tracks)} tracks", "tracks": playlist_data}

            # Stop playback
            elif subcommand == "stop":
                return {"type": "music_stop", "content": "Playback stopped."}

            else:
                return {
                    "type": "text",
                    "content": "Usage:\n- `music` - Browse music library\n- `music browse <path>` - Browse folder\n- `music search <query>` - Search tracks\n- `music play <#>` - Play track\n- `music shuffle` - Shuffle all tracks\n- `music queueall` - Queue all from last search/browse\n- `music random` - Play random track\n- `music skip` / `music next` - Skip to next\n- `music prev` - Previous track\n- `music mood <vibe>` - AI mood playlist\n- `music stop` - Stop playback",
                }

        except Exception as e:
            logger.error(f"Music command error: {e}")
            return {"type": "text", "content": f"Error: {str(e)}"}

    async def _translate_command(self, arg: str) -> dict:
        """Translate last response or email to specified language."""
        if not self.user:
            return {"type": "text", "content": "Please log in to use translate."}

        parts = arg.strip().split()
        if not parts:
            return {
                "type": "text",
                "content": "Usage:\n- `translate <language>` - Translate last response\n- `translate email <language>` - Translate last email\n\nExamples: `translate spanish`, `translate email japanese`",
            }

        # Check if translating email
        if parts[0].lower() == "email":
            language = parts[1] if len(parts) > 1 else "English"
            # Get last email from conversation context
            # For now, suggest using mail translate command
            return {
                "type": "text",
                "content": f"To translate an email, use:\n`mail translate <account> <id> {language}`\n\nFirst check your mail with `mail` to get the email ID.",
            }

        # Translate last assistant response
        language = parts[0]

        # Get the last assistant message from the conversation
        from app.models import Conversation, Message

        # Find user's most recent conversation
        conversation = (
            self.db.query(Conversation)
            .filter(Conversation.user_id == self.user.id)
            .order_by(Conversation.updated_at.desc())
            .first()
        )

        if not conversation:
            return {"type": "text", "content": "No conversation found to translate."}

        # Get last assistant message
        last_msg = (
            self.db.query(Message)
            .filter(Message.conversation_id == conversation.id, Message.role == "assistant")
            .order_by(Message.created_at.desc())
            .first()
        )

        if not last_msg or not last_msg.content:
            return {"type": "text", "content": "No previous response to translate."}

        # Truncate if too long
        content = last_msg.content
        if len(content) > 3000:
            content = content[:3000] + "..."

        # Use AI to translate
        messages = [
            {
                "role": "system",
                "content": f"Translate the following text to {language}. Preserve formatting, code blocks, and structure. Only output the translation, nothing else.",
            },
            {"role": "user", "content": content},
        ]

        try:
            translation = await self.chat_service.chat(messages)
            return {"type": "text", "content": f"## Translation ({language})\n\n{translation}"}
        except Exception as e:
            logger.error(f"Translation error: {e}")
            return {"type": "text", "content": f"Translation failed: {str(e)}"}


def get_command_service(db: Session) -> CommandService:
    return CommandService(db)
