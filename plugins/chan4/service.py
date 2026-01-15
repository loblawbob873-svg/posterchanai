"""
4chan board catalog service
Fetches board catalog and parses thread data with images
"""
import httpx
import logging
import json
import re
from typing import Optional, List, Tuple
from dataclasses import dataclass
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

CHAN4_BASE_URL = "https://boards.4chan.org"

# Popular boards
BOARDS = {
    "g": "Technology",
    "b": "Random",
    "pol": "Politically Incorrect",
    "a": "Anime & Manga",
    "v": "Video Games",
    "vg": "Video Game Generals",
    "mu": "Music",
    "tv": "Television & Film",
    "fit": "Fitness",
    "sci": "Science & Math",
    "lit": "Literature",
    "his": "History",
    "x": "Paranormal",
    "sp": "Sports",
    "news": "News",
    "int": "International",
    "out": "Outdoors",
}


@dataclass
class Chan4Thread:
    """Represents a 4chan thread"""
    thread_id: int
    board: str
    subject: str
    comment: str
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    replies: int = 0
    images: int = 0
    thread_url: str = ""


@dataclass
class Chan4Post:
    """Represents a 4chan post"""
    post_id: int
    thread_id: int
    board: str
    subject: str
    comment: str
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    name: str = ""
    timestamp: str = ""


# Use shared proxy utility
from app.services.proxy_utils import require_proxy


async def fetch_thread_posts(board: str, thread_id: int, proxy_config: str) -> List[Chan4Post]:
    """Fetch all posts from a specific thread"""
    thread_url = f"{CHAN4_BASE_URL}/{board}/thread/{thread_id}.json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": f"{CHAN4_BASE_URL}/{board}/",
    }
    
    posts = []
    try:
        # httpx 0.28.1 uses 'proxy' (string) not 'proxies' (dict)
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            proxy=proxy_config
        ) as client:
            response = await client.get(thread_url, headers=headers)
            response.raise_for_status()
            thread_data = response.json()
            
            # Extract posts from thread
            posts_data = thread_data.get("posts", [])
            for post_data in posts_data:
                try:
                    post_id = post_data.get("no", 0)
                    subject = post_data.get("sub", "")
                    comment = post_data.get("com", "")
                    
                    # Clean HTML from comment
                    if comment:
                        soup = BeautifulSoup(comment, "html.parser")
                        comment = soup.get_text()
                    
                    # Get image info
                    image_url = None
                    thumbnail_url = None
                    if "tim" in post_data and "ext" in post_data:
                        tim = post_data["tim"]
                        ext = post_data["ext"]
                        # Direct image URL: /board/tim.ext
                        image_url = f"{CHAN4_BASE_URL}/{board}/{tim}{ext}"
                        # Thumbnail URL: /board/thumb/tims.jpg
                        thumbnail_url = f"{CHAN4_BASE_URL}/{board}/thumb/{tim}s.jpg"
                    
                    name = post_data.get("name", "Anonymous")
                    timestamp = post_data.get("now", "")
                    
                    posts.append(Chan4Post(
                        post_id=post_id,
                        thread_id=thread_id,
                        board=board,
                        subject=subject,
                        comment=comment,
                        image_url=image_url,
                        thumbnail_url=thumbnail_url,
                        name=name,
                        timestamp=timestamp
                    ))
                except Exception as e:
                    logger.debug(f"Error parsing post: {e}")
                    continue
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error fetching thread /{board}/thread/{thread_id}: {e.response.status_code} - {e.response.text[:200] if hasattr(e.response, 'text') else ''}")
        return []
    except httpx.RequestError as e:
        logger.error(f"Request error fetching thread /{board}/thread/{thread_id}: {e}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error for thread /{board}/thread/{thread_id}: {e}")
        return []
    except Exception as e:
        logger.error(f"Error fetching thread /{board}/thread/{thread_id}: {e}", exc_info=True)
        return []
    
    if posts:
        logger.debug(f"Fetched {len(posts)} posts from thread {thread_id} on /{board}/")
    else:
        logger.warning(f"No posts found in thread {thread_id} on /{board}/ (thread may be deleted or empty)")
    return posts


