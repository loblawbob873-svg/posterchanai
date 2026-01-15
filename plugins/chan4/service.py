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
    
    # Realistic browser headers - match latest Chrome exactly
    # These headers are critical for bypassing Cloudflare
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Cache-Control": "max-age=0",
        "DNT": "1",
    }
    
    threads = []
    # Proxy is REQUIRED for 4chan (privacy/security)
    try:
        proxy_config = require_proxy("4chan access")
    except ValueError as e:
        logger.error(f"Proxy requirement failed: {e}")
        raise
    
    # Verify proxy_config is a string (httpx 0.28.1 uses 'proxy' parameter with string URL)
    if not isinstance(proxy_config, str):
        logger.error(f"Invalid proxy_config type: {type(proxy_config)}, expected str. Value: {proxy_config}")
        raise ValueError(f"Proxy configuration must be a string URL, got {type(proxy_config)}")
    
    logger.info(f"Fetching 4chan /{board}/catalog via proxy: {proxy_config}")
    
    try:
        # httpx 0.28.1 uses 'proxy' (string) not 'proxies' (dict)
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            proxy=proxy_config
        ) as client:
            # Skip connectivity test - go straight to catalog to avoid extra requests that might trigger blocking
            
            # Skip main page visit - go straight to catalog like browser would
            # Browsers don't always visit main page first when you navigate directly to /g/catalog
            headers["Referer"] = f"{CHAN4_BASE_URL}/"
            
            # Try JSON first, fallback to HTML
            json_url = f"{CHAN4_BASE_URL}/{board}/catalog.json"
            try:
                json_response = await client.get(json_url, headers=headers)
                
                # Check for blocking
                if json_response.status_code == 403:
                    logger.error(f"4chan returned 403 Forbidden for {json_url}. Response headers: {dict(json_response.headers)}")
                    # Try without some headers that might trigger detection
                    logger.info("Retrying with minimal headers...")
                    minimal_headers = {
                        "User-Agent": headers["User-Agent"],
                        "Accept": "application/json, text/html, */*",
                    }
                    json_response = await client.get(json_url, headers=minimal_headers)
                    if json_response.status_code == 403:
                        raise ValueError(f"4chan is blocking requests (HTTP 403). The site may be blocking proxy/Tor connections. If you can access 4chan in your browser through the proxy, check that the proxy configuration matches.")
                
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
            
            # Fallback to HTML catalog - use exact same approach as browser
            logger.info(f"Using HTML catalog endpoint: {catalog_url}")
            
            # Try with full browser headers first
            response = await client.get(catalog_url, headers=headers)
            logger.info(f"Catalog response: status={response.status_code}, length={len(response.text)}, cookies={len(client.cookies)}")
            
            # Check for blocking
            if response.status_code == 403:
                logger.error(f"4chan returned 403 Forbidden for {catalog_url}")
                logger.error(f"Response headers: {dict(response.headers)}")
                logger.error(f"Response preview: {response.text[:500]}")
                raise ValueError(f"4chan returned 403 Forbidden. If this works in your browser with the same proxy, the request headers may be different. Check logs for details.")
            
            response.raise_for_status()
            html = response.text
            logger.info(f"HTML catalog response: status={response.status_code}, length={len(html)}, content-type: {response.headers.get('content-type', 'unknown')}")
            
            # Quick check: if HTML is very short, might be an error page
            if len(html) < 500:
                logger.error(f"HTML response is suspiciously short ({len(html)} chars). Content: {html[:1000]}")
                # Check if it's a specific error message
                if "cloudflare" in html.lower():
                    raise ValueError(f"4chan is using Cloudflare protection. The site may be blocking proxy/Tor connections.")
                elif "access denied" in html.lower() or "blocked" in html.lower():
                    raise ValueError(f"4chan is blocking access. The site may be blocking proxy/Tor connections.")
                else:
                    raise ValueError(f"Received suspiciously short response from 4chan ({len(html)} chars). May be blocked or error page. Response: {html[:500]}")
        
            # Check if we got an error page
            if "404" in html or "not found" in html.lower() or "error" in html.lower()[:500]:
                logger.warning(f"Possible error page received. HTML preview: {html[:500]}")
            
            # Check for embedded JSON in script tags (4chan sometimes embeds catalog data)
            script_tags = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
            if script_tags:
                logger.info(f"Found {len(script_tags)} script tags in HTML, checking for embedded JSON...")
                for i, script_content in enumerate(script_tags[:5]):  # Check first 5 scripts
                    # Look for JSON-like structures
                    if '"threads"' in script_content or '"catalog"' in script_content or f'"/{board}/thread/' in script_content:
                        logger.info(f"Script tag #{i} contains thread/catalog data (length: {len(script_content)})")
                        # Try to extract JSON from script
                        json_match = re.search(r'(\{[^{}]*"threads"[^{}]*\})', script_content, re.DOTALL)
                        if json_match:
                            try:
                                embedded_json = json.loads(json_match.group(1))
                                logger.info(f"Found embedded JSON in script tag #{i}")
                                # Process embedded JSON similar to catalog.json
                                # (This would need similar parsing logic as the JSON catalog section)
                            except json.JSONDecodeError:
                                pass
            
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
            
            # Check HTML content for thread indicators BEFORE parsing
            thread_indicators = ["/thread/", f"/{board}/thread/", "thread", "data-thread", "catalog"]
            found_indicators = [ind for ind in thread_indicators if ind in html or ind.lower() in html.lower()]
            if found_indicators:
                logger.info(f"HTML contains thread indicators: {found_indicators}")
            else:
                logger.warning("HTML does not contain obvious thread indicators - may be wrong page or Cloudflare challenge")
            
            # Log HTML sample for debugging
            logger.info(f"HTML sample (first 2000 chars): {html[:2000]}")
            logger.info(f"HTML sample (last 1000 chars): {html[-1000:]}")
            
            # Check for Cloudflare or blocking indicators
            if "cf-browser-verification" in html.lower() or "challenge-platform" in html.lower() or "just a moment" in html.lower():
                logger.error("Cloudflare protection detected in HTML")
                raise ValueError("Cloudflare protection detected. The site may be blocking proxy/Tor connections.")
            
            # PRIMARY METHOD: Search raw HTML with regex FIRST (most reliable - works regardless of HTML structure)
            logger.info(f"Searching raw HTML ({len(html)} chars) with regex for thread URLs...")
            
            # Try multiple regex patterns to find thread URLs
            # 4chan uses /board/thread/ID format in URLs
            patterns = [
                (r'/(\w+)/thread/(\d+)', 'standard'),  # /g/thread/12345678
                (r'/thread/(\d+)', 'no-board'),  # /thread/12345678 (if board is in URL already)
                (r'thread[_-]?(\d+)', 'underscore'),  # thread_12345678 or thread-12345678
                (r'href=["\']([^"\']*/(\w+)/thread/(\d+)[^"\']*)["\']', 'href-with-board'),  # href="/g/thread/12345678"
                (r'data-thread-id=["\']?(\d+)["\']?', 'data-attr'),  # data-thread-id="12345678"
                (r'id=["\']thread[_-]?(\d+)["\']', 'id-attr'),  # id="thread12345678"
                (r'class=["\'][^"\']*thread[^"\']*["\']', 'thread-class'),  # class="thread" or "boardThread"
            ]
            
            regex_matches = []
            for pattern, name in patterns:
                try:
                    matches = re.findall(pattern, html, re.IGNORECASE)
                    if matches:
                        logger.info(f"✓ Pattern '{name}' ({pattern}) found {len(matches)} matches")
                        # Show first few matches for debugging
                        if len(matches) > 0:
                            logger.info(f"  First 5 matches: {matches[:5]}")
                    else:
                        logger.info(f"✗ Pattern '{name}' found 0 matches")
                except Exception as e:
                    logger.warning(f"Error with pattern '{name}': {e}")
                    continue
                
                if matches:
                    
                    if name == 'standard':
                        regex_matches = matches
                        logger.info(f"✓ Using standard pattern with {len(regex_matches)} matches")
                        break
                    elif name == 'href-with-board':
                        # Extract (board, thread_id) from href matches - matches are tuples like (full_href, board, thread_id)
                        try:
                            regex_matches = [(m[1], m[2]) if isinstance(m, tuple) and len(m) >= 3 else (board, str(m)) for m in matches]
                            logger.info(f"✓ Using href pattern with {len(regex_matches)} matches")
                            break
                        except (IndexError, TypeError) as e:
                            logger.warning(f"Error parsing href matches: {e}, matches: {matches[:3]}")
                            continue
                    elif name == 'no-board' and not regex_matches:
                        # Convert to (board, thread_id) format using current board
                        regex_matches = [(board, str(tid)) for tid in matches]
                        logger.info(f"✓ Using no-board pattern with {len(regex_matches)} matches (using board: {board})")
                        break
                    elif name == 'data-attr' and not regex_matches:
                        # Convert to (board, thread_id) format
                        regex_matches = [(board, str(tid)) for tid in matches]
                        logger.info(f"✓ Using data-attr pattern with {len(regex_matches)} matches (using board: {board})")
                        break
                else:
                    logger.info(f"✗ Pattern '{name}' found 0 matches")
            
            logger.info(f"Total found: {len(regex_matches)} thread URLs via regex in raw HTML")
            
            # If no matches, try a very simple pattern - just find any numbers that might be thread IDs
            if not regex_matches:
                logger.warning("No regex matches found with standard patterns, trying simple number search...")
                # Look for patterns like /g/12345678 or similar
                simple_patterns = [
                    (r'/{}/?(\d{{8,}})'.format(board), 'board-number'),  # /g/12345678 (8+ digits)
                    (r'/{}/thread/?(\d+)'.format(board), 'board-thread-simple'),  # /g/thread12345678
                    (r'threads?["\']?\s*:\s*(\d+)', 'json-thread'),  # JSON format: "thread": 12345678
                    (r'"no"\s*:\s*(\d+)', 'json-no'),  # JSON format: "no": 12345678
                ]
                for pattern, name in simple_patterns:
                    try:
                        matches = re.findall(pattern, html, re.IGNORECASE)
                        if matches:
                            logger.info(f"Simple pattern '{name}' found {len(matches)} matches: {matches[:5]}")
                            regex_matches = [(board, str(tid)) for tid in matches[:limit]]
                            break
                    except Exception as e:
                        logger.warning(f"Error with simple pattern '{name}': {e}")
                        continue
                
                # If still no matches, check if HTML contains the board name at all
                if not regex_matches:
                    if f"/{board}/" in html:
                        logger.warning(f"HTML contains '/{board}/' but no thread URLs found - may be Cloudflare challenge page")
                        # Try to find any numeric IDs that might be thread IDs (8+ digits)
                        all_numbers = re.findall(r'\b(\d{8,})\b', html)
                        if all_numbers:
                            logger.info(f"Found {len(all_numbers)} potential thread IDs (8+ digits): {all_numbers[:10]}")
                    else:
                        logger.warning(f"HTML does not contain '/{board}/' - may be wrong page")
                        # Log what the page actually contains
                        if "json" in html.lower()[:500]:
                            logger.info("HTML appears to contain JSON - may need JSON parsing")
                        if "<script" in html.lower()[:1000]:
                            logger.info("HTML contains scripts - may be JavaScript-rendered content")
            
            # If we found matches but they're in wrong format, try to fix
            if regex_matches and isinstance(regex_matches[0], str):
                # Single string matches - convert to (board, thread_id)
                logger.warning(f"Regex matches are in wrong format, converting...")
                regex_matches = [(board, str(m)) for m in regex_matches]
            
            # Use regex matches if we found any (most reliable method)
            if regex_matches:
                logger.info(f"Using {len(regex_matches)} thread URLs found via regex")
                # Create minimal thread objects from regex matches
                seen_thread_ids = set()
                for board_match, thread_id_str in regex_matches[:limit]:
                    try:
                        thread_id = int(thread_id_str)
                        if thread_id in seen_thread_ids:
                            continue
                        seen_thread_ids.add(thread_id)
                        
                        thread_url = f"{CHAN4_BASE_URL}/{board}/thread/{thread_id}"
                        threads.append(Chan4Thread(
                            thread_id=thread_id,
                            board=board,
                            subject=f"Thread {thread_id}",
                            comment="",
                            image_url=None,
                            thumbnail_url=None,
                            replies=0,
                            images=0,
                            thread_url=thread_url
                        ))
                    except ValueError:
                        continue
                
                if threads:
                    logger.info(f"Extracted {len(threads)} threads from regex pattern matching")
                    return threads[:limit]
            
            # Fallback: Find by links to threads using BeautifulSoup
            thread_links = soup.find_all("a", href=re.compile(r'/\w+/thread/\d+'))
            logger.info(f"Found {len(thread_links)} thread links in HTML via BeautifulSoup")
            
            # Always try extracting from links as primary method (most reliable)
            seen_thread_ids = set()
            link_threads = []
            
            for link in thread_links:
                href = link.get("href", "")
                match = re.search(r'/(\w+)/thread/(\d+)', href)
                if match:
                    thread_id = int(match.group(2))
                    if thread_id in seen_thread_ids:
                        continue
                    seen_thread_ids.add(thread_id)
                    
                    # Try to find parent container for more info
                    parent = None
                    for parent_selector in [
                        lambda: link.find_parent("div", class_=re.compile("thread|post")),
                        lambda: link.find_parent("div"),
                        lambda: link.find_parent("article"),
                        lambda: link.find_parent("li"),
                    ]:
                        try:
                            parent = parent_selector()
                            if parent:
                                break
                        except:
                            pass
                    
                    link_threads.append((thread_id, parent or link, href))
            
            logger.info(f"Extracted {len(link_threads)} unique thread IDs from links")
            
            # If we have link-based threads, use those
            if link_threads:
                thread_divs = [item[1] for item in link_threads]
                logger.info(f"Using {len(thread_divs)} threads from link extraction")
            
            logger.info(f"Total found: {len(thread_divs)} thread containers in HTML")
            
            # If we found no containers and no links, log the HTML structure for debugging
            if not thread_divs and not link_threads and not regex_matches and not threads:
                logger.error(f"No thread containers, links, or regex matches found. HTML structure analysis:")
                logger.error(f"HTML length: {len(html)}")
                
                # Check if HTML contains thread-related content
                html_lower = html.lower()
                if "/thread/" in html:
                    logger.warning("HTML contains '/thread/' but regex didn't match - checking pattern...")
                    # Try different regex patterns
                    patterns = [
                        r'/thread/(\d+)',
                        r'thread[_-]?(\d+)',
                        r'/(\w+)/thread/(\d+)',
                        r'threads?[_-]?(\d+)',
                    ]
                    for pattern in patterns:
                        matches = re.findall(pattern, html, re.IGNORECASE)
                        if matches:
                            logger.warning(f"Pattern '{pattern}' found {len(matches)} matches: {matches[:5]}")
                
                # Check for common HTML elements
                body = soup.find("body")
                if body:
                    logger.error(f"Body found, contains {len(body.find_all())} elements")
                    # Look for any divs
                    all_divs = soup.find_all("div")
                    logger.error(f"Total divs in page: {len(all_divs)}")
                    if all_divs:
                        # Show classes of first 20 divs
                        div_classes = [div.get("class") for div in all_divs[:20]]
                        logger.error(f"First 20 div classes: {div_classes}")
                    # Check for any links at all
                    all_links = soup.find_all("a", href=True)
                    logger.error(f"Total links in page: {len(all_links)}")
                    if all_links:
                        # Show first 10 link hrefs that might be threads
                        thread_related = [link.get("href", "") for link in all_links if "thread" in link.get("href", "").lower()][:10]
                        logger.error(f"Thread-related links (first 10): {thread_related}")
                        # Show first 10 link hrefs overall
                        link_hrefs = [link.get("href", "")[:50] for link in all_links[:10]]
                        logger.error(f"First 10 link hrefs: {link_hrefs}")
                else:
                    logger.error("No body tag found in HTML")
                
                # Log a sample of the HTML to see what we got
                logger.error(f"HTML sample (first 3000 chars): {html[:3000]}")
                # Also check middle and end
                if len(html) > 6000:
                    logger.error(f"HTML sample (middle 3000 chars): {html[len(html)//2-1500:len(html)//2+1500]}")
                    logger.error(f"HTML sample (last 3000 chars): {html[-3000:]}")
            
            # If we still have no thread divs but have link_threads, extract minimal info
            if not thread_divs and link_threads:
                logger.warning("No thread containers found, extracting minimal thread info from links")
                seen_thread_ids = set()
                for thread_id, link_elem, href in link_threads[:limit]:
                    if thread_id in seen_thread_ids:
                        continue
                    seen_thread_ids.add(thread_id)
                    
                    # Extract basic info from link text or nearby elements
                    link_text = ""
                    if hasattr(link_elem, 'get_text'):
                        link_text = link_elem.get_text(strip=True)[:100]
                    elif hasattr(link_elem, 'text'):
                        link_text = link_elem.text[:100] if link_elem.text else ""
                    
                    subject = link_text if link_text else f"Thread {thread_id}"
                    
                    # Try to find thumbnail in nearby elements
                    thumbnail_url = None
                    if hasattr(link_elem, 'find'):
                        img = link_elem.find("img")
                        if img:
                            thumbnail_url = img.get("src", "") or img.get("data-src", "")
                            if thumbnail_url and not thumbnail_url.startswith("http"):
                                if thumbnail_url.startswith("//"):
                                    thumbnail_url = f"https:{thumbnail_url}"
                                elif thumbnail_url.startswith("/"):
                                    thumbnail_url = f"{CHAN4_BASE_URL}{thumbnail_url}"
                    
                    thread_url = f"{CHAN4_BASE_URL}/{board}/thread/{thread_id}"
                    
                    threads.append(Chan4Thread(
                        thread_id=thread_id,
                        board=board,
                        subject=subject,
                        comment="",
                        image_url=None,
                        thumbnail_url=thumbnail_url,
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
            
            if not threads:
                logger.error(f"No threads found after parsing HTML. HTML length: {len(html)}, checking for error indicators...")
                # Check for common error indicators
                if "cloudflare" in html.lower() or "cf-ray" in html.lower():
                    logger.error("Cloudflare protection detected - may need different approach")
                if len(html) < 1000:
                    logger.warning(f"HTML response is very short ({len(html)} chars), might be an error page")
                    logger.warning(f"HTML content: {html[:500]}")
    
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error fetching 4chan catalog: {e.response.status_code}")
        logger.error(f"Response text preview: {e.response.text[:500] if hasattr(e.response, 'text') else 'N/A'}")
        if e.response.status_code == 404:
            raise ValueError(f"Board /{board}/ not found. Check board name.")
        elif e.response.status_code == 403:
            raise ValueError(f"Access denied to /{board}/. The board may be restricted or blocked.")
        elif e.response.status_code == 503:
            raise ValueError(f"4chan service unavailable (503). The site may be down or blocking requests.")
        raise ValueError(f"HTTP error {e.response.status_code} fetching /{board}/ catalog. Check proxy configuration and network connectivity.")
    except httpx.RequestError as e:
        logger.error(f"Request error fetching 4chan catalog: {e}")
        raise ValueError(f"Failed to connect to 4chan. Check proxy configuration and ensure proxy is running.")
    except ValueError as e:
        # Re-raise proxy requirement errors
        if "proxy" in str(e).lower():
            raise
        logger.error(f"ValueError fetching 4chan catalog: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error fetching 4chan catalog: {e}", exc_info=True)
        raise ValueError(f"Error fetching /{board}/ catalog: {str(e)}. Check logs for details.")
    
    logger.info(f"Returning {len(threads[:limit])} threads from fetch_board_catalog for /{board}/")
    
    if not threads:
        # Log detailed diagnostic information
        logger.error(f"DIAGNOSTIC: No threads found for /{board}/")
        logger.error(f"  - Proxy config: {proxy_config}")
        logger.error(f"  - Catalog URL: {catalog_url}")
        logger.error(f"  - This indicates either: proxy not working, 4chan blocking, or parsing failed")
    
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
