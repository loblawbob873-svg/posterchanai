import json
import re
import requests
from config import SEARXNG_URL, OPENAI_ENDPOINT, OPENAI_API_KEY, MODEL
from core.utils import is_safe_url

SEARXNG_TIMEOUT = 15  # seconds


def search_web(query, limit=5, categories=None, time_range=None):
    """
    Search the web using SearXNG.

    Args:
        query: Search query string
        limit: Maximum number of results (default 5)
        categories: optional SearXNG category (e.g. "news", "videos", "science")
        time_range: optional SearXNG time filter ("day", "week", "month", "year")

    Returns:
        List of dicts with keys: url, title, content
    """
    if not SEARXNG_URL:
        print("ERROR: SEARXNG_URL not configured")
        return []

    try:
        search_url = f"{SEARXNG_URL}/search"
        params = {
            "q": query,
            "format": "json",
            "language": "en",
            "safesearch": "0"  # Disable safe search filter
        }
        if categories:
            params["categories"] = categories
        if time_range:
            params["time_range"] = time_range

        scope = f" [{categories}]" if categories else ""
        print(f"[SearXNG] Web search ({len(query or '')} chars){scope}")
        response = requests.get(search_url, params=params, timeout=SEARXNG_TIMEOUT)
        response.raise_for_status()

        data = response.json()
        results = data.get("results", [])

        # Return top results with required fields
        return [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("content", "")
            }
            for r in results[:limit]
            if r.get("url")
        ]

    except requests.exceptions.Timeout:
        print(f"[SearXNG] Timeout on a {len(query or '')}-char web search")
        return []
    except requests.exceptions.RequestException as e:
        print(f"[SearXNG] Request error: {e}")
        return []
    except json.JSONDecodeError as e:
        print(f"[SearXNG] JSON decode error: {e}")
        return []


# Maps a SearXNG category to the trigger words that imply it.
# Order matters: earlier categories win when multiple match.
_CATEGORY_KEYWORDS = {
    "news": ("news", "headline", "headlines", "breaking", "latest on",
             "press release", "current events"),
    "files": ("torrent", "torrents", "magnet", "iso", "download",
              "downloads", "files"),
    "videos": ("video", "videos", "clip", "clips", "footage", "trailer"),
    "music": ("song", "songs", "lyrics", "album", "music"),
    "science": ("paper", "papers", "study", "studies", "research",
                "journal", "publication", "preprint"),
    "it": ("github", "stackoverflow", "stack overflow", "source code",
           "repository", "pip package", "npm package", "man page"),
    "map": ("map", "directions", "near me", "nearby", "route to"),
    "social media": ("reddit", "subreddit", "tweet", "tweets", "mastodon",
                     "lemmy", "hacker news"),
}

# Time-range trigger words -> SearXNG time_range value.
_TIME_KEYWORDS = {
    "day": ("today", "past 24 hours", "last 24 hours", "past day"),
    "week": ("this week", "past week", "last week"),
    "month": ("this month", "past month", "last month"),
    "year": ("this year", "past year", "last year"),
}


def detect_search_intent(query):
    """Heuristically infer the SearXNG category and time range from a natural
    query. Returns (clean_query, categories, time_range).

    The trigger word is kept in the query (SearXNG ranks fine with it, and
    stripping risks dropping meaningful terms) — only the filters are derived.
    """
    lowered = f" {query.lower()} "

    categories = None
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(f" {kw} " in lowered for kw in keywords):
            categories = category
            break

    time_range = None
    for tr, keywords in _TIME_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            time_range = tr
            break

    return query.strip(), categories, time_range


def smart_search(query, limit=5):
    """Detect intent from a natural query, run a targeted SearXNG search, and
    fall back to a plain search if the targeted one comes up empty.

    Returns (results, categories) so callers can show the detected scope.
    """
    clean_query, categories, time_range = detect_search_intent(query)
    results = search_web(clean_query, limit=limit, categories=categories, time_range=time_range)
    # A category/time filter can leave too few results to summarize well — broaden
    # to a plain search when the targeted one comes up sparse.
    if len(results) < 3 and (categories or time_range):
        plain = search_web(clean_query, limit=limit)
        if len(plain) > len(results):
            results, categories = plain, None
    return results, categories