async def fetch_board_catalog(board: str, limit: int = 20) -> List[Chan4Thread]:
    """
    Fetch board catalog from 4chan
    
    Args:
        board: Board name (e.g., 'g', 'pol', 'b')
        limit: Maximum number of threads to return
    
    Returns:
        List of Chan4Thread objects
    """
    board = board.lower().strip()
    
    if board not in BOARDS:
        # Allow any board name, just warn
        logger.info(f"Unknown board: {board}, fetching anyway")
    
    # Use HTML catalog endpoint (more reliable than JSON)
    catalog_url = f"{CHAN4_BASE_URL}/{board}/catalog"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": f"{CHAN4_BASE_URL}/",
    }
    
    threads = []
    # Proxy is REQUIRED for 4chan (privacy/security)
    proxy_config = require_proxy("4chan access")
    
    # Verify proxy_config is a string (httpx 0.28.1 uses 'proxy' parameter with string URL)
    if not isinstance(proxy_config, str):
        logger.error(f"Invalid proxy_config type: {type(proxy_config)}, expected str. Value: {proxy_config}")
        raise ValueError(f"Proxy configuration must be a string URL, got {type(proxy_config)}")
    
    try:
        logger.info(f"Fetching 4chan /{board}/catalog via proxy: {proxy_config}")
        # httpx 0.28.1 uses 'proxy' (string) not 'proxies' (dict)
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            proxy=proxy_config
        ) as client:
            # Try JSON first, fallback to HTML
            json_url = f"{CHAN4_BASE_URL}/{board}/catalog.json"
            try:
                json_response = await client.get(json_url, headers=headers)
                json_response.raise_for_status()
                json_text = json_response.text
                content_type = json_response.headers.get("content-type", "").lower()
                
                # Only use JSON if it's actually JSON
                if "application/json" in content_type and not json_text.strip().startswith("<"):
                    logger.info(f"Using JSON catalog: status={json_response.status_code}, length={len(json_text)}")
                    try:
                        catalog_data = json.loads(json_text)
                        all_threads = {}
                        
                        if isinstance(catalog_data, dict):
                            # Iterate through pages
                            for page_key, page_data in catalog_data.items():
                                if isinstance(page_data, dict):
                                    page_threads = page_data.get("threads", {})
                                    if not page_threads and all(isinstance(k, str) and k.isdigit() for k in list(page_data.keys())[:5]):
                                        page_threads = page_data
                                    if isinstance(page_threads, dict):
                                        all_threads.update(page_threads)
                        
                        logger.info(f"Found {len(all_threads)} threads in catalog JSON for /{board}/")
                        
                        # Parse threads from JSON
                        for thread_id_str, thread_data in list(all_threads.items())[:limit]:
                            try:
                                thread_id = int(thread_id_str)
                                subject = thread_data.get("sub", "") or thread_data.get("teaser", "")[:100]
                                comment = thread_data.get("com", "") or ""
                                if comment:
                                    soup = BeautifulSoup(comment, "html.parser")
                                    comment = soup.get_text()[:200]
                                
                                image_url = None
                                thumbnail_url = None
                                if "tim" in thread_data and "ext" in thread_data:
                                    tim = thread_data["tim"]
                                    ext = thread_data["ext"]
                                    image_url = f"{CHAN4_BASE_URL}/{board}/{tim}{ext}"
                                    thumbnail_url = f"{CHAN4_BASE_URL}/{board}/thumb/{tim}s.jpg"
                                
                                replies = thread_data.get("replies", 0)
                                images = thread_data.get("images", 0)
                                thread_url = f"{CHAN4_BASE_URL}/{board}/thread/{thread_id}".strip()
                                
                                threads.append(Chan4Thread(
                                    thread_id=thread_id,
                                    board=board,
                                    subject=subject or comment[:50] or f"Thread {thread_id}",
                                    comment=comment,
                                    image_url=image_url,
                                    thumbnail_url=thumbnail_url,
                                    replies=replies,
                                    images=images,
                                    thread_url=thread_url
                                ))
                            except (ValueError, KeyError, TypeError) as e:
                                logger.debug(f"Error parsing thread {thread_id_str}: {e}")
                                continue
                        
                        if threads:
                            logger.info(f"Successfully parsed {len(threads)} threads from catalog JSON for /{board}/")
                            return threads[:limit]
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse JSON catalog: {e}")
            except Exception as e:
                logger.debug(f"JSON catalog failed: {e}, falling back to HTML")
            
            # Fallback to HTML catalog
            logger.info(f"Using HTML catalog endpoint")
            response = await client.get(catalog_url, headers=headers)
            response.raise_for_status()
            html = response.text
            logger.info(f"HTML catalog response: status={response.status_code}, length={len(html)}")
        
            # Check if we got an error page
            if "404" in html or "not found" in html.lower() or "error" in html.lower()[:500]:
                logger.warning(f"Possible error page received. HTML preview: {html[:500]}")
            
            # Parse HTML catalog
            soup = BeautifulSoup(html, "lxml")
            
            logger.info(f"Parsing HTML catalog for /{board}/, HTML length: {len(html)}")
            
            # Log a sample of the HTML to see structure
            if len(html) > 0:
                logger.debug(f"HTML preview (first 1000 chars): {html[:1000]}")
            
            # Check if page title indicates an error
            title = soup.find("title")
            if title:
                title_text = title.get_text()
                logger.info(f"Page title: {title_text}")
                if "404" in title_text or "not found" in title_text.lower() or "error" in title_text.lower():
                    raise ValueError(f"Board /{board}/ not found or unavailable (404 error)")
            
            # Find thread containers - 4chan uses various class names
            # Try multiple selectors to find threads
            thread_divs = []
            
            # Method 1: Standard thread divs
            divs1 = soup.find_all("div", class_="thread")
            if divs1:
                logger.info(f"Found {len(divs1)} threads via 'div.thread'")
                thread_divs.extend(divs1)
            
            # Method 2: Board thread class
            if not thread_divs:
                divs2 = soup.find_all("div", class_="boardThread")
                if divs2:
                    logger.info(f"Found {len(divs2)} threads via 'div.boardThread'")
                    thread_divs.extend(divs2)
            
            # Method 3: Data attribute
            if not thread_divs:
                divs3 = soup.find_all("div", {"data-thread-id": True})
                if divs3:
                    logger.info(f"Found {len(divs3)} threads via 'data-thread-id'")
                    thread_divs.extend(divs3)
            
            # Method 4: ID pattern
            if not thread_divs:
                divs4 = soup.find_all("div", id=re.compile(r'thread'))
                if divs4:
                    logger.info(f"Found {len(divs4)} threads via 'id=thread*'")
                    thread_divs.extend(divs4)
            
            # Method 5: Find by links to threads (most reliable fallback)
            if not thread_divs:
                thread_links = soup.find_all("a", href=re.compile(r'/\w+/thread/\d+'))
                logger.info(f"Found {len(thread_links)} thread links in HTML")
                
                seen_thread_ids = set()
                for link in thread_links:
                    href = link.get("href", "")
                    match = re.search(r'/(\w+)/thread/(\d+)', href)
                    if match:
                        thread_id = int(match.group(2))
                        if thread_id in seen_thread_ids:
                            continue
                        seen_thread_ids.add(thread_id)
                        
                        # Try to find parent container
                        parent = link.find_parent("div", class_=re.compile("thread|post"))
                        if not parent:
                            parent = link.find_parent("div")
                        if not parent:
                            parent = link.find_parent("article")
                        if not parent:
                            parent = link.find_parent("li")
                        
                        if parent:
                            thread_divs.append(parent)
                        else:
                            # Create a minimal container from the link itself
                            thread_divs.append(link)
            
            logger.info(f"Total found: {len(thread_divs)} thread containers in HTML")
            
            # If we still have no thread divs, try extracting directly from links
            if not thread_divs:
                logger.warning("No thread containers found, trying direct link extraction")
                thread_links = soup.find_all("a", href=re.compile(r'/\w+/thread/\d+'))
                logger.info(f"Found {len(thread_links)} thread links for direct extraction")
                
                seen_thread_ids = set()
                for link in thread_links[:limit]:
                    href = link.get("href", "")
                    match = re.search(r'/(\w+)/thread/(\d+)', href)
                    if match:
                        thread_id = int(match.group(2))
                        if thread_id in seen_thread_ids:
                            continue
                        seen_thread_ids.add(thread_id)
                        
                        # Extract basic info from link text
                        link_text = link.get_text(strip=True)
                        subject = link_text[:100] if link_text else f"Thread {thread_id}"
                        
                        thread_url = f"{CHAN4_BASE_URL}/{board}/thread/{thread_id}"
                        
                        threads.append(Chan4Thread(
                            thread_id=thread_id,
                            board=board,
                            subject=subject,
                            comment="",
                            image_url=None,
                            thumbnail_url=None,
                            replies=0,
                            images=0,
                            thread_url=thread_url
                        ))
                
                if threads:
                    logger.info(f"Extracted {len(threads)} threads directly from links")
                    return threads[:limit]
            
            for div in thread_divs[:limit]:
                try:
                    # Extract thread ID from data attribute, link, or div ID
                    thread_id = None
                    
                    # Try data-thread-id attribute first
                    if div.get("data-thread-id"):
                        thread_id = int(div.get("data-thread-id"))
                    # Try div ID like "thread_12345678"
                    elif div.get("id"):
                        id_match = re.search(r'thread[_-]?(\d+)', div.get("id", ""))
                        if id_match:
                            thread_id = int(id_match.group(1))
                    
                    # Fallback to finding link
                    if not thread_id:
                        thread_link = div.find("a", href=True)
                        if thread_link:
                            href = thread_link.get("href", "")
                            # Extract thread ID from href like "/g/thread/12345678"
                            match = re.search(r'/thread/(\d+)', href)
                            if match:
                                thread_id = int(match.group(1))
                    
                    if not thread_id:
                        logger.debug(f"Could not extract thread ID from div")
                        continue
                    
                    # Get subject - try multiple selectors
                    subject = ""
                    for selector in ["span.subject", "span.fileText", ".subject", "h3", "h4"]:
                        subject_elem = div.select_one(selector) if hasattr(div, 'select_one') else div.find(selector.split('.')[-1] if '.' in selector else selector)
                        if subject_elem:
                            subject = subject_elem.get_text(strip=True)
                            break
                    
                    # Get comment/preview - try multiple selectors
                    comment = ""
                    for selector in ["blockquote", "div.postMessage", ".postMessage", ".comment", "p"]:
                        comment_elem = div.select_one(selector) if hasattr(div, 'select_one') else div.find(selector.split('.')[-1] if '.' in selector else selector)
                        if comment_elem:
                            comment = comment_elem.get_text(strip=True)[:200]
                            break
                    
                    # Get thumbnail - try multiple selectors
                    thumbnail = None
                    for selector in ["img.thumb", "img[src*='thumb']", "img"]:
                        thumb_elem = div.select_one(selector) if hasattr(div, 'select_one') else div.find("img")
                        if thumb_elem:
                            thumbnail = thumb_elem
                            break
                    
                    thumbnail_url = None
                    if thumbnail:
                        thumbnail_url = thumbnail.get("src", "") or thumbnail.get("data-src", "")
                        if thumbnail_url and not thumbnail_url.startswith("http"):
                            if thumbnail_url.startswith("//"):
                                thumbnail_url = f"https:{thumbnail_url}"
                            elif thumbnail_url.startswith("/"):
                                thumbnail_url = f"{CHAN4_BASE_URL}{thumbnail_url}"
                    
                    # Get image URL from thumbnail
                    image_url = None
                    if thumbnail:
                        parent_link = thumbnail.find_parent("a", href=True)
                        if parent_link:
                            img_href = parent_link.get("href", "")
                            if img_href:
                                if img_href.startswith("//"):
                                    image_url = f"https:{img_href}"
                                elif img_href.startswith("/"):
                                    image_url = f"{CHAN4_BASE_URL}{img_href}"
                                else:
                                    image_url = img_href
                    
                    # Get reply/image counts
                    replies = 0
                    images = 0
                    count_elem = div.find("span", class_="replyCount") or div.find("span", class_="omittedPosts")
                    if count_elem:
                        text = count_elem.get_text()
                        match = re.search(r'(\d+)', text)
                        if match:
                            replies = int(match.group(1))
                    
                    thread_url = f"{CHAN4_BASE_URL}/{board}/thread/{thread_id}"
                    
                    threads.append(Chan4Thread(
                        thread_id=thread_id,
                        board=board,
                        subject=subject or comment[:50] or f"Thread {thread_id}",
                        comment=comment,
                        image_url=image_url,
                        thumbnail_url=thumbnail_url,
                        replies=replies,
                        images=images,
                        thread_url=thread_url
                    ))
                except Exception as e:
                    logger.debug(f"Error parsing thread div: {e}")
                    continue
            
            logger.info(f"Successfully parsed {len(threads)} threads from HTML catalog for /{board}/")
    
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error fetching 4chan catalog: {e.response.status_code}")
        if e.response.status_code == 404:
            raise ValueError(f"Board /{board}/ not found. Check board name.")
        elif e.response.status_code == 403:
            raise ValueError(f"Access denied to /{board}/. The board may be restricted.")
        raise ValueError(f"HTTP error {e.response.status_code} fetching /{board}/ catalog")
    except httpx.RequestError as e:
        logger.error(f"Request error fetching 4chan catalog: {e}")
        raise ValueError(f"Failed to connect to 4chan. Check proxy configuration.")
    except Exception as e:
        logger.error(f"Error fetching 4chan catalog: {e}")
        raise
    
    logger.info(f"Returning {len(threads[:limit])} threads from fetch_board_catalog for /{board}/")
    return threads[:limit]


