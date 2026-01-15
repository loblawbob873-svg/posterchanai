from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
import httpx
import logging
from bs4 import BeautifulSoup
from urllib.parse import urlparse, quote

from app.database import get_db
from app.models import User, Setting
from app.auth import get_current_user
from app.services.inference_factory import get_inference_service, prepare_vram_for_llm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news", tags=["news"])


def get_news_sources(db: Session) -> str:
    """Get news sources from settings"""
    setting = db.query(Setting).filter(Setting.key == "news_sources").first()
    return setting.value if setting and setting.value else ""


async def fetch_headlines_from_url(url: str) -> dict:
    """Fetch and extract headlines from a news site URL"""
    if not url.startswith("http"):
        url = f"https://{url}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "lxml")
            for el in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
                el.decompose()

            parsed = urlparse(url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"

            links = []
            seen = set()
            skip = [
                'trending', 'live updates', 'breaking', 'watch', 'listen',
                'subscribe', 'sign in', 'log in', 'login', 'sign up', 'register',
                'menu', 'search', 'more', 'read more', 'see more', 'show more',
                'click here', 'learn more', 'advertisement', 'sponsored', 'ad:',
                'shop ', 'buy ', 'sale', 'discount', 'coupon', 'promo',
                'delivery', 'shipping', 'cart', 'checkout', 'order',
                'advertise', 'contact us', 'about us', 'privacy', 'terms',
                'cookie', 'newsletter', 'print edition', 'e-edition', 'app',
                'facebook', 'twitter', 'instagram', 'youtube', 'tiktok',
                'share', 'comment', 'reply', 'follow us', 'connect',
                'great gifts', 'home delivery', 'editions',
            ]

            for a in soup.find_all('a', href=True):
                text = ' '.join(a.get_text(separator=' ', strip=True).split())
                href = a['href']

                if not text or len(text) < 20 or href.startswith('#'):
                    continue
                # Skip if mostly emojis
                clean_text = ''.join(c for c in text if not c in '🎁📱💰🔥⚡️✨')
                if len(clean_text.strip()) < 15:
                    continue
                if text.lower() in seen or any(s in text.lower() for s in skip):
                    continue

                seen.add(text.lower())
                if href.startswith('/'):
                    href = base_url + href
                elif not href.startswith('http'):
                    continue

                # Add link
                links.append(f"- [{text}]({href})")
                if len(links) >= 12:
                    break

            return {"links": links, "error": None}

        except Exception as e:
            logger.warning(f"Error fetching {url}: {e}")
            return {"links": [], "error": str(e)}


async def summarize_with_ai(links: list, db: Session) -> str:
    """Use native inference service to create clickable summaries"""
    def log(msg):
        with open("/tmp/news_debug.log", "a") as f:
            f.write(f"{msg}\n")
    
    try:
        log(f"Starting AI summarization for {len(links)} headlines")
        prepare_vram_for_llm(db)
        service = get_inference_service(db)
        log(f"Got inference service: {type(service).__name__ if service else 'None'}")
        
        if service is None:
            log("ERROR: No inference service available")
            return None

        messages = [
            {"role": "system", "content": """Summarize each news headline in 1 sentence.
IMPORTANT: You MUST preserve the exact markdown link format [title](url) for each item.
Output as a bullet list starting with "- ".
Example input: - [Biden announces new policy](https://example.com/article)
Example output: - [Biden unveils initiative affecting millions](https://example.com/article)
No extra text or commentary."""},
            {"role": "user", "content": "\n".join(links)}
        ]

        log("Calling chat_completion...")
        result = await service.chat_completion(
            messages=messages,
            temperature=0.3,
            max_tokens=2048
        )
        log(f"chat_completion returned: {list(result.keys()) if isinstance(result, dict) else type(result)}")

        if "error" in result:
            log(f"ERROR from AI: {result['error']}")
            return None

        content = result["choices"][0]["message"]["content"]
        if content:
            from app.services.text_utils import strip_thinking_tags
            summary = strip_thinking_tags(content.strip())
            log(f"AI summarization successful, {len(summary)} chars")
            return summary
        else:
            log("WARNING: AI returned empty content")

    except Exception as e:
        log(f"EXCEPTION: {e}")
        import traceback
        log(traceback.format_exc())

    return None


async def fetch_news_from_source(source_url: str, source_name: str, db: Session) -> str:
    """Fetch news with AI summaries - max 10"""
    def log(msg):
        with open("/tmp/news_debug.log", "a") as f:
            f.write(f"{msg}\n")
    
    log(f"Fetching from: {source_url}")
    result = await fetch_headlines_from_url(source_url)
    links = result["links"][:10]
    log(f"Got {len(links)} headlines from {source_name}")

    if not links:
        return f"**{source_name}:** Could not fetch headlines. {result.get('error', '')}"

    # Use AI to summarize
    log("Calling summarize_with_ai...")
    ai_result = await summarize_with_ai(links, db)
    log(f"summarize_with_ai returned: {'Success' if ai_result else 'None'}")
    
    if ai_result:
        # Add extra spacing between articles for better TUI readability
        spaced_result = ai_result.replace("\n- ", "\n\n- ")
        return f"**{source_name}:**\n\n{spaced_result}"

    # Fallback: raw links with spacing
    log(f"Using raw links fallback for {source_name}")
    return f"**{source_name}:**\n\n" + "\n\n".join(links)


def parse_news_sources(raw: str) -> list:
    """Parse news sources from settings"""
    default = [
        {"url": "drudgereport.com", "name": "Drudge Report"},
        {"url": "npr.org/sections/news", "name": "NPR"},
        {"url": "nypost.com", "name": "NY Post"},
        {"url": "foxnews.com", "name": "Fox News"},
        {"url": "newsweek.com", "name": "Newsweek"},
    ]

    if not raw or not raw.strip():
        return default

    sources = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if "|" in line:
            url, name = line.split("|", 1)
            sources.append({"url": url.strip(), "name": name.strip()})
        elif line:
            sources.append({"url": line, "name": line})

    return sources if sources else default


def get_user_news_sources(user: User, db: Session) -> list:
    """Get news sources - user's custom sources, or admin setting, or defaults"""
    # First check user's custom sources
    if user.news_sources and user.news_sources.strip():
        return parse_news_sources(user.news_sources)
    # Fall back to admin setting
    admin_sources = get_news_sources(db)
    return parse_news_sources(admin_sources)


@router.get("/sources")
async def get_sources(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get the list of news sources for current user"""
    sources = get_user_news_sources(current_user, db)
    return {"sources": sources}


@router.get("/headlines/{source_url:path}")
async def get_headlines(
    source_url: str,
    conversation_id: int = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get news headlines from a source with AI summaries"""
    # Multiple ways to ensure we see the log
    logger.warning(f"[NEWS] === get_headlines called for: {source_url} ===")
    print(f"[NEWS] === get_headlines called for: {source_url} ===", flush=True)
    
    # Also write to a file for debugging
    with open("/tmp/news_debug.log", "a") as f:
        f.write(f"get_headlines called for: {source_url}\n")
    from app.models import Conversation, Message

    sources = get_user_news_sources(current_user, db)

    source_name = source_url
    for s in sources:
        if s["url"] == source_url or source_url in s["url"]:
            source_name = s["name"]
            break

    print(f"[NEWS] Calling fetch_news_from_source for {source_name}")
    markdown = await fetch_news_from_source(source_url, source_name, db)
    print(f"[NEWS] Got markdown response: {len(markdown)} chars")

    # Save to conversation if provided
    if conversation_id:
        conversation = db.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        ).first()
        if conversation:
            message = Message(
                conversation_id=conversation_id,
                role="assistant",
                content=markdown
            )
            db.add(message)
            db.commit()

    return {"markdown": markdown}


@router.get("/all")
async def get_all_headlines(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get headlines from all sources"""
    sources = get_user_news_sources(current_user, db)

    results = []
    for source in sources:
        markdown = await fetch_news_from_source(source["url"], source["name"], db)
        results.append(markdown)

    return {"markdown": "\n\n---\n\n".join(results)}


@router.post("/miniflux/sync")
async def sync_miniflux_news(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Manually trigger Miniflux news sync for current user"""
    from app.services.miniflux_scheduler import process_miniflux_news_for_user
    from app.services.miniflux_service import MinifluxService

    # Check if user has Miniflux enabled and configured
    if not current_user.miniflux_enabled:
        return {"success": False, "error": "Miniflux is not enabled for your account"}

    miniflux = MinifluxService.from_settings(db, current_user)
    if not miniflux:
        return {"success": False, "error": "Miniflux credentials not configured"}

    try:
        # Test connection first
        entries = await miniflux.get_unread_entries(limit=1)
        if entries is None:
            return {"success": False, "error": "Failed to connect to Miniflux - check your credentials"}

        # Run the sync
        await process_miniflux_news_for_user(current_user.id)
        return {"success": True, "message": "Miniflux sync completed"}
    except Exception as e:
        logger.error(f"Manual Miniflux sync failed: {e}")
        return {"success": False, "error": str(e)}


@router.get("/miniflux/status")
async def get_miniflux_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get Miniflux configuration status for current user"""
    from app.services.miniflux_service import MinifluxService

    # Check global setting
    global_enabled = db.query(Setting).filter(Setting.key == "miniflux_enabled").first()
    global_enabled = global_enabled and global_enabled.value.lower() == "true"

    # Check user setting
    user_enabled = current_user.miniflux_enabled
    has_credentials = bool(
        current_user.miniflux_url and
        current_user.miniflux_username and
        current_user.miniflux_password
    )

    return {
        "global_enabled": global_enabled,
        "user_enabled": user_enabled,
        "has_credentials": has_credentials,
        "miniflux_url": current_user.miniflux_url or "",
        "configured": global_enabled and user_enabled and has_credentials
    }


@router.get("/summarize")
async def summarize_article(
    url: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Summarize a specific article URL"""
    import httpx
    import json
    import re
    from bs4 import BeautifulSoup

    try:
        # Fetch the article with full browser-like headers
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            html = response.text

        # Parse and extract main content
        soup = BeautifulSoup(html, "html.parser")

        # Log response info for debugging
        logger.info(f"Article fetch: {url} - status={response.status_code}, content_length={len(html)}")

        # Try to extract from JSON-LD (common in JS-heavy sites like MSN)
        text = ""
        json_ld_scripts = soup.find_all("script", type="application/ld+json")
        for script in json_ld_scripts:
            try:
                data = json.loads(script.string)
                # Handle array of objects
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") in ["NewsArticle", "Article", "WebPage"]:
                            data = item
                            break
                if isinstance(data, dict):
                    # Extract article body from JSON-LD
                    body = data.get("articleBody") or data.get("description") or ""
                    if body and len(body) > 200:
                        text = body
                        logger.info(f"Extracted {len(text)} chars from JSON-LD")
                        break
            except (json.JSONDecodeError, TypeError):
                continue

        # Also try extracting from embedded JavaScript data (MSN-style)
        if not text:
            # Look for __NEXT_DATA__ or similar
            next_data = soup.find("script", id="__NEXT_DATA__")
            if next_data:
                try:
                    data = json.loads(next_data.string)
                    # Navigate through common Next.js structures
                    props = data.get("props", {}).get("pageProps", {})
                    article = props.get("article") or props.get("content") or props.get("story") or {}
                    body = article.get("body") or article.get("content") or article.get("text") or ""
                    if body and len(body) > 200:
                        text = body
                        logger.info(f"Extracted {len(text)} chars from __NEXT_DATA__")
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass

        # Only do HTML extraction if JSON-LD didn't find content
        if not text:
            # Remove scripts, styles, nav, footer, ads
            for el in soup(["script", "style", "nav", "footer", "aside", "header", "form", "noscript", "iframe"]):
                el.decompose()

            # Try to find article content in common containers (expanded list)
            article = None
            selectors = [
                ("article", {}),
                (None, {"class_": ["article", "post", "content", "story", "entry", "article-body", "post-content", "entry-content", "article-content", "story-body"]}),
                (None, {"id": ["article", "content", "main-content", "article-body", "story"]}),
                ("main", {}),
                (None, {"role": "main"}),
                (None, {"class_": ["body", "text", "article-text"]}),
            ]

            for tag, attrs in selectors:
                if tag:
                    article = soup.find(tag, **attrs) if attrs else soup.find(tag)
                else:
                    article = soup.find(**attrs)
                if article:
                    logger.info(f"Found article content using: tag={tag}, attrs={attrs}")
                    break

            if article:
                text = article.get_text(separator="\n", strip=True)
            else:
                # Fallback to body
                logger.info("No article container found, falling back to body")
                body = soup.find("body")
                text = body.get_text(separator="\n", strip=True) if body else ""

        # Clean up and limit text
        lines = [line.strip() for line in text.split("\n") if line.strip() and len(line.strip()) > 10]
        text = "\n".join(lines[:150])  # Max 150 lines

        logger.info(f"Extracted text length: {len(text)} chars, {len(lines)} lines")

        if len(text) < 50:
            logger.warning(f"Article extraction failed - only {len(text)} chars extracted from {url}")
            # Try more aggressive extraction - just get all text
            body = soup.find("body")
            if body:
                raw_text = body.get_text(separator=" ", strip=True)
                # Remove excessive whitespace
                import re
                raw_text = re.sub(r'\s+', ' ', raw_text).strip()
                if len(raw_text) > 200:
                    text = raw_text[:8000]
                    logger.info(f"Fallback extraction got {len(text)} chars")
                else:
                    return {"summary": f"Could not extract article content. HTML length: {len(html)}. Site may use JavaScript rendering."}

        # Get AI service - use inference factory (same as news headlines)
        prepare_vram_for_llm(db)
        service = get_inference_service(db)

        # Summarize with AI
        messages = [
            {"role": "system", "content": "Summarize this article in 2-3 paragraphs. Focus on the key facts and main points."},
            {"role": "user", "content": text[:8000]}  # Limit context
        ]

        result = await service.chat_completion(
            messages=messages,
            temperature=0.3,
            max_tokens=1024
        )

        if "error" in result:
            return {"summary": f"AI error: {result['error']}"}

        content = result["choices"][0]["message"]["content"]
        if content:
            from app.services.text_utils import strip_thinking_tags
            return {"summary": strip_thinking_tags(content.strip())}

        return {"summary": "Could not generate summary."}

    except Exception as e:
        logger.error(f"Article summarization failed: {e}")
        return {"summary": f"Error: {str(e)}"}