def search_images(query, limit=10):
    """
    Search for images using SearXNG.

    Args:
        query: Search query string
        limit: Maximum number of results (default 10)

    Returns:
        List of dicts with keys: url (image URL), title, source (page URL)
    """
    if not SEARXNG_URL:
        print("ERROR: SEARXNG_URL not configured")
        return []

    try:
        search_url = f"{SEARXNG_URL}/search"
        params = {
            "q": query,
            "format": "json",
            "categories": "images",
            "language": "en",
            "safesearch": "0"  # Disable safe search filter
        }

        print(f"[SearXNG] Image search ({len(query or '')} chars)")
        response = requests.get(search_url, params=params, timeout=SEARXNG_TIMEOUT)
        response.raise_for_status()

        data = response.json()
        results = data.get("results", [])

        # Return top results with image-specific fields
        return [
            {
                "url": r.get("img_src", r.get("url", "")),
                "title": r.get("title", ""),
                "source": r.get("url", "")
            }
            for r in results[:limit]
            if r.get("img_src") or r.get("url")
        ]

    except requests.exceptions.Timeout:
        print(f"[SearXNG] Timeout on a {len(query or '')}-char image search")
        return []
    except requests.exceptions.RequestException as e:
        print(f"[SearXNG] Request error: {e}")
        return []
    except json.JSONDecodeError as e:
        print(f"[SearXNG] JSON decode error: {e}")
        return []


def summarize_search_results(results, query, categories=None):
    """
    Use AI to write one cohesive summary of the search results, then list the
    sources separately (matches the PosterChanAI Telegram search style).

    Args:
        results: List of search results from search_web()/smart_search()
        query: Original search query
        categories: optional detected SearXNG category, shown in the scope line

    Returns:
        Formatted string: an AI prose summary followed by a "Sources:" list.
        Falls back to a plain titles+URLs list if AI summarization fails.
    """
    if not results:
        return f'No results found for "{query}".'

    # Sources block: plain title + bare URL so links auto-render on every
    # platform (Pleroma).
    sources = "\n\n".join(
        f"{i+1}. {r['title']}\n{r['url']}"
        for i, r in enumerate(results)
    )
    sources_block = f"\n\nSources:\n{sources}"

    # Build context for the AI: numbered title + url + snippet, like PosterChanAI.
    scope = f" ({categories})" if categories else ""
    context = f"Search results for '{query}'{scope}:\n\n"
    for i, r in enumerate(results, 1):
        snippet = r['content'][:300] if r.get('content') else ''
        context += f"{i}. {r['title']}\n{r['url']}\n{snippet}\n\n"

    # Try AI summarization if configured
    if OPENAI_ENDPOINT and OPENAI_ENDPOINT.startswith(("http://", "https://")):
        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENAI_API_KEY}",
            }

            payload = {
                "model": MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"You are a helpful assistant that summarizes web search results about: \"{query}\".\n"
                            "Summarize ONLY the information contained in the search results provided. "
                            "Stay strictly on the topic of the query. Do NOT introduce facts, companies, "
                            "people, or topics (e.g. unrelated cryptocurrencies or products) that are not "
                            "present in the results. If the results don't cover the topic, say so. "
                            "Be concise and highlight key information. Do not list the sources or URLs. "
                            "No thinking tags."
                        )
                    },
                    {
                        "role": "user",
                        "content": context
                    }
                ],
                "temperature": 0.2,
                "max_tokens": 600
            }

            print(f"[SearXNG] Requesting AI summarization for {len(results)} results")
            response = requests.post(
                OPENAI_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=60
            )
            response.raise_for_status()

            data = response.json()
            ai_response = ""

            if "choices" in data and len(data["choices"]) > 0:
                ai_response = data["choices"][0].get("message", {}).get("content", "")
            elif "response" in data:
                ai_response = data["response"]

            if ai_response:
                # Strip thinking tags if present (handles unclosed tags too)
                ai_response = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', ai_response, flags=re.IGNORECASE | re.DOTALL)
                ai_response = re.sub(r'<think(?:ing)?>.*$', '', ai_response, flags=re.IGNORECASE | re.DOTALL)
                if re.search(r'</think(?:ing)?>', ai_response, re.IGNORECASE):
                    ai_response = re.split(r'(?i)</think(?:ing)?>', ai_response)[-1]
                ai_response = ai_response.strip()

                if ai_response:
                    return ai_response + sources_block

        except Exception as e:
            print(f"[SearXNG] AI summarization failed: {e}")

    # Fallback: query header + titles with URLs (plain text, URLs auto-link)
    return f"{query}:{sources_block}"


