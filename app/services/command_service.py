import json
import logging
import re
import threading
from typing import TYPE_CHECKING, Callable, Optional, Tuple
from datetime import datetime

from sqlalchemy.orm import Session

from app.routers.news import fetch_news_from_source, get_user_news_sources
from app.services.chat_service import ChatService
from app.services.proxy_image_cache import register as proxy_image_register
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
from app.services.nyaa_service import NyaaResult, format_nyaa_results, search_nyaa
from app.services.search_service import SearchService
from app.services.torrent_service import (
    TorrentResult,
    format_all_categories,
    format_torrent_results,
    scrape_all_categories,
    scrape_torrents,
    search_torrents,
)
from app.services.youtube_service import (
    check_ytdlp_available,
    download_video_and_save_to_storage,
    download_mp3_and_save_to_storage,
    extract_download_urls,
    extract_youtube_urls,
    format_download_result,
    is_youtube_url,
    summarize_youtube,
)

# Lock now handled inside image_factory for fine-grained control

if TYPE_CHECKING:
    from app.models import User

logger = logging.getLogger(__name__)


def _find_firefox() -> Optional[str]:
    """Locate a Firefox binary, or None. Prefers PATH, then common install dirs."""
    import os
    import shutil
    return (
        shutil.which("firefox")
        or shutil.which("firefox-bin")
        or next((c for c in ("/opt/firefox/firefox", "/usr/bin/firefox-bin",
                             "/usr/bin/firefox") if os.path.exists(c)), None)
    )


# Firefox uses its default profile for --screenshot (a fresh `-profile` dir hangs
# headless), so serialize captures to avoid the profile's single-instance lock.
_screenshot_lock = threading.Lock()