async def fetch_all_front_page_posts(board: str, limit: int = 20) -> Tuple[List[Chan4Post], List[str]]:
    """
    Fetch all posts from front page threads
    
    Returns:
        Tuple of (posts list, images list for display)
    """
    # Proxy is required
    proxy_config = require_proxy("4chan access")
    
    # First get catalog to find front page threads
    threads = await fetch_board_catalog(board, limit=limit)
    
    if not threads:
        logger.warning(f"No threads found in catalog for /{board}/")
        return [], []
    
    logger.info(f"Found {len(threads)} threads in catalog for /{board}/, fetching posts...")
    
    # Fetch all posts from each thread
    all_posts = []
    all_images = []
    
    import asyncio
    
    async def fetch_thread_posts_wrapper(thread: Chan4Thread):
        posts = await fetch_thread_posts(thread.board, thread.thread_id, proxy_config)
        return posts
    
    # Fetch all threads in parallel
    tasks = [fetch_thread_posts_wrapper(thread) for thread in threads]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Collect all posts and images
    successful_threads = 0
    failed_threads = 0
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.warning(f"Error fetching thread {threads[i].thread_id} posts: {result}", exc_info=True)
            failed_threads += 1
            continue
        if not result:
            logger.debug(f"Thread {threads[i].thread_id} returned no posts")
            failed_threads += 1
            continue
        successful_threads += 1
        for post in result:
            all_posts.append(post)
            if post.image_url:
                all_images.append(post.image_url)
    
    logger.info(f"Fetched {len(all_posts)} total posts from {successful_threads}/{len(threads)} threads on /{board}/ ({failed_threads} failed)")
    
    # If we got threads but no posts, log a warning
    if threads and not all_posts:
        logger.warning(f"Found {len(threads)} threads but 0 posts. This may indicate an issue fetching individual thread data.")
    
    return all_posts, all_images


