#!/usr/bin/env python3
"""
News fetching and summarization module for posterchan.
Fetches headlines from news sources and summarizes them with AI.
"""

import requests
import logging
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from ai.client import generate_reply

logger = logging.getLogger(__name__)

# Default news sources
DEFAULT_NEWS_SOURCES = {
    "drudge": {"url": "drudgereport.com", "name": "Drudge Report"},
    "npr": {"url": "npr.org/sections/news", "name": "NPR"},
    "nypost": {"url": "nypost.com", "name": "NY Post"},
    "fox": {"url": "foxnews.com", "name": "Fox News"},
    "foxnews": {"url": "foxnews.com", "name": "Fox News"},
    "newsweek": {"url": "newsweek.com", "name": "Newsweek"},
    "haaretz": {"url": "haaretz.com", "name": "Haaretz"},
    "lgbtqnation": {"url": "lgbtqnation.com", "name": "LGBTQ Nation"},
    "cnn": {"url": "cnn.com", "name": "CNN"},
}


def get_news_source(source_name: str):
    """Get news source by name or URL"""
    source_lower = source_name.lower().strip()
    
    # Check if it's a known source name
    if source_lower in DEFAULT_NEWS_SOURCES:
        return DEFAULT_NEWS_SOURCES[source_lower]
    
    # Check if it matches any source name partially
    for key, source in DEFAULT_NEWS_SOURCES.items():
        if key in source_lower or source_lower in key:
            return source
    
    # If it looks like a URL, use it directly
    if "." in source_name:
        return {"url": source_name, "name": source_name}
    
    # Default to drudge if not found
    return DEFAULT_NEWS_SOURCES["drudge"]


def fetch_headlines_from_url(url: str, max_headlines: int = 10):
    """Fetch and extract headlines from a news site URL"""
    if not url.startswith("http"):
        url = f"https://{url}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        response = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
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
            clean_text = ''.join(c for c in text if c not in '🎁📱💰🔥⚡️✨')
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
            links.append(f"[{text}]({href})")
            if len(links) >= max_headlines:
                break

        return {"links": links, "error": None}

    except Exception as e:
        logger.warning(f"Error fetching {url}: {e}")
        return {"links": [], "error": str(e)}


def summarize_with_ai(links: list):
    """Use AI to create short summaries for news headlines"""
    if not links:
        return None
    
    # Check if AI is configured before attempting to use it
    from ai.client import is_ai_configured
    if not is_ai_configured():
        logger.info("AI not configured, skipping summarization")
        return None
    
    # Create prompt for AI
    links_text = "\n".join(links[:10])  # Max 10 headlines
    
    system_prompt = """You are a news summarizer. For each news headline, provide a SHORT 1 sentence summary.
CRITICAL: You MUST preserve the exact markdown link format [title](url) for each item.
Format each item as a single line:
- [Headline Title](url) - One sentence summary of what happened.

Keep summaries very concise - one sentence only. Focus on key facts. Output format: - [Title](url) - Summary"""

    user_prompt = f"Summarize these news headlines with short summaries:\n\n{links_text}"
    
    try:
        # Use the existing AI client
        result = generate_reply(
            user_prompt,
            previous_content=None,
            ping=False,
            thread_history=None,
            narrate_mode=False,
            custom_system_prompt=system_prompt
        )
        
        if result:
            logger.info("AI summarization successful")
            return result.strip()
        else:
            logger.info("AI returned None (likely not configured)")
    except Exception as e:
        logger.warning(f"AI summarization failed: {e}")
    
    return None


def format_for_plain(text: str) -> str:
    """Convert markdown links to simple format: summary - url (one line per article)"""
    import re
    lines = text.split('\n')
    formatted_lines = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Header lines (source name) - keep it but clean it up
        if ':' in line and not '[' in line and not 'http' in line:
            header = line.replace('**', '').replace(':', '').strip()
            if header:
                formatted_lines.append(header)
            continue
        
        # Already in correct format: "text - http://url"
        if ' - http' in line or ' - https' in line:
            formatted_lines.append(line)
            continue
        
        # Find markdown link pattern: - [Title](url) - Summary
        # Pattern: - [title](url) - summary text
        match = re.match(r'^-\s*\[([^\]]+)\]\(([^\)]+)\)\s*-\s*(.+)$', line)
        if match:
            title = match.group(1)
            url = match.group(2)
            summary = match.group(3).strip()
            # Output: summary - url
            formatted_lines.append(f"{summary} - {url}")
            continue
        
        # Try simpler pattern: [title](url) - summary
        match = re.match(r'\[([^\]]+)\]\(([^\)]+)\)\s*-\s*(.+)$', line)
        if match:
            title = match.group(1)
            url = match.group(2)
            summary = match.group(3).strip()
            formatted_lines.append(f"{summary} - {url}")
            continue
        
        # Just a link without summary: [title](url)
        if '[' in line and '](' in line:
            link_match = re.search(r'\[([^\]]+)\]\(([^\)]+)\)', line)
            if link_match:
                title = link_match.group(1)
                url = link_match.group(2)
                formatted_lines.append(f"{title} - {url}")
                continue
        
        # If we get here and the line has content but no link, log it for debugging
        if line and not line.startswith('-'):
            # Might be a summary line without link, skip it
            pass
    
    result = '\n'.join(formatted_lines)
    # Debug: log if we have very few lines (computed outside the f-string so it stays
    # valid on Python 3.11, which forbids backslashes inside f-string expressions).
    _in_lines = len([l for l in text.split('\n') if l.strip()])
    _out_lines = len([l for l in result.split('\n') if l.strip()])
    if _out_lines < 2:
        logger.warning(f"format_for_plain produced few lines. Input had {_in_lines} lines, output has {_out_lines} lines")
    return result


