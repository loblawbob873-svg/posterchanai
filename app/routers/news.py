from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
import logging

from app.database import get_db
from app.models import User
from app.auth import get_current_user
from app.services import instance_membership
from app.services.inference_factory import get_inference_service, prepare_vram_for_llm
from app.services.proxy_utils import require_proxy

logger = logging.getLogger(__name__)


async def get_instance_user(user=Depends(get_current_user)):
    return await instance_membership.require_user(user)


router = APIRouter(prefix="/api/news", tags=["news"])


# THE `news` COMMAND AND "NEWS SOURCES" ARE GONE (2026-10-05, "remove News sources from Settings and the
# code feature"): its per-user/admin source lists, headline fetcher, AI digest and the /sources,
# /headlines and /all endpoints. What stays is the News APP's own summarizer below -- the RSS reader
# (static/js/client/news.js) fetches through /api/rss and keeps its feeds in the user's Nostr documents.


@router.get("/summarize")
async def summarize_article(
    url: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_instance_user)
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