def _capture_full_page(url: str, width: int = 1280, timeout: int = 60) -> bytes:
    """Render `url` in headless Firefox and return a full-page PNG (blocking).

    Uses Firefox's built-in `--screenshot` mode (full-page by default) via a
    subprocess instead of Selenium/geckodriver: the marionette handshake hangs on
    some Firefox builds, and a subprocess gives a hard, killable timeout with no
    orphaned browser processes. Raises RuntimeError/TimeoutExpired on failure.
    """
    import os
    import shutil
    import subprocess
    import tempfile

    firefox = _find_firefox()
    if not firefox:
        raise RuntimeError("Firefox not found on the server")

    tmpdir = tempfile.mkdtemp(prefix="ffshot_")
    out = os.path.join(tmpdir, "shot.png")
    try:
        with _screenshot_lock:
            # Width only (no height) → Firefox captures the FULL page height at this
            # width; passing a height (e.g. 1280,1080) crops to just that viewport.
            proc = subprocess.run(
                [firefox, "--headless", "--new-instance",
                 f"--window-size={width}", "--screenshot", out, url],
                timeout=timeout, capture_output=True,
            )
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            err = (proc.stderr or b"").decode("utf-8", "ignore").strip()
            raise RuntimeError(f"Firefox produced no screenshot. {err[-300:]}")
        with open(out, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# Cache for torrent results (per user, per category) - thread-safe with locks
_torrent_cache: dict[int, dict[str, list[TorrentResult]]] = {}
_nyaa_cache: dict[int, list[NyaaResult]] = {}


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
        "files": "Search for files in your storage",
        "help": "Show this help message",
        "search": "Web search: search <query>",
        "images": "Image search: images <query>",
        "geni": "Generate image: geni <prompt>",
        "yt": "YouTube search: yt <query>",
        "ytdl": "Download YouTube or X: ytdl <url> (MP3 default), ytdl mp3/video <url>",
        "torrents": "Torrent search: torrents <query>",
        "nyaa": "Anime torrents: nyaa <query>",
        "dailynews": "Web news: dailynews <source>",
        "logs": "View system logs",
        "mail": "Email: mail <to> [subject] <body>",
        "translate": "Translate: translate <text> to <lang>",
        "4chan": "4chan browser: 4chan [g|pol|h] - view catalog",
        "compress": "Compress attached image(s) or video(s)",
        "convert": "Convert image(s) to PDF or a PDF to images",
        "node": "Remote node mgmt: node <name> <cmd> | node all <cmd> | node agent <name> <goal> | node list | node jobs | node log <id> | node kill <id>",
        "budget": "Show your budget summary (income, unpaid bills, remaining)",
        "bills": "List your bills: bills (unpaid) | bills all | bills paid",
        "pay": "Pay a bill by name: pay <bill name>",
        "addbill": "Add a bill: addbill <name> <amount> [income]",
        "screenshot": "Full-page screenshot of a website: screenshot <url>",
    }
    # Command aliases (alias -> canonical command)
    COMMAND_ALIASES = {
        "torrent": "torrents",
        "bt": "torrents",
        "yt-dlp": "ytdl",
        "ytdlp": "ytdl",
        "youtube": "yt",
        "nodes": "node",
        "finance": "budget",
        "shot": "screenshot",
        "ss": "screenshot",
    }

    # Natural language phrases that map directly to commands with arguments
    # Format: "phrase" -> ("command", "argument")
    PHRASE_COMMANDS = {}

    def __init__(self, db: Session, user: Optional["User"] = None):
        self.db = db
        self.user = user
        self.search_service = SearchService(db)
        self.chat_service = ChatService(db, user=user)

    def parse_command(self, message: str) -> Tuple[Optional[str], str]:
        """Parse message for commands, return (command, argument)"""
        # Remove emojis and other unicode symbols that might interfere with matching
        import re
        # Remove common emojis and symbols (✏️, 🔄, etc.) but keep the text
        cleaned_message = re.sub(r'[✏️🔄📅📆🗓️➕➖✕×]', '', message)
        lower = cleaned_message.lower().strip()

        # Check natural language phrases first (exact match)
        if lower in self.PHRASE_COMMANDS:
            cmd, arg = self.PHRASE_COMMANDS[lower]
            return cmd, arg

        # Video downloads
        for prefix in ["download this video ", "download video "]:
            if lower.startswith(prefix):
                url = message[len(prefix):].strip()
                return "ytdl", f"video {url}"
        
        # Generic download with YouTube URL
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
        node_notify: Optional[Callable] = None,
    ) -> dict:
        """Execute a command and return the result.

        Args:
            command: The command name
            arg: Command arguments
            last_prompt: Last image generation prompt (for regeneration)
            stop_check: Callable to check if execution should stop
            attachments: List of (filename, data_bytes, content_type) tuples for mail
        """
        # Resolve aliases (e.g. "shot" → "screenshot") centrally so callers that match
        # commands literally (Telegram) accept them just like the web UI's parse_command.
        command = self.COMMAND_ALIASES.get(command, command)
        if command == "help":
            return await self._help_command()
        elif command == "search":
            return await self._search_command(arg)
        elif command == "images":
            return await self._images_command(arg)
        elif command == "files":
            return await self._files_command(arg)
        elif command == "geni":
            return await self._geni_command(arg, stop_check)
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
        elif command == "mail":
            return await self._mail_command(arg, attachments=attachments)
        elif command == "todo":
            return await self._todo_command(arg)
        elif command == "translate":
            return await self._translate_command(arg, attachments=attachments)
        elif command == "4chan":
            return await self._4chan_command(arg)
        elif command == "compress":
            return await self._compress_command(attachments)
        elif command == "convert":
            return await self._convert_command(arg, attachments)
        elif command == "node":
            return await self._node_command(arg, notify=node_notify)
        elif command == "budget":
            return await self._budget_command()
        elif command == "bills":
            return await self._bills_command(arg)
        elif command == "pay":
            return await self._pay_command(arg)
        elif command == "addbill":
            return await self._addbill_command(arg)
        elif command == "screenshot":
            return await self._screenshot_command(arg)
        else:
            return {"type": "text", "content": f"Unknown command: {command}"}

    async def _help_command(self) -> dict:
        """Show available commands and plugins"""
        help_text = "## Available Commands\n\n"

        # Built-in commands
        for cmd, desc in self.COMMANDS.items():
            help_text += f"**{cmd}** - {desc}\n"

        # Plugin commands


        return {"type": "text", "content": help_text}

    # --- Finance (Budget Manager) commands ---------------------------------

    async def _budget_command(self) -> dict:
        from app.services import finance_service
        try:
            base, key = finance_service.get_config(self.db, self.user)
            summary = await finance_service.get_summary(base, key)
        except finance_service.FinanceError as e:
            return {"type": "text", "content": f"💰 {e}"}
        return {"type": "text", "content": finance_service.format_summary(summary)}

    async def _bills_command(self, arg: str) -> dict:
        from app.services import finance_service
        arg = (arg or "").strip().lower()
        status = None if arg == "all" else (arg if arg in ("paid", "unpaid") else "unpaid")
        header = {"paid": "Paid bills", "unpaid": "Unpaid bills", None: "All bills"}.get(status, "Unpaid bills")
        try:
            base, key = finance_service.get_config(self.db, self.user)
            bills = await finance_service.get_bills(base, key, status=status)
        except finance_service.FinanceError as e:
            return {"type": "text", "content": f"💰 {e}"}
        return {"type": "text", "content": finance_service.format_bills(bills, header=header)}

    async def _pay_command(self, arg: str) -> dict:
        from app.services import finance_service
        name = (arg or "").strip()
        if not name:
            return {"type": "text", "content": "Usage: pay <bill name>"}
        try:
            base, key = finance_service.get_config(self.db, self.user)
            result = await finance_service.pay_bill(base, key, name)
        except finance_service.FinanceError as e:
            return {"type": "text", "content": f"💰 {e}"}
        return {"type": "text", "content": f"✅ {result.get('message', 'Paid.')}"}

    async def _addbill_command(self, arg: str) -> dict:
        from app.services import finance_service
        try:
            name, amount, is_income = finance_service.parse_add_bill_arg(arg or "")
            base, key = finance_service.get_config(self.db, self.user)
            result = await finance_service.add_bill(base, key, name, amount, is_income=is_income)
        except finance_service.FinanceError as e:
            return {"type": "text", "content": f"💰 {e}"}
        return {"type": "text", "content": f"✅ {result.get('message', 'Added.')}"}

    async def _search_command(self, query: str) -> dict:
        if not query:
            return {"type": "text", "content": "Please provide a search query. Example: `search latest AI news`"}

        clean_query, categories, time_range = self.search_service.detect_search_intent(query)
        results = await self.search_service.web_search(
            clean_query, limit=5, categories=categories, time_range=time_range
        )
        # Fall back to a plain general search if a category search came up empty.
        if not results and (categories or time_range):
            results = await self.search_service.web_search(clean_query, limit=5)
        if not results:
            return {"type": "text", "content": f"No results found for: {query}"}

        scope = f" ({categories})" if categories else ""
        # Format results for AI summarization
        context = f"Search results for '{clean_query}'{scope}:\n\n"
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
        results = results[:10]
        # For Android: limit to 5 items and send both thumb_id and img_src so payload fits and direct fallback works.
        # (10 items + img_src truncates; 10 items without img_src = proxy fails = 0 images. 5 + img_src = 5 images.)
        images_payload = []
        for r in results[:5]:
            thumb_url = (r.get("img_src") or "").strip()
            if not thumb_url:
                continue
            page_url = (r.get("url") or thumb_url).strip()
            title = (r.get("title") or "Image")[:200]
            try:
                thumb_id = proxy_image_register(thumb_url, self.db)
                images_payload.append({"title": title, "url": page_url, "thumb_id": thumb_id, "img_src": thumb_url})
            except Exception:
                images_payload.append({"title": title, "url": page_url, "img_src": thumb_url})
        return {"type": "images", "content": f"Found {len(images_payload)} images for: {query}", "images": images_payload}

    async def _files_command(self, query: str) -> dict:
        """Search for files in user's storage."""
        if not query:
            return {"type": "text", "content": "Please provide a search query. Example: `files image` or `files document.pdf`"}
        
        return await self._search_files_internal(query)
    
    async def _search_files_internal(self, query: str) -> dict:
        """Internal file search function - handles storage proxy correctly."""
        from pathlib import Path
        from app.services.storage_service import get_storage_service
        from app.models import Setting
        import httpx

        # Check if using remote storage
        storage_setting = self.db.query(Setting).filter(Setting.key == "storage_server_url").first()
        if storage_setting and storage_setting.value and storage_setting.value.startswith(('http://', 'https://')):
            # Use remote storage API with async httpx (same as files router)
            url = storage_setting.value.strip()
            try:
                headers = {
                    "X-Posterchanai-Load-Balanced": "true"
                }
                
                # Try both endpoints (same as files router)
                search_urls = [
                    f"{url.rstrip('/')}/api/files/search",
                    f"{url.rstrip('/')}/api/storage/search"
                ]
                
                response = None
                async with httpx.AsyncClient(timeout=60.0) as client:
                    for search_url in search_urls:
                        try:
                            response = await client.get(
                                search_url,
                                params={"query": query, "username": self.user.username},
                                headers=headers
                            )
                            if response.status_code == 200:
                                break
                        except Exception as e:
                            logger.debug(f"Tried {search_url}, got error: {e}")
                            continue
                    
                    if response and response.status_code == 200:
                        data = response.json()
                        results = data.get('results', [])
                        return {
                            "type": "files",
                            "content": f"Found {len(results)} file(s) matching '{query}'",
                            "files": results[:50],  # Limit to 50 results
                            "query": query
                        }
                    else:
                        logger.warning(f"Storage server search failed, falling back to local search")
            except Exception as e:
                logger.warning(f"Error searching remote files: {e}, falling back to local search")

        # Local storage search (or fallback if remote search failed)
        storage = get_storage_service(self.db)
        user_path = storage.get_user_path(self.user.username)

        results = []
        query_lower = query.lower()

        try:
            # Recursively search through user's files
            for item in user_path.rglob('*'):
                try:
                    if item.is_dir():
                        continue

                    filename = item.name.lower()
                    relative_path = str(item.relative_to(user_path)).lower()

                    if query_lower in filename or query_lower in relative_path:
                        stat = item.stat()
                        results.append({
                            "name": item.name,
                            "path": str(item.relative_to(user_path)),
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                        })
                except Exception as e:
                    logger.warning(f"Error processing file {item}: {e}")
                    continue

            # Sort by modified time (newest first)
            results.sort(key=lambda x: x.get('modified', 0), reverse=True)

            return {
                "type": "files",
                "content": f"Found {len(results)} file(s) matching '{query}'",
                "files": results[:50],  # Limit to 50 results
                "query": query
            }
        except Exception as e:
            logger.error(f"Error searching files locally: {e}", exc_info=True)
            return {"type": "text", "content": f"Error searching files: {str(e)}"}

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
            logger.info(f"Generating image with prompt: {prompt[:100]}...")
            image_data = await generate_image_for_user(
                db=self.db,
                user=self.user,
                prompt=prompt,
            )
        except Exception as e:
            logger.error(f"Image generation exception: {e}", exc_info=True)
            return {"type": "text", "content": f"Image generation error: {str(e)}\n\nCheck logs for details."}

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

        if not image_data:
            # Get backend info for better error message
            from app.services.image_factory import get_image_backend_info
            backend_info = get_image_backend_info(self.db)
            backend_type = backend_info.get("backend", "unknown")
            
            error_msg = "## ❌ Image Generation Failed\n\n"
            
            if backend_type == "comfyui":
                comfyui_url = backend_info.get("comfyui_url", "")
                if not comfyui_url:
                    error_msg += "**ComfyUI URL not configured.**\n\n"
                    error_msg += "Go to Admin → Services → Image Generation and set the ComfyUI URL.\n"
                else:
                    error_msg += f"**ComfyUI backend configured** (`{comfyui_url}`)\n\n"
                    error_msg += "Possible issues:\n"
                    error_msg += "- ComfyUI server is not running\n"
                    error_msg += "- ComfyUI server is not accessible at the configured URL\n"
                    error_msg += "- Check server logs for errors\n"
            elif backend_type == "native":
                error_msg += "**Native diffusers backend**\n\n"
                error_msg += "Possible issues:\n"
                error_msg += "- Model not loaded (check VRAM availability)\n"
                error_msg += "- Generation failed (check logs)\n"
                error_msg += "- GPU/XPU not available\n"
            else:
                error_msg += "**Image backend not properly configured.**\n\n"
                error_msg += "Go to Admin → Services → Image Generation to configure.\n"
            
            error_msg += "\n**Prompt:** " + prompt
            logger.warning(f"Image generation returned None for prompt: {prompt[:100]}...")
            
            return {"type": "text", "content": error_msg}

        # Don't save automatically - just display the image with a save button
        return {
            "type": "generated_image",
            "content": f"Generated image for: {prompt}",
            "image": image_data,
            "prompt": prompt,
        }

    async def _screenshot_command(self, arg: str) -> dict:
        """Capture a full-page screenshot of a website via headless Firefox.

        Returns the shared `generated_image` shape so every channel renders it the
        same way: inline in the web UI (with a save button), a photo/document on
        Telegram, and an uploaded image in Matrix.
        """
        import asyncio
        import base64

        url = arg.strip().split()[0] if arg.strip() else ""
        if not url:
            return {"type": "text", "content": "Usage: `screenshot <url>` — e.g. `screenshot example.com`"}
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url

        import subprocess
        try:
            # Backstop above the subprocess's own timeout so the handler always replies.
            png = await asyncio.wait_for(asyncio.to_thread(_capture_full_page, url), timeout=75)
        except (asyncio.TimeoutError, subprocess.TimeoutExpired):
            return {"type": "text", "content": f"📸 Timed out capturing {url} — the page took too long to render."}
        except Exception as e:
            logger.error(f"[screenshot] {url}: {e}", exc_info=True)
            msg = str(e)
            if "firefox not found" in msg.lower():
                return {"type": "text", "content": f"📸 Couldn't capture {url}: Firefox isn't installed on the server."}
            first_line = next((ln for ln in msg.splitlines() if ln.strip()), "unknown error")
            return {"type": "text", "content": f"📸 Couldn't capture {url}: {first_line}"}

        return {
            "type": "generated_image",
            "content": f"📸 {url}",
            "image": base64.b64encode(png).decode("ascii"),
            "prompt": url,
            # Telegram compresses photos (tiny/unreadable for tall pages) — deliver as a
            # full-resolution document instead. Ignored by the web UI / Matrix renderers.
            "prefer_document": True,
        }

    def _format_size(self, size_bytes: int) -> str:
        """Format bytes to human readable size"""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} PB"

    async def _youtube_command(self, arg: str) -> dict:
        """Summarize a YouTube video transcript"""
        if not arg:
            return {
                "type": "text",
                "content": """## YouTube Commands

**Summarize a video:**
`yt <url>` - Get AI summary of video transcript

**Download:**
- `ytdl <url>` - Download as MP3 to Music (default)
- `ytdl mp3 <url>` - Download as MP3 to Music
- `ytdl video <url>` - Download as video (MP4) to YouTube Videos

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
        """Download a YouTube video (audio or video) to storage"""

        if not arg:
            return {
                "type": "text",
                "content": """## YouTube / X (Twitter) Download

**Usage:**
- `ytdl <url>` - Download as MP3 to Music (default)
- `ytdl mp3 <url>` - Download as MP3 to Music
- `ytdl video <url>` - Download as video (MP4) to folder

**Supported:** YouTube, X.com (Twitter) links.

**Examples:**
- `ytdl https://youtube.com/watch?v=...` - Download as MP3
- `ytdl video https://x.com/i/status/123...` - Download X video
- `ytdl https://x.com/user/status/123...` - Download as MP3

Files are saved to your Storage.""",
            }

        # Check if yt-dlp is available
        if not check_ytdlp_available():
            return {"type": "text", "content": "❌ yt-dlp not installed. Install with: `pip install yt-dlp`"}

        # Parse: "ytdl video <url>" | "ytdl mp3 <url>" | "ytdl <url>" (default = MP3)
        parts = arg.strip().split(maxsplit=1)
        first = parts[0].lower()
        if first == "video":
            url_arg = parts[1] if len(parts) > 1 else ""
            if not url_arg:
                return {"type": "text", "content": "Usage: `ytdl video <url>`\n\nExample: `ytdl video https://youtube.com/watch?v=...`"}
            as_mp3 = False
        elif first == "mp3":
            url_arg = parts[1] if len(parts) > 1 else ""
            if not url_arg:
                return {"type": "text", "content": "Usage: `ytdl mp3 <url>`\n\nExample: `ytdl mp3 https://youtube.com/watch?v=...`"}
            as_mp3 = True
        else:
            url_arg = arg
            as_mp3 = True  # default: MP3

        urls = extract_download_urls(url_arg)
        if not urls:
            return {"type": "text", "content": "Could not find a valid YouTube or X (Twitter) URL. Example: `ytdl https://x.com/i/status/123` or `ytdl https://youtube.com/watch?v=...`"}

        target_url = urls[0]
        if as_mp3:
            logger.info(f"[ytdl] Command: mp3 url={target_url!r} user_id={self.user.id}")
            result = await download_mp3_and_save_to_storage(
                url=target_url,
                user_id=self.user.id,
                db=self.db,
                subfolder="Music",
            )
        else:
            logger.info(f"[ytdl] Command: video url={target_url!r} user_id={self.user.id}")
            result = await download_video_and_save_to_storage(
                url=target_url,
                user_id=self.user.id,
                db=self.db,
                subfolder="YouTube Videos",
            )

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

        # Server-to-server requests don't need authentication
        url = f"{server_url.rstrip('/')}/api/torrent{endpoint}"
        headers = {
            "X-Posterchanai-Load-Balanced": "true"
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info(f"[TORRENT] TUI request to {url} (load-balanced)")
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
            except ValueError as e:
                msg = str(e)
                suffix = "\n\nConfigure proxy in Admin → Site Settings → BitTorrent Client → HTTP Proxy Host" if "requires http proxy" in msg.lower() else ""
                return {"type": "text", "content": f"{msg}{suffix}"}
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
            except ValueError as e:
                msg = str(e)
                suffix = "\n\nConfigure proxy in Admin → Site Settings → BitTorrent Client → HTTP Proxy Host" if "requires http proxy" in msg.lower() else ""
                return {"type": "text", "content": f"{msg}{suffix}"}
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
            results = await scrape_torrents(self.db, category, limit=10)

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

        except ValueError as e:
            msg = str(e)
            suffix = "\n\nConfigure proxy in Admin → Site Settings → BitTorrent Client → HTTP Proxy Host" if "requires http proxy" in msg.lower() else ""
            return {"type": "text", "content": f"{msg}{suffix}"}
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
            results = await search_nyaa(query, limit=20)

            if not results:
                return {"type": "text", "content": f"No results found for '{query}' on nyaa.si"}

            # Cache results for download command
            user_id = self.user.id if self.user else 0
            _nyaa_cache[user_id] = results

            formatted = format_nyaa_results(results, query)

            return {"type": "text", "content": formatted}

        except ValueError as e:
            msg = str(e)
            suffix = "\n\nConfigure proxy in Admin → Site Settings → BitTorrent Client → HTTP Proxy Host" if "requires http proxy" in msg.lower() else ""
            return {"type": "text", "content": f"{msg}{suffix}"}
        except Exception as e:
            logger.error(f"Nyaa command error: {e}")
            return {"type": "text", "content": f"Error searching nyaa.si: {str(e)}"}


    async def _news_command(self, arg: str) -> dict:
        """Get news from configured web sources"""
        return await self._dailynews_command(arg)

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
                    source_names = ", ".join(s["name"] for s in all_sources)
                    return {"type": "text", "content": f"No news source matching '{arg.strip()}'. Available sources: {source_names}"}
            else:
                sources = all_sources

            # Fetch news from sources concurrently with timeout
            import asyncio

            async def fetch_single_source(source):
                try:
                    # Add timeout per source to prevent hanging
                    async with asyncio.timeout(45):  # 45 second timeout per source (fetch + AI summary)
                        markdown = await fetch_news_from_source(source["url"], source["name"], self.db)
                        return markdown
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout fetching news from {source['name']}")
                    return f"**{source['name']}:** ⚠️ Timeout fetching headlines (took too long)"
                except Exception as e:
                    logger.error(f"Error fetching news from {source['name']}: {e}")
                    return f"**{source['name']}:** ❌ Error fetching headlines: {str(e)[:100]}"

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

    async def _node_command(self, arg: str, notify: Optional[Callable] = None) -> dict:
        """Run OS commands on configured nodes (SSH or local) as background jobs, with an
        optional agentic mode. Gated by admin Settings (enabled + user allowlist).

        `notify`, when given, is an async callback the caller supplies to deliver a
        finished job's output back to the channel the command came from (web UI
        conversation or Telegram chat). It must not rely on the request's DB session,
        which is closed by the time a long-running job finishes."""
        from app.services import node_service

        if not node_service.user_allowed(self.db, self.user):
            return {"type": "text", "content": "⛔ Remote node management is disabled or you are not authorized. An admin can enable it in Admin → Services → Remote Node Management."}

        parts = arg.strip().split(maxsplit=2)
        sub = parts[0].lower() if parts else ""
        nodes = node_service.get_nodes(self.db)

        def _fmt_nodes() -> str:
            if not nodes:
                return "No nodes configured. Add them in Admin → Services → Remote Node Management (one per line: `name|user@host`)."
            lines = ["**Configured nodes:**"]
            for name, target in nodes.items():
                where = "this host" if target == "local" else target
                lines.append(f"- `{name}` → {where}")
            return "\n".join(lines)

        def _result_for(job, header: str) -> dict:
            """Render a finished job. Short output goes inline; long output shows a tail
            preview inline and attaches the full output as a .txt (delivered as a Telegram
            document or a web-UI download link by the existing `type=='files'` handlers)."""
            out = (job.output or "(no output)").strip()
            preview = f"{header}\n\n```\n{node_service.tail(out, node_service.INLINE_LIMIT)}\n```"
            if len(out) > node_service.INLINE_LIMIT:
                return {
                    "type": "files",
                    "content": preview,
                    "files": [{"filename": f"node-{job.node}-job{job.id}.txt", "data": out.encode("utf-8", "replace")}],
                }
            return {"type": "text", "content": preview}

        # --- management subcommands ---
        if sub in ("", "list", "ls", "help"):
            usage = (
                "**Remote node management**\n\n"
                "- `node <name> <command>` — run a command (long ones run in the background)\n"
                "- `node all <command>` — run a command on every node\n"
                "- `node agent <name> <goal>` — let the AI run commands toward a goal\n"
                "- `node jobs` — list your recent jobs\n"
                "- `node log <id>` — show a job's output\n"
                "- `node kill <id>` — stop a running job\n\n"
                f"{_fmt_nodes()}"
            )
            return {"type": "text", "content": usage}

        if sub == "jobs":
            jobs = node_service.list_jobs(user_id=self.user.id if self.user else None)
            if not jobs:
                return {"type": "text", "content": "No jobs yet."}
            icon = {"running": "⏳", "done": "✅", "failed": "❌", "killed": "🛑"}
            lines = ["**Your node jobs:**"]
            for j in jobs:
                lines.append(f"- {icon.get(j.status, '•')} #{j.id} `{j.node}`: `{j.command[:60]}` — {j.status}")
            lines.append("\nUse `node log <id>` for output.")
            return {"type": "text", "content": "\n".join(lines)}

        if sub == "log":
            if len(parts) < 2 or not parts[1].isdigit():
                return {"type": "text", "content": "Usage: `node log <id>`"}
            job = node_service.get_job(int(parts[1]), user_id=self.user.id if self.user else None)
            if not job:
                return {"type": "text", "content": f"Job #{parts[1]} not found."}
            return _result_for(job, f"**Job #{job.id}** `{job.node}` — {job.status} (exit {job.exit_code})\n`{job.command}`")

        if sub == "kill":
            if len(parts) < 2 or not parts[1].isdigit():
                return {"type": "text", "content": "Usage: `node kill <id>`"}
            _uid = self.user.id if self.user else None
            job = node_service.get_job(int(parts[1]), user_id=_uid)
            if not job:
                return {"type": "text", "content": f"Job #{parts[1]} not found."}
            ok = node_service.kill_job(int(parts[1]), user_id=_uid)
            return {"type": "text", "content": f"{'🛑 Killed' if ok else 'Could not kill (already finished?)'} job #{parts[1]}."}

        # --- fan-out: run the same command on every node ---
        if sub == "all":
            import asyncio
            command = arg.strip()[len(parts[0]):].strip()
            if not command:
                return {"type": "text", "content": "Usage: `node all <command>`"}
            if not nodes:
                return {"type": "text", "content": _fmt_nodes()}
            jobs = {
                name: node_service.start_job(
                    self.db, name, target, command,
                    user_id=self.user.id if self.user else None,
                )
                for name, target in nodes.items()
            }
            await asyncio.gather(*(node_service.await_job(j, wait=10.0) for j in jobs.values()))
            icon = {"done": "✅", "failed": "❌", "killed": "🛑"}
            lines = [f"## `{command}` on {len(jobs)} node(s)"]
            for name, j in jobs.items():
                if j.done:
                    out = (j.output or "(no output)").strip()
                    lines.append(f"\n**{icon.get(j.status, 'ℹ️')} {name}** (exit {j.exit_code})\n```\n{node_service.tail(out, 1200)}\n```")
                else:
                    # Still running — deliver its output to this channel when it finishes.
                    node_service.notify_on_done(j, notify)
                    lines.append(f"\n**⏳ {name}** — still running (job #{j.id}, `node log {j.id}`)")
            return {"type": "text", "content": "\n".join(lines)}

        # --- agentic mode ---
        if sub == "agent":
            if len(parts) < 3:
                return {"type": "text", "content": "Usage: `node agent <name> <goal>`"}
            name, goal = parts[1], parts[2]
            if name not in nodes:
                return {"type": "text", "content": f"Unknown node `{name}`.\n\n{_fmt_nodes()}"}
            try:
                summary = await node_service.run_agent(self.db, self.user, name, nodes[name], goal, self.chat_service)
                return {"type": "text", "content": summary}
            except Exception as e:
                logger.error(f"[node] agent error: {e}", exc_info=True)
                return {"type": "text", "content": f"Agent error: {e}"}

        # --- direct command: node <name> <command...> ---
        name = sub
        if name not in nodes:
            return {"type": "text", "content": f"Unknown node `{name}`.\n\n{_fmt_nodes()}"}
        # Everything after the node name is the command (preserve original spacing/casing).
        command = arg.strip()[len(parts[0]):].strip()
        if not command:
            return {"type": "text", "content": f"Usage: `node {name} <command>`"}

        job = node_service.start_job(
            self.db, name, nodes[name], command,
            user_id=self.user.id if self.user else None,
        )
        await node_service.await_job(job, wait=8.0)
        if job.done:
            icon = {"done": "✅", "failed": "❌", "killed": "🛑"}.get(job.status, "ℹ️")
            return _result_for(job, f"{icon} `{name}` exit {job.exit_code}")
        # Still running — deliver its output to this channel when it finishes.
        node_service.notify_on_done(job, notify)
        return {"type": "text", "content": f"⏳ Started job #{job.id} on `{name}` (still running).\nI'll post the output here when it's done — or check with `node log {job.id}` / stop with `node kill {job.id}`."}

    async def check_youtube_url(self, message: str) -> Optional[dict]:
        """Check if message contains a YouTube URL and summarize it"""
        if not is_youtube_url(message):
            return None

        # Don't auto-summarize if user wants to download
        lower = message.lower()
        download_keywords = ["download", "ytdl", "mp3", "save", "get song", "get video", "download song", "download video"]
        if any(kw in lower for kw in download_keywords):
            return None

        urls = extract_youtube_urls(message)
        if not urls:
            return None

        # Summarize the first YouTube URL found
        success, result = await summarize_youtube(urls[0], self.chat_service)
        # Return result whether success or failure (so user sees error messages)
        return {"type": "text", "content": result}

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
                # Wrap in asyncio timeout to prevent hanging
                import asyncio
                try:
                    messages = await asyncio.wait_for(
                        asyncio.to_thread(fetch_all_accounts, self.user.id, self.db, limit_per_account=10),
                        timeout=20.0  # 20 second total timeout
                    )
                    if not messages:
                        messages = []  # Ensure it's a list
                    return {"type": "text", "content": format_message_list(messages)}
                except asyncio.TimeoutError:
                    logger.warning("Mail fetch timed out after 20 seconds")
                    return {"type": "text", "content": "Mail fetch timed out. The mail server may be slow or unreachable. Please try again."}

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
                return {"type": "text", "content": "⚠️ Calendar event extraction is temporarily unavailable."}

            elif subcommand == "extract-bill":
                return {"type": "text", "content": "⚠️ Bill extraction is temporarily unavailable."}

            elif subcommand == "reply":
                if len(parts) < 4:
                    return {
                        "type": "text",
                        "content": "Usage: `mail reply <account> [folder:]<id> <message>`\n\nExample: `mail reply verita84 123 Thanks for the info!` or `mail reply verita84 INBOX.Archive:456 Thanks!`",
                    }

                account_hint = parts[1]
                uid_part = parts[2]
                reply_body = parts[3]
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

                # Pass attachments if available
                success = await asyncio.to_thread(
                    reply_to_message, self.user.id, self.db, account_email, uid, reply_body, 
                    reply_all=False, attachments=attachments, folder=folder
                )
                if success:
                    attachment_note = f" with {len(attachments)} attachment(s)" if attachments else ""
                    return {"type": "text", "content": f"Reply sent successfully{attachment_note}."}
                else:
                    return {"type": "text", "content": "Failed to send reply."}

            elif subcommand == "forward":
                if len(parts) < 4:
                    return {
                        "type": "text",
                        "content": "Usage: `mail forward <account> [folder:]<id> <recipient> [message]`\n\n**Examples:**\n- `mail forward verita84 123 john@example.com` - Forward message #123 to john@example.com\n- `mail forward verita84 123 john@example.com Check this out!` - Forward with custom message\n- `mail forward verita84 123 john@example.com \"case #12345\" Hello, here is my info:` - Forward with multi-line message\n\n**Note:** The message body can be multi-line. Original message attachments are automatically included.",
                    }

                account_hint = parts[1]
                uid_part = parts[2]
                # parts[3] contains recipient and optionally body text (due to maxsplit=3 in mail command handler)
                recipient_and_body = parts[3].strip()
                
                # Extract recipient - look for email pattern (contains @) or take first word
                # Handle quoted recipients and extract email address
                recipient = None
                forward_body = ""
                
                # Try to find an email address pattern in the string
                # Email pattern: word characters, dots, hyphens, plus signs, followed by @, then domain
                email_pattern = r'\b[\w\.\-+]+@[\w\.\-]+\.[a-zA-Z]{2,}\b'
                email_match = re.search(email_pattern, recipient_and_body)
                
                if email_match:
                    # Found an email address - extract it and everything after it is the body
                    email_start = email_match.start()
                    email_end = email_match.end()
                    recipient = email_match.group(0).strip('"\'')  # Remove quotes if present
                    # Get body text after the email (skip any spaces immediately after)
                    body_start = email_end
                    while body_start < len(recipient_and_body) and recipient_and_body[body_start] in ' \t':
                        body_start += 1
                    if body_start < len(recipient_and_body):
                        forward_body = recipient_and_body[body_start:].strip()
                else:
                    # No email pattern found - try to extract first word/token as recipient
                    # Remove quotes if present
                    tokens = recipient_and_body.split(maxsplit=1)
                    recipient = tokens[0].strip('"\'')
                    if len(tokens) > 1:
                        forward_body = tokens[1].strip()
                
                # Sanitize recipient - remove newlines, quotes, and other invalid characters for email headers
                if recipient:
                    recipient = recipient.replace("\n", " ").replace("\r", "").strip()
                    # Remove surrounding quotes if present
                    recipient = recipient.strip('"\'')
                else:
                    recipient = ""
                
                # Basic email validation - check if it looks like an email address
                # Must contain @ and have a domain part (something after @)
                if not recipient:
                    return {"type": "text", "content": "No recipient email address provided. Usage: `mail forward <account> <id> <recipient> [message]`"}
                
                if "@" not in recipient:
                    return {"type": "text", "content": f"Invalid recipient: `{recipient}`. Please provide a valid email address (must contain @). Example: `mail forward verita84 123 user@example.com`"}
                
                # Check that there's a domain part after @
                email_parts = recipient.split("@")
                if len(email_parts) != 2 or not email_parts[1] or "." not in email_parts[1]:
                    return {"type": "text", "content": f"Invalid email address: `{recipient}`. Email must have a domain (e.g., user@example.com)."}

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

                # Pass attachments if available
                success = await asyncio.to_thread(
                    forward_message, self.user.id, self.db, account_email, uid, recipient, forward_body, 
                    attachments=attachments, folder=folder
                )
                if success:
                    attachment_note = f" with {len(attachments)} attachment(s)" if attachments else ""
                    return {"type": "text", "content": f"Email forwarded to {recipient} successfully{attachment_note}."}
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

                # Don't save automatically - just display the attachment with a save button
                # Encode attachment data as base64 for display
                import base64
                attachment_base64 = base64.b64encode(attachment.data).decode('utf-8')
                
                # Determine MIME type
                import mimetypes
                mime_type, _ = mimetypes.guess_type(attachment.filename)
                if not mime_type:
                    mime_type = 'application/octet-stream'
                
                # Return attachment data for display (image preview if it's an image, otherwise download button)
                if mime_type.startswith('image/'):
                    return {
                        "type": "mail_attachment",
                        "content": f"📎 **{attachment.filename}** ({attachment.size / 1024:.1f} KB)",
                        "filename": attachment.filename,
                        "data": attachment_base64,
                        "mime_type": mime_type,
                        "size": attachment.size,
                        "account": account_email,
                        "uid": uid,
                        "index": att_index,
                    }
                else:
                    return {
                        "type": "mail_attachment",
                        "content": f"📎 **{attachment.filename}** ({attachment.size / 1024:.1f} KB)",
                        "filename": attachment.filename,
                        "data": attachment_base64,
                        "mime_type": mime_type,
                        "size": attachment.size,
                        "account": account_email,
                        "uid": uid,
                        "index": att_index,
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
            # Require full email address since contacts feature is removed
            return {
                "type": "text",
                "content": f"Please provide a full email address. Example: `mail linda@example.com hello`",
            }

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
    async def _todo_command(self, arg: str) -> dict:
        """Todo command - DISABLED (CalDAV removed)"""
        return {"type": "text", "content": "⚠️ The todo feature is temporarily unavailable."}

    async def _translate_command(self, arg: str, attachments: Optional[list] = None) -> dict:
        """Translate an uploaded image/PDF (OCR), or the last response, or an email."""
        if not self.user:
            return {"type": "text", "content": "Please log in to use translate."}

        # Uploaded image/PDF wins: OCR it and translate the whole thing.
        if attachments:
            return await self._translate_attachments(arg, attachments)

        # A URL → fetch the page's real text and translate it (reliable; no OCR).
        _url_match = re.search(r'https?://\S+', arg)
        if _url_match:
            return await self._translate_url(arg, _url_match.group(0).rstrip('.,)>'))

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

        # Translate the last assistant response.
        language = self._parse_language(arg)
        from app.models import Conversation, Message
        conversation = (
            self.db.query(Conversation)
            .filter(Conversation.user_id == self.user.id)
            .order_by(Conversation.updated_at.desc())
            .first()
        )
        if not conversation:
            return {"type": "text", "content": "No conversation found to translate."}
        last_msg = (
            self.db.query(Message)
            .filter(Message.conversation_id == conversation.id, Message.role == "assistant")
            .order_by(Message.created_at.desc())
            .first()
        )
        if not last_msg or not last_msg.content:
            return {"type": "text", "content": "No previous response to translate."}
        return await self._translate_text(last_msg.content, language)

    @staticmethod
    def _parse_language(arg: str) -> str:
        """'spanish' / 'to spanish' / '' → 'Spanish' / 'Spanish' / 'English'."""
        lang = (arg or "").strip()
        if lang.lower().startswith("to "):
            lang = lang[3:].strip()
        return (lang or "English").title()

    async def _translate_text(self, text: str, language: str, *, kind: str = "text") -> dict:
        """Translate `text` into `language`, raising the output budget so long content
        isn't cut off. `kind` labels the prompt ('text' / 'web page text'). Shared by the
        last-response, URL and attachment translate paths."""
        messages = [
            {"role": "system", "content": (
                f"Translate the following {kind} to {language}. Translate ALL of it — every "
                "line and list item — do not summarize, omit, or stop early. Preserve the "
                "original line breaks and formatting. Output only the translation.")},
            {"role": "user", "content": (text or "")[:24000]},
        ]
        # Output is about as long as the input; the default ~2048 cap stops long pages early.
        _orig_np = self.chat_service.num_predict
        self.chat_service.num_predict = max(_orig_np, 8192)
        try:
            translation = await self.chat_service.chat(messages)
            return {"type": "text", "content": f"## Translation ({language})\n\n{translation}"}
        except Exception as e:
            logger.error(f"Translation error: {e}")
            return {"type": "text", "content": f"Translation failed: {str(e)}"}
        finally:
            self.chat_service.num_predict = _orig_np

    async def _translate_url(self, arg: str, url: str) -> dict:
        """Fetch a web page's text and translate the whole thing (no OCR).

        `translate <url>` (→ English) or `translate <url> to <language>`.
        """
        language = self._parse_language(arg.replace(url, ""))
        try:
            fetched = await self.search_service.fetch_urls([url], max_urls=1)
        except Exception as e:
            return {"type": "text", "content": f"Couldn't fetch {url}: {e}"}
        if not fetched or fetched[0].get("error") or not fetched[0].get("content"):
            err = (fetched[0].get("error") if fetched else None) or "no readable text found"
            return {"type": "text", "content": f"Couldn't fetch text from {url}: {err}"}
        title = fetched[0].get("title", "")
        body = (f"Title: {title}\n\n" if title else "") + fetched[0]["content"]
        return await self._translate_text(body, language, kind="web page text")

    async def _translate_attachments(self, arg: str, attachments: list) -> dict:
        """OCR uploaded image(s)/PDF(s) and translate the FULL extracted text.

        Shared by the web UI, Telegram and Matrix (`translate <lang>` + an upload).
        Returns an `error: 'no_text'` field when nothing could be extracted (e.g. a
        Telegram-compressed photo) so callers can show a tailored hint.
        """
        import base64 as _b64
        from app.services.document_service import extract_image_text, extract_pdf_text
        from app.services.media_service import is_image, is_pdf

        language = self._parse_language(arg)
        parts = []
        for fn, data, ct in attachments:
            try:
                b64 = _b64.b64encode(data).decode()
            except Exception:
                continue
            if is_pdf(fn, ct):
                parts.append(extract_pdf_text(b64) or "")
            elif is_image(fn, ct):
                parts.append(extract_image_text(b64) or "")
        src = "\n\n".join(p for p in parts if p).strip()
        if not src:
            return {"type": "text", "error": "no_text",
                    "content": "Couldn't extract any text to translate from the upload."}
        return await self._translate_text(src, language)

    async def _compress_command(self, attachments: Optional[list]) -> dict:
        """Compress attached image(s) or video(s) and return the smaller files."""
        if not attachments:
            return {
                "type": "text",
                "content": "Attach an image or video, then send `compress` to shrink it.",
            }
        import asyncio
        from app.services.media_service import compress_attachments

        # ffmpeg transcodes can block; run off the event loop.
        outputs, summary = await asyncio.to_thread(compress_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _convert_command(self, arg: str, attachments: Optional[list]) -> dict:
        """Convert attached image(s) to a PDF, or a PDF to images."""
        if not attachments:
            return {
                "type": "text",
                "content": (
                    "Attach file(s) then send `convert`:\n"
                    "- image(s) → a single PDF\n"
                    "- a PDF → one PNG per page"
                ),
            }
        import asyncio
        from app.services.media_service import convert_attachments

        outputs, summary = await asyncio.to_thread(convert_attachments, attachments, arg)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _4chan_command(self, arg: str) -> dict:
        """Open 4chan catalog browser. Optional board: g, pol, a, or h."""
        allowed_boards = ("g", "pol", "a", "h")
        board = (arg or "g").strip().lower()
        if board not in allowed_boards:
            board = "g"
        return {
            "type": "4chan",
            "content": f"Opening 4chan /{board}/ catalog.",
            "board": board,
        }


def get_command_service(db: Session) -> CommandService:
    return CommandService(db)