def format_image_results(results, query):
    """
    Format image search results as clickable markdown links.

    Args:
        results: List of image results from search_images()
        query: Original search query

    Returns:
        Formatted string with clickable image links
    """
    if not results:
        return f'No images found for "{query}".'

    formatted = [
        f"{i+1}. {r['title'] or 'Image'}\n{r['url']}"
        for i, r in enumerate(results)
    ]

    return f'Images for "{query}":\n\n' + "\n\n".join(formatted)


def detect_image_type(image_bytes):
    """Detect image type from magic bytes. Returns (extension, mime_type) or (None, None)."""
    if not image_bytes or len(image_bytes) < 8:
        return None, None
    
    # Check magic bytes
    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png', 'image/png'
    elif image_bytes[:2] == b'\xff\xd8':
        return 'jpg', 'image/jpeg'
    elif image_bytes[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif', 'image/gif'
    elif image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
        return 'webp', 'image/webp'
    elif image_bytes[:4] == b'\x00\x00\x00\x0c' and image_bytes[4:8] == b'jP  ':
        return 'jp2', 'image/jp2'
    else:
        # Default to PNG
        return 'png', 'image/png'


def download_image(url, timeout=30):
    """
    Download an image from a URL and return its bytes.

    Args:
        url: Image URL to download
        timeout: Request timeout in seconds

    Returns:
        Tuple of (image_bytes, mime_type) if successful, (None, None) otherwise
    """
    if not url or not is_safe_url(url):
        return None, None

    try:
        print(f"[SearXNG] Downloading image: {url[:100]}...")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=timeout, stream=True)
        response.raise_for_status()

        # Check content type is an image
        content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            print(f"[SearXNG] Not an image: {content_type}")
            return None, None

        # Limit size to 10MB
        max_size = 10 * 1024 * 1024
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > max_size:
                    print(f"[SearXNG] Image too large: {content_length} bytes")
                    return None, None
            except ValueError:
                pass  # Invalid Content-Length header, continue with download

        image_bytes = response.content
        # Double-check actual size (Content-Length may be missing or wrong)
        if len(image_bytes) > max_size:
            print(f"[SearXNG] Downloaded image too large: {len(image_bytes)} bytes")
            return None, None
        
        # Detect actual image type from bytes
        ext, mime = detect_image_type(image_bytes)
        print(f"[SearXNG] Downloaded {len(image_bytes)} bytes, type={mime}")
        return image_bytes, mime

    except requests.exceptions.Timeout:
        print(f"[SearXNG] Timeout downloading image: {url[:100]}")
        return None, None
    except requests.exceptions.RequestException as e:
        print(f"[SearXNG] Error downloading image: {e}")
        return None, None


def search_and_download_images(query, max_images=4):
    """
    Search for images and download the top results.

    Args:
        query: Search query string
        max_images: Maximum number of images to download (default 4)

    Returns:
        Tuple of (text_response, list_of_tuples) where each tuple is (image_bytes, mime_type)
    """
    print(f"[SearXNG] search_and_download_images called with a {len(query or '')}-char query, "
          f"max_images={max_images}")
    results = search_images(query, limit=max_images * 2)  # Get extra in case some fail

    if not results:
        print(f"[SearXNG] No search results found")
        return f'No images found for "{query}".', []

    print(f"[SearXNG] Found {len(results)} image results, downloading up to {max_images}...")
    downloaded = []
    for i, result in enumerate(results):
        if len(downloaded) >= max_images:
            break

        url = result.get("url")
        print(f"[SearXNG] Attempting download {i+1}: {url[:80] if url else 'None'}...")
        image_bytes, mime_type = download_image(url)
        if image_bytes:
            print(f"[SearXNG] Successfully downloaded image {i+1}: {len(image_bytes)} bytes, mime={mime_type}")
            downloaded.append((image_bytes, mime_type))
        else:
            print(f"[SearXNG] Failed to download image {i+1}")

    print(f"[SearXNG] Download complete: {len(downloaded)} images downloaded")

    if not downloaded:
        # Fallback to text links if all downloads fail
        return format_image_results(results[:max_images], query), []

    return f'Found {len(downloaded)} images for "{query}"', downloaded
