from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
import httpx
import logging
from bs4 import BeautifulSoup
from urllib.parse import urlparse

from app.database import get_db
from app.models import User
from app.services import settings_store
from app.auth import get_current_user
from app.services.inference_factory import get_inference_service, prepare_vram_for_llm
from app.services.proxy_utils import require_proxy

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news", tags=["news"])


def get_news_sources(db: Session) -> str:
    """Get news sources from settings"""
    setting = settings_store.get("news_sources")
    return setting if setting else ""


def _parse_rss_feed(raw: bytes, base_url: str) -> tuple:
    """Parse RSS or Atom feed XML from raw bytes. Returns (links, error_str)."""
    import xml.etree.ElementTree as ET
    links = []
    try:
        # Strip UTF-8 BOM bytes if present — ET handles encoding declarations in bytes mode
        if raw.startswith(b'\xef\xbb\xbf'):
            raw = raw[3:]
        root = ET.fromstring(raw)

        ns_strip = lambda tag: tag.split('}', 1)[-1] if '}' in tag else tag

        items = []
        for item in root.iter():
            if ns_strip(item.tag) in ('item', 'entry'):
                items.append(item)

        logger.info(f"RSS: found {len(items)} items")

        seen = set()
        for item in items[:12]:
            title = ''
            link = ''
            for child in item:
                tag = ns_strip(child.tag)
                if tag == 'title' and not title:
                    title = (child.text or '').strip()
                elif tag == 'link' and not link:
                    # Atom: <link href="..."/> RSS: <link>url</link>
                    link = child.get('href') or (child.text or '').strip()
                elif tag == 'guid' and not link:
                    # Some feeds use <guid> as the URL
                    val = (child.text or '').strip()
                    if val.startswith('http'):
                        link = val

            if title and link and title.lower() not in seen:
                seen.add(title.lower())
                if link.startswith('/'):
                    link = base_url + link
                if link.startswith('http'):
                    links.append(f"- [{title}]({link})")

        if not links and items:
            return links, f"Feed had {len(items)} items but none had parseable title+link"
        if not items:
            return links, "Feed parsed but contained no items (wrong URL or empty feed)"

    except ET.ParseError as e:
        return links, f"XML parse error: {e}"
    except Exception as e:
        return links, f"RSS parse error: {e}"

    return links, None


async def fetch_headlines_from_url(url: str) -> dict:
    """Fetch and extract headlines from a news site URL (HTML scrape or RSS/Atom feed)."""
    if not url.startswith("http"):
        url = f"https://{url}"

    # Proxy is required for news fetching
    proxy_config = require_proxy("News fetching")

    # Validate proxy config
    if not proxy_config or not isinstance(proxy_config, str):
        logger.error(f"Invalid proxy config for news: {proxy_config}")
        raise ValueError(f"Invalid proxy configuration: {proxy_config}")

    logger.info(f"News fetching via proxy: {proxy_config} for URL: {url}")

    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    headers = {
        # Tor Browser standardised UA (all Tor users share this to prevent fingerprinting)
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        # Omit brotli — httpx doesn't auto-decompress br, so compressed bytes would corrupt XML parsing
        "Accept-Encoding": "gzip, deflate",
    }

    logger.debug(f"Creating httpx client with proxy={proxy_config}")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, proxy=proxy_config) as client:
        logger.debug(f"Making request to {url} through proxy {proxy_config}")
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            raw = response.content  # bytes — avoids encoding roundtrip issues

            # Detect RSS/Atom feeds by content-type or raw byte sniff
            sniff = raw.lstrip(b'\xef\xbb\xbf')[:500]
            is_feed = (
                "xml" in content_type or
                "rss" in content_type or
                sniff.startswith(b"<?xml") or
                b"<rss" in sniff or
                b"<feed" in sniff
            )

            if is_feed:
                links, feed_error = _parse_rss_feed(raw, base_url)
                return {"links": links, "error": feed_error}

            text = response.text

            # --- HTML scraping path ---
            soup = BeautifulSoup(text, "lxml")
            for el in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
                el.decompose()

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
    import asyncio
    try:
        prepare_vram_for_llm(db)
        service = get_inference_service(db)
        
        if service is None:
            logger.warning("No inference service available for news summarization")
            return None

        messages = [
            {"role": "system", "content": """For each news headline, provide a 2-3 sentence summary explaining the story.
IMPORTANT: You MUST preserve the exact markdown link format [title](url) for each item.
Format each item as:
- [Headline Title](url)
  Brief 2-3 sentence summary of what this story is about, key details, and why it matters.

Example:
- [Major Tech Company Announces Layoffs](https://example.com/article)
  The company is cutting 10% of its workforce amid economic uncertainty. This affects approximately 5,000 employees across multiple divisions. Industry analysts suggest this reflects broader trends in the tech sector.

Provide informative summaries that give readers context about each story."""},
            {"role": "user", "content": "\n".join(links)}
        ]

        # Add timeout for AI summarization
        try:
            async with asyncio.timeout(25):  # 25 second timeout for AI
                result = await service.chat_completion(
                    messages=messages,
                    temperature=0.3,
                    max_tokens=4096
                )

            if "error" in result:
                logger.warning(f"AI summarization error: {result['error']}")
                return None

            content = result["choices"][0]["message"]["content"]
            if content:
                from app.services.text_utils import strip_thinking_tags
                return strip_thinking_tags(content.strip())
                
        except asyncio.TimeoutError:
            logger.warning("AI summarization timed out after 25 seconds")
            return None

    except Exception as e:
        logger.warning(f"AI summarization failed: {e}")

    return None