def fetch_news_from_source(source_name: str, max_headlines: int = 10, for_plain: bool = False):
    """Fetch news from a source with AI summaries"""
    try:
        source = get_news_source(source_name)
        source_url = source["url"]
        source_display_name = source["name"]
        
        logger.info(f"Fetching news from {source_display_name} ({source_url}), for_plain={for_plain}")
        result = fetch_headlines_from_url(source_url, max_headlines)
        links = result["links"]
        
        logger.info(f"Fetched {len(links)} headlines")
        
        if not links:
            error_msg = f"{source_display_name}: Could not fetch headlines. {result.get('error', 'Unknown error')}"
            logger.warning(error_msg)
            return error_msg
        
        # Use AI to summarize
        ai_result = summarize_with_ai(links)
        
        if ai_result:
            logger.info(f"AI summarization successful, length: {len(ai_result)}")
            # For markdown platforms (Pleroma), use markdown format with bold header
            # For plain-text channels, format AI output
            if not for_plain:
                # Pleroma: markdown format with bold header
                formatted = f"**{source_display_name}:**\n\n{ai_result}"
                logger.info(f"Pleroma format: {len(formatted)} chars")
            else:
                # plain text: format AI output through parser
                formatted = f"{source_display_name}:\n\n{ai_result}"
                logger.info(f"Before format_for_plain: {len(formatted)} chars")
                formatted = format_for_plain(formatted)
                logger.info(f"After format_for_plain: {len(formatted)} chars, {len([l for l in formatted.split(chr(10)) if l.strip()])} lines")
                # Debug: show first 500 chars
                logger.info(f"Formatted preview: {formatted[:500]}")
        else:
            logger.info(f"No AI result, using fallback format with {len(links)} links")
            # Fallback: format raw links directly (no AI, so format immediately)
            formatted_links = []
            for i, link in enumerate(links):
                # Extract text and URL from markdown link [text](url)
                import re
                match = re.match(r'\[([^\]]+)\]\(([^\)]+)\)', link)
                if match:
                    text = match.group(1)
                    url = match.group(2)
                    if for_plain:
                        # simple format (already correct, no formatting needed)
                        formatted_links.append(f"{text} - {url}")
                    else:
                        # Pleroma: markdown format
                        formatted_links.append(f"- [{text}]({url})")
                    logger.debug(f"Formatted link {i+1}: {text[:50]}... -> {url[:50]}...")
                else:
                    logger.warning(f"Could not parse link {i+1}: {link[:100]}")
                    formatted_links.append(link)
            if for_plain:
                # already in correct format, just add header
                formatted = f"{source_display_name}\n" + "\n".join(formatted_links)
            else:
                formatted = f"**{source_display_name}:**\n\n" + "\n".join(formatted_links)
            logger.info(f"Fallback format: {len(formatted)} chars, {len(formatted_links)} links")
        
        # Fix URLs that might have lost the colon (https// -> https://)
        formatted = formatted.replace('https//', 'https://').replace('http//', 'http://')
        
        # Final validation - ensure we have links/URLs in the output
        has_links = 'http' in formatted or 'https' in formatted
        if not has_links:
            logger.error("Formatted output has no links! Using emergency fallback")
            # Emergency fallback: re-extract links directly
            formatted_links = []
            for link in links:
                import re
                match = re.match(r'\[([^\]]+)\]\(([^\)]+)\)', link)
                if match:
                    text = match.group(1)
                    url = match.group(2)
                    if for_plain:
                        formatted_links.append(f"{text} - {url}")
                    else:
                        formatted_links.append(f"- [{text}]({url})")
            if formatted_links:
                if for_plain:
                    formatted = f"{source_display_name}\n" + "\n".join(formatted_links)
                else:
                    formatted = f"**{source_display_name}:**\n\n" + "\n".join(formatted_links)
                logger.info(f"Emergency fallback: {len(formatted_links)} links formatted")
            else:
                logger.error("Emergency fallback also failed - no links extracted")
        
        # Final validation - ensure we have content
        if for_plain:
            lines = [l.strip() for l in formatted.split('\n') if l.strip()]
            logger.info(f"final validation: {len(lines)} lines")
            if len(lines) < 2:
                # Emergency fallback: re-extract links directly
                logger.warning(f"formatting produced only {len(lines)} lines, using emergency fallback")
                formatted_links = []
                for link in links:
                    import re
                    match = re.match(r'\[([^\]]+)\]\(([^\)]+)\)', link)
                    if match:
                        text = match.group(1)
                        url = match.group(2)
                        formatted_links.append(f"{text} - {url}")
                if formatted_links:
                    formatted = f"{source_display_name}\n" + "\n".join(formatted_links)
                    logger.info(f"Emergency fallback: {len(formatted_links)} links formatted")
                else:
                    logger.error("Emergency fallback also failed - no links extracted")
            else:
                # Rejoin lines properly
                formatted = '\n'.join(lines)
    
        logger.info(f"Final formatted output: {len(formatted)} chars, has_links: {'http' in formatted or 'https' in formatted}")
        # Final check - if still no links, something is very wrong
        if 'http' not in formatted and 'https' not in formatted:
            logger.error(f"CRITICAL: Final output has no links! Output: {formatted[:200]}")
        
        return formatted
    except Exception as e:
        logger.error(f"Error in fetch_news_from_source: {e}", exc_info=True)
        import traceback
        traceback.print_exc()
        return f"Error fetching news: {str(e)}"
