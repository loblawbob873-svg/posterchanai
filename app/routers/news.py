from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
import httpx
import logging
from bs4 import BeautifulSoup
from urllib.parse import urlparse

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

                links.append(f"- [{text}]({href})")
                if len(links) >= 12:
                    break

            return {"links": links, "error": None}

        except Exception as e:
            logger.warning(f"Error fetching {url}: {e}")
            return {"links": [], "error": str(e)}


async def summarize_with_ai(links: list, db: Session) -> str:
    """Use native inference service to create clickable summaries"""
    try:
        prepare_vram_for_llm(db)
        service = get_inference_service(db)

        messages = [
            {"role": "system", "content": "Summarize each headline in 1 sentence. Keep the markdown link format. Output as a bullet list. No extra text."},
            {"role": "user", "content": "\n".join(links)}
        ]

        result = await service.chat_completion(
            messages=messages,
            temperature=0.3,
            max_tokens=2048
        )

        if "error" in result:
            logger.warning(f"AI summarization error: {result['error']}")
            return None

        content = result["choices"][0]["message"]["content"]
        if content:
            from app.services.text_utils import strip_thinking_tags
            return strip_thinking_tags(content.strip())

    except Exception as e:
        logger.warning(f"AI summarization failed: {e}")

    return None


async def fetch_news_from_source(source_url: str, source_name: str, db: Session) -> str:
    """Fetch news with AI summaries - max 10"""
    result = await fetch_headlines_from_url(source_url)
    links = result["links"][:10]

    if not links:
        return f"**{source_name}:** Could not fetch headlines. {result.get('error', '')}"

    # Use AI to summarize
    ai_result = await summarize_with_ai(links, db)
    if ai_result:
        return f"**{source_name}:**\n\n{ai_result}"

    # Fallback: raw links
    return f"**{source_name}:**\n\n" + "\n".join(links)


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
    from app.models import Conversation, Message

    sources = get_user_news_sources(current_user, db)

    source_name = source_url
    for s in sources:
        if s["url"] == source_url or source_url in s["url"]:
            source_name = s["name"]
            break

    markdown = await fetch_news_from_source(source_url, source_name, db)

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


@router.get("/summarize")
async def summarize_article(
    url: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Summarize a specific article URL"""
    import httpx
    from bs4 import BeautifulSoup

    try:
        # Fetch the article
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            html = response.text

        # Parse and extract main content
        soup = BeautifulSoup(html, "html.parser")

        # Remove scripts, styles, nav, footer, ads
        for el in soup(["script", "style", "nav", "footer", "aside", "header", "form", "noscript"]):
            el.decompose()

        # Try to find article content in common containers
        article = soup.find("article") or soup.find(class_=["article", "post", "content", "story", "entry"])
        if article:
            text = article.get_text(separator="\n", strip=True)
        else:
            # Fallback to body
            body = soup.find("body")
            text = body.get_text(separator="\n", strip=True) if body else ""

        # Clean up and limit text
        lines = [line.strip() for line in text.split("\n") if line.strip() and len(line.strip()) > 20]
        text = "\n".join(lines[:100])  # Max 100 lines

        if len(text) < 100:
            return {"summary": "Could not extract article content."}

        # Get AI service
        from app.services.custom_ai_service import CustomAIService
        from app.services.llm_backend import get_llm_service

        if current_user.custom_ai_enabled and current_user.custom_ai_url:
            service = CustomAIService(
                api_type=current_user.custom_ai_type or "ollama",
                base_url=current_user.custom_ai_url,
                model=current_user.custom_ai_model,
                api_key=current_user.custom_ai_api_key
            )
        else:
            service = get_llm_service(db)

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