def format_catalog_results(threads: List[Chan4Thread], board: str) -> str:
    """Format 4chan catalog results for display"""
    if not threads:
        board_name = BOARDS.get(board, board.upper())
        return f"No threads found on /{board}/ ({board_name}). The board may be empty or unavailable."
    
    board_name = BOARDS.get(board, board.upper())
    lines = [f"## ◈ /{board}/ - {board_name} ◈\n"]
    
    for i, thread in enumerate(threads, 1):
        # Truncate long subjects/comments
        subject = thread.subject[:60] + "..." if len(thread.subject) > 60 else thread.subject
        comment = thread.comment[:100] + "..." if len(thread.comment) > 100 else thread.comment
        
        # Escape brackets for markdown
        subject_escaped = subject.replace("[", "(").replace("]", ")")
        comment_escaped = comment.replace("[", "(").replace("]", ")")
        
        # Build display
        thread_display = f"**{i}. {subject_escaped}**"
        if comment_escaped:
            thread_display += f"\n   {comment_escaped}"
        
        # Add image thumbnail if available
        if thread.thumbnail_url:
            thread_display += f"\n   ![Thumb]({thread.thumbnail_url})"
        
        # Add thread link - markdown format will be converted to clickable <a> tag with target="_blank"
        # Ensure link is on its own line for proper parsing
        thread_display += f"\n[Visit Thread]({thread.thread_url})"
        
        # Add stats
        stats = []
        if thread.replies > 0:
            stats.append(f"R:{thread.replies}")
        if thread.images > 0:
            stats.append(f"I:{thread.images}")
        if stats:
            thread_display += f" | {' '.join(stats)}"
        
        lines.append(thread_display + "\n")
    
    return "\n".join(lines)