async def fetch_news_from_source(source_url: str, source_name: str, db: Session) -> str:
    """Fetch news with AI summaries - max 10"""
    result = await fetch_headlines_from_url(source_url)
    links = result["links"][:10]

    if not links:
        err = result.get('error') or 'No headlines found'
        return f"**{source_name}:** Could not fetch headlines. {err}"

    # Use AI to summarize
    ai_result = await summarize_with_ai(links, db)
    
    if ai_result:
        # Add extra spacing between articles for better readability
        spaced_result = ai_result.replace("\n- ", "\n\n- ")
        return f"**{source_name}:**\n\n{spaced_result}"

    # Fallback: raw links with spacing
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
    """This user's OWN news sources — empty when they haven't added any.

    Deliberately does NOT fall back to the admin's global `news_sources`. That fallback meant every
    account without a list of its own (104 of 105 here) read the ADMIN'S personal feed and couldn't
    tell it apart from an instance default. A new user now starts empty and adds their own RSS/site
    URLs (News ＋, or Settings → News sources); the global setting stays as the default seed shown in
    Admin → Tools → News Sources and for non-user contexts like the bots."""
    if user and user.news_sources and user.news_sources.strip():
        return parse_news_sources(user.news_sources)
    return []


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
    from app.models import Conversation, Message

    sources = get_user_news_sources(current_user, db)

    source_name = source_url
    for s in sources:
        if s["url"] == source_url or source_url in s["url"]:
            source_name = s["name"]
            break

    markdown = await fetch_news_from_source(source_url, source_name, db)
    print(f"[NEWS] Got markdown response: {len(markdown)} chars")

    # Save to conversation if provided
    if conversation_id:
        conversation = db.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        ).first()
        if conversation:
            # Encrypted transcript event, not a plaintext row (see chat_history).
            from app.services import chat_history
            from datetime import datetime as _dt
            await chat_history.append(db, current_user, conversation_id, "assistant", markdown)
            conversation.updated_at = _dt.utcnow()
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
        # A VIDEO IS NOT AN ARTICLE, and scraping one here could never work.
        #
        # This handler fetched every URL as HTML through the news proxy, which is Tor. YouTube answers
        # a Tor exit with a redirect to google.com/sorry — the CAPTCHA wall — so `raise_for_status`
        # threw and the user got the raw text of a 429 against a google.com/sorry URL, which reads
        # like the summarizer is broken rather than like the page was never going to be readable.
        # Measured in the log: two attempts, both 429, both surfaced verbatim.
        #
        # The app already has the right answer for a video and AI Chat has been using it all along
        # (`yt` → summarize_youtube → get_transcript): ask for the TRANSCRIPT. Same text the model
        # wants, and it is not a page scrape, so the wall does not apply.
        #
        # NOTE ON THE PROXY, deliberately: the transcript call does NOT go through the news proxy —
        # it is the same direct call the `yt` command already makes, so this introduces no new
        # exposure that the app did not already have, but it IS a different path from the rest of
        # this handler and that is worth knowing when reading it.
        yt_text = ""
        try:
            from app.services import youtube_service as _yt
            if _yt.is_youtube_url(url):
                _vid = _yt.extract_video_id(url)
                yt_text = (_yt.get_transcript(_vid) or "") if _vid else ""
                if not yt_text:
                    # Say which of the two it is. "No summary" for a video with subtitles turned off
                    # and "no summary" for a blocked fetch are the same sentence and different bugs.
                    return {"summary": "This is a YouTube video and it has no transcript available "
                                       "(subtitles may be disabled for it), so there is no text to "
                                       "summarize."}
        except Exception as _e:
            logger.warning(f"YouTube transcript path failed for {url}: {_e}")
            yt_text = ""

        # Proxy is required for news article fetching
        proxy_config = require_proxy("News article fetching")
        
        # Validate proxy config
        if not proxy_config or not isinstance(proxy_config, str):
            logger.error(f"Invalid proxy config for article: {proxy_config}")
            raise ValueError(f"Invalid proxy configuration: {proxy_config}")
        
        logger.info(f"News article fetching via proxy: {proxy_config} for URL: {url}")
        
        # Fetch the article with full browser-like headers
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        if yt_text:
            # The transcript IS the article body. Skipping the fetch is the point: the request that
            # produced the 429 is the one not made.
            html = ""
            soup = BeautifulSoup("", "html.parser")
            logger.info(f"YouTube transcript: {len(yt_text)} chars for {url} (no page fetch)")
        else:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, proxy=proxy_config) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                html = response.text

            # Parse and extract main content
            soup = BeautifulSoup(html, "html.parser")

        # Log response info for debugging. Guarded: there IS no response on the transcript branch,
        # and an unguarded read here is a NameError that the outer except turns into
        # "Error: name 'response' is not defined" — a worse message than the 429 it replaced.
        if not yt_text:
            logger.info(f"Article fetch: {url} - status={response.status_code}, content_length={len(html)}")

        # Try to extract from JSON-LD (common in JS-heavy sites like MSN)
        # …unless the transcript already IS the text, in which case every extraction step below is a
        # no-op on an empty document and would end at "Could not extract article content".
        text = yt_text
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
