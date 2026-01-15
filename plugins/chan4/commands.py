"""
4chan Plugin Commands

Command handlers for the `4chan` and `4chang` commands.
"""
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import User
    from sqlalchemy.orm import Session

from plugins.chan4.service import (
    fetch_all_front_page_posts,
    fetch_board_catalog,
    format_catalog_results,
    format_posts_results,
)

logger = logging.getLogger(__name__)


async def handle_chan4_command(arg: str, user: "User", db: "Session") -> dict:
    """
    Handle the `4chan` or `4chang` command.
    
    Usage:
        - 4chan <board> - Browse board catalog (e.g., 4chan g, 4chan pol)
        - 4chang <board> - Alias for 4chan
    """
    if not arg or not arg.strip():
        return {
            "type": "text",
            "content": """## 4chan Board Browser

**Usage:** `4chan <board>` or `4chang <board>`

**Popular boards:**
- `4chan g` - Technology
- `4chan pol` - Politically Incorrect  
- `4chan b` - Random
- `4chan a` - Anime & Manga
- `4chan v` - Video Games
- `4chan mu` - Music
- `4chan tv` - Television & Film

**Examples:**
- `4chan g` - Browse /g/ catalog
- `4chang pol` - Browse /pol/ catalog

All requests are routed through Tor proxy for privacy."""
        }
    
    board = arg.strip().lower()
    
    try:
        # Fetch catalog threads (faster and more reliable than fetching all posts)
        threads = await fetch_board_catalog(board, limit=20)
        
        if not threads:
            # Provide more helpful error message with troubleshooting
            return {
                "type": "text",
                "content": f"""No threads found on /{board}/. 

**Possible causes:**
1. Proxy connection issue - verify proxy is working
2. Board may be temporarily unavailable
3. Network/connectivity issue
4. 4chan may be blocking requests

**Troubleshooting:**
- Check application logs for detailed error messages
- Verify proxy is configured in Admin Settings → Site Settings → BitTorrent Client → HTTP Proxy Host
- Try a different board (e.g., `4chan b` for Random)
- Ensure Tor and HTTP proxy are running

**Check logs for:**
- "Fetching 4chan /{board}/catalog via proxy: ..."
- "Found X thread links in HTML"
- Any HTTP error codes or connection errors"""
            }
        
        # Collect images from thread thumbnails
        images = []
        for thread in threads:
            if thread.image_url:
                images.append(thread.image_url)
            elif thread.thumbnail_url:
                # Use thumbnail if full image not available
                images.append(thread.thumbnail_url)
        
        # Format catalog results
        formatted = format_catalog_results(threads, board)
        
        # Return with images if available
        if images:
            return {
                "type": "images",
                "content": formatted,
                "images": images
            }
        else:
            return {
                "type": "text",
                "content": formatted
            }
    
    except ValueError as e:
        # Proxy requirement error
        if "proxy" in str(e).lower():
            return {
                "type": "text",
                "content": f"{str(e)}\n\nConfigure proxy in Admin → Site Settings → BitTorrent Client → HTTP Proxy Host"
            }
        raise
    except Exception as e:
        logger.error(f"Error in 4chan command: {e}", exc_info=True)
        error_msg = str(e)
        # Provide more helpful error message
        if "No threads found" in error_msg or "empty or unavailable" in error_msg:
            return {
                "type": "text",
                "content": f"""No threads found on /{board}/. 

**Possible causes:**
1. Proxy connection issue - check if proxy is working
2. Board may be temporarily unavailable
3. Network/connectivity issue

**Debug steps:**
- Check application logs for detailed error messages
- Verify proxy is configured correctly in Admin Settings
- Try a different board (e.g., `4chan b` for Random)

**Error details:** {error_msg}"""
            }
        return {
            "type": "text",
            "content": f"Error fetching /{board}/ catalog: {error_msg}\n\nCheck application logs for more details."
        }