def format_posts_results(posts: List[Chan4Post], board: str) -> str:
    """Format 4chan posts for display"""
    if not posts:
        board_name = BOARDS.get(board, board.upper())
        return f"No posts found on /{board}/ ({board_name})."
    
    board_name = BOARDS.get(board, board.upper())
    lines = [f"## ◈ /{board}/ - {board_name} - All Front Page Posts ◈\n"]
    
    for i, post in enumerate(posts, 1):
        # Truncate long subjects/comments
        subject = post.subject[:60] + "..." if len(post.subject) > 60 else post.subject if post.subject else ""
        comment = post.comment[:200] + "..." if len(post.comment) > 200 else post.comment
        
        # Escape brackets for markdown
        subject_escaped = subject.replace("[", "(").replace("]", ")")
        comment_escaped = comment.replace("[", "(").replace("]", ")")
        
        # Build display
        post_display = f"**{i}. "
        if subject_escaped:
            post_display += f"{subject_escaped}**"
        else:
            post_display += f"Post #{post.post_id}**"
        
        if post.name:
            post_display += f" - {post.name}"
        if post.timestamp:
            post_display += f" ({post.timestamp})"
        
        if comment_escaped:
            post_display += f"\n   {comment_escaped}"
        
        # Add thread link - use proper markdown format
        # 4chan thread URL format: https://boards.4chan.org/board/thread/threadid#postid
        # Construct URL ensuring no extra whitespace
        thread_url = f"{CHAN4_BASE_URL}/{board}/thread/{post.thread_id}#p{post.post_id}"
        # Markdown link will be converted to: <a href="..." target="_blank">View in Thread</a>
        # Ensure link is on its own line for proper parsing
        post_display += f"\n[View in Thread]({thread_url})"
        
        lines.append(post_display + "\n")
    
    return "\n".join(lines)
