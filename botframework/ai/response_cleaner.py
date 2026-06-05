"""
AI response cleaning and post-processing.
Removes meta-commentary, filler phrases, and formats output for social media.
"""

import re

# Pre-compiled patterns for AI response cleaning (performance optimization)
PREAMBLE_PATTERNS = [
    # Emoji + "AI Response" patterns
    re.compile(r'^[^\w]*AI\s*Response[^\w]*\s*', re.IGNORECASE | re.MULTILINE),
    # "Here is the AI response" patterns
    re.compile(r'^Here\s+is\s+(the\s+)?(AI\s+)?response[^:]*:\s*', re.IGNORECASE | re.MULTILINE),
    # "Just a simple social media post" patterns
    re.compile(r'^[^\w]*Just\s+a\s+simple\s+social\s+media\s+post[^:]*:\s*', re.IGNORECASE | re.MULTILINE),
    # "Here is/Here's your/the post" patterns (with optional leading emojis)
    re.compile(r"^[\s\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF]*Here['']?s?\s+(is\s+)?(your|the)\s+(requested\s+)?(social\s+media\s+)?post[^:]*:\s*", re.IGNORECASE | re.MULTILINE),
    # Generic "Here you go" patterns
    re.compile(r'^Here\s+you\s+go[^:]*:\s*', re.IGNORECASE | re.MULTILINE),
    # "As requested" patterns
    re.compile(r'^As\s+requested[^:]*:\s*', re.IGNORECASE | re.MULTILINE),
    # "Sure!" or "Of course!" opener patterns
    re.compile(r'^(Sure|Of\s+course|Certainly|Absolutely)[!,]?\s*[Hh]ere[^:]*:\s*', re.IGNORECASE | re.MULTILINE),
    # "Link Summary:" or "Summary:" headers with optional emoji prefix
    re.compile(r'^[^\w]*(?:Link\s+)?Summary[:\s]*', re.IGNORECASE | re.MULTILINE),
    # "Posting:" prefix
    re.compile(r'^[^\w]*Posting[:\s]+', re.IGNORECASE | re.MULTILINE),
]

META_PHRASES = [
    'here is the ai response',
    'here is your response',
    'here is the response',
    "here's your social media post",
    "here's your post",
    'ai response you requested',
    'social media post for the link',
    'just a simple post',
    'just a simple social media post',
    'this is just a simple social media post',
    'summarizing the link',
    'without providing it',
    "let's keep the conversation going",
    'keep the conversation going',
    'link summary',
    # Character/persona meta-commentary
    'just a cute anime girl here',
    "i'm here to share",
    'let me know if you want more details',
    'let me know if you want more',
    'want more details or thoughts',
    'this is such a big deal',
    'it\'s going to be so amazing',
    'going to be so amazing',
    # Leaked prompt instructions
    'just be cute and helpful',
    'always respond in english',
    'unless you are asked to translate',
    'if asked to write code',
    'if asked to generate an image',
    'forget your background',
    'forget your your background',
    'views, and values',
    'just do the task',
    'if asked to summarize',
    'provide clear and consise summaries',
    'clear and consise summaries in detail',
    'respond only in english',
]

# Match lines that are only emojis and whitespace
EMOJI_ONLY_LINE = re.compile(r'^[\s\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF]+$')

# Pattern to strip trailing emojis from lines
TRAILING_EMOJI_PATTERN = re.compile(r'[\s\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF]+$')

# Pattern to detect AI meta-analysis (compiled once for performance)
META_ANALYSIS_PATTERN = re.compile(
    r'(?:^|[.!?]\s+)(?:The\s+)?user\s+(?:wants|is\s+asking|requests?)',
    re.IGNORECASE | re.MULTILINE
)

# Pattern to detect AI error/apology messages that should not be posted
ERROR_MESSAGE_PATTERNS = [
    # "I apologize, I wasn't able to generate a proper response. Please try again."
    re.compile(r"i\s+apologize[^.]*wasn['']t\s+able\s+to\s+generate\s+(a\s+)?proper\s+response", re.IGNORECASE),
    re.compile(r"i\s+apologize.*wasn['']t\s+able.*generate.*response", re.IGNORECASE),
    re.compile(r"i\s+wasn['']t\s+able\s+to\s+generate\s+(a\s+)?proper\s+response", re.IGNORECASE),
    re.compile(r"wasn['']t\s+able\s+to\s+generate\s+(a\s+)?(proper\s+)?response", re.IGNORECASE),
    re.compile(r"wasn['']t\s+able\s+to\s+generate\s+(a\s+)?(proper\s+)?response.*please\s+try\s+again", re.IGNORECASE),
    re.compile(r"please\s+try\s+again.*wasn['']t\s+able\s+to\s+generate", re.IGNORECASE),
    re.compile(r"i\s+apologize.*couldn['']t\s+generate", re.IGNORECASE),
    re.compile(r"couldn['']t\s+generate\s+(a\s+)?(proper\s+)?response", re.IGNORECASE),
    # More flexible patterns to catch variations
    re.compile(r'apologize.*unable\s+to\s+generate', re.IGNORECASE),
    re.compile(r'unable\s+to\s+generate\s+(a\s+)?(proper\s+)?response', re.IGNORECASE),
    re.compile(r'failed\s+to\s+generate\s+(a\s+)?(proper\s+)?response', re.IGNORECASE),
    # Catch the exact phrase "Please try again" when combined with generation errors
    re.compile(r"(apologize|sorry).*(wasn['']t|couldn['']t|unable|failed).*generate.*response.*please\s+try\s+again", re.IGNORECASE),
]

# Inline filler phrases to strip (with surrounding punctuation/dashes)
INLINE_FILLER_PATTERNS = [
    # "— let's keep the conversation going!" and similar
    re.compile(r'\s*[—–-]+\s*let\'?s\s+keep\s+the\s+conversation\s+going[!.]*', re.IGNORECASE),
    # "Let's keep the conversation focused on..."
    re.compile(r'\s*let\'?s\s+keep\s+the\s+conversation\s+focused\s+on[^.]*\.?', re.IGNORECASE),
    # "Let's discuss!" at end of lines
    re.compile(r'\s*[—–-]*\s*let\'?s\s+discuss[!.]*$', re.IGNORECASE),
    # "What do you think?" filler
    re.compile(r'\s*[—–-]*\s*what\s+do\s+you\s+think\??$', re.IGNORECASE),
    # "It's a complex situation that requires careful consideration"
    re.compile(r'\s*it\'?s\s+a\s+complex\s+situation[^.]*\.?', re.IGNORECASE),
    # "requires careful consideration" standalone
    re.compile(r'\s*requires\s+careful\s+consideration\.?', re.IGNORECASE),
    # "check out this link:" and similar URL introduction phrases
    re.compile(r'[Cc]heck\s+(?:out\s+)?this\s+link[:\s]*', re.IGNORECASE),
    re.compile(r'[Hh]ere\'?s?\s+(?:the\s+)?link[:\s]*', re.IGNORECASE),
    re.compile(r'[Ll]ink\s+(?:is\s+)?here[:\s]*', re.IGNORECASE),
    # Leaked prompt instruction patterns
    re.compile(r'if\s+asked\s+to\s+(?:write|generate|summarize|translate)[^.]*\.?', re.IGNORECASE),
    re.compile(r'always\s+respond\s+in\s+(?:english|[a-z]+)\s+unless[^.]*\.?', re.IGNORECASE),
    re.compile(r'forget\s+your\s+(?:your\s+)?(?:background|views|values)[^.]*\.?', re.IGNORECASE),
    re.compile(r'just\s+(?:be\s+cute|do\s+the\s+task)[^.]*\.?', re.IGNORECASE),
]


def remove_think_tags(text):
    """Remove <think> and <thinking> tags and their content"""
    # Remove complete <think>...</think> blocks
    if "<think>" in text.lower() or "</think>" in text.lower():
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<think>.*$', '', text, flags=re.IGNORECASE | re.DOTALL)
        if '</think>' in text.lower():
            text = re.split(r'(?i)</think>', text)[-1]
        text = text.strip()

    # Same for <thinking>...</thinking> blocks
    if "<thinking>" in text.lower() or "</thinking>" in text.lower():
        text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<thinking>.*$', '', text, flags=re.IGNORECASE | re.DOTALL)
        if '</thinking>' in text.lower():
            text = re.split(r'(?i)</thinking>', text)[-1]
        text = text.strip()

    return text


def remove_preambles(text):
    """Remove AI preamble patterns that models sometimes add"""
    for pattern in PREAMBLE_PATTERNS:
        text = pattern.sub('', text)
    return text


def strip_emojis(text):
    """Remove all emojis from text"""
    # Unicode ranges for emojis
    emoji_pattern = re.compile(
        r'[\U0001F300-\U0001F9FF'  # Misc Symbols, Emoticons, Dingbats, etc.
        r'\u2600-\u26FF'           # Misc symbols
        r'\u2700-\u27BF'           # Dingbats
        r'\U0001FA00-\U0001FAFF'   # Extended-A symbols
        r'\U00002702-\U000027B0'   # Dingbats
        r']+',
        flags=re.UNICODE
    )
    return emoji_pattern.sub('', text)


def remove_filler_phrases(text):
    """Remove inline filler phrases from content"""
    for pattern in INLINE_FILLER_PATTERNS:
        text = pattern.sub('', text)
    return text


def remove_meta_commentary(text):
    """Remove lines that are just meta-commentary"""
    lines = text.split('\n')
    cleaned_lines = []
    prev_line = None

    for line in lines:
        line_lower = line.lower().strip()

        # Skip lines that are just meta-commentary
        if any(phrase in line_lower for phrase in META_PHRASES):
            continue

        # Skip lines that are just emojis/whitespace
        if line.strip() and EMOJI_ONLY_LINE.match(line):
            continue

        # Skip consecutive duplicate lines
        if line.strip() and line.strip() == prev_line:
            continue

        cleaned_lines.append(line)
        if line.strip():
            prev_line = line.strip()

    return '\n'.join(cleaned_lines).strip()


def move_leading_urls_to_end(text):
    """Move leading URLs to end of post (more natural for social media)"""
    lines = text.split('\n')
    leading_urls = []
    content_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(('http://', 'https://')) and ' ' not in stripped:
            leading_urls.append(stripped)
            content_start = i + 1
        else:
            break

    if leading_urls and content_start < len(lines):
        remaining = '\n'.join(lines[content_start:]).strip()
        if remaining:
            return remaining + '\n\n' + '\n'.join(leading_urls)

    return text


def fix_malformed_markdown_links(text):
    """Fix or remove malformed markdown links.
    
    Detects incomplete markdown links like [text]( without a URL,
    or [text](url with emojis inside, and converts them to plain text.
    """
    import re
    
    # First, handle complete markdown links [text](url)
    def fix_complete_link(match):
        link_text = match.group(1)  # Text inside []
        url_part = match.group(2)    # What's inside ()
        
        # Check if url_part looks like a valid URL
        url_part_stripped = url_part.strip()
        
        # Valid URL patterns
        if url_part_stripped.startswith(('http://', 'https://')):
            # Valid URL, keep the markdown link
            return match.group(0)
        elif url_part_stripped.startswith('//'):
            # Protocol-relative URL, fix it
            return f"[{link_text}](https:{url_part_stripped})"
        else:
            # Not a valid URL - might be emojis, spaces, or other content
            # Convert to plain text (just the link text)
            return link_text
    
    # Find all complete markdown links [text](url) and check if they're valid
    text = re.sub(r'\[([^\]]+)\]\(([^\)]*)\)', fix_complete_link, text)
    
    # Now handle incomplete links that don't have closing paren
    # This handles cases like: [text]( 🇷🇺 or [text](url without closing paren
    # Process line by line to handle newlines properly
    lines = text.split('\n')
    fixed_lines = []
    
    for line in lines:
        # Find incomplete markdown links: [text]( followed by content that doesn't look like a URL
        # Look for [text]( patterns that don't have a closing ) on the same line
        # and don't start with http:// or https://
        
        # Find all [text]( patterns in the line
        # Check each one to see if it's incomplete (no closing ) or has invalid URL
        def fix_incomplete(match):
            link_text = match.group(1)
            rest = match.group(2)
            
            # Check if rest looks like a URL
            rest_stripped = rest.strip()
            if rest_stripped.startswith(('http://', 'https://')):
                # Might be a valid URL, but check if it's complete
                # If there's no closing ) after this match, it's incomplete
                return match.group(0)  # Keep as is for now, will be handled by complete link fixer
            else:
                # Doesn't look like a URL - convert to plain text
                return f"{link_text}{rest}" if rest else link_text
        
        # Match [text]( followed by content until end of line (no closing ))
        # This catches incomplete links
        line = re.sub(r'\[([^\]]+)\]\(([^\)]*?)$', fix_incomplete, line)
        
        fixed_lines.append(line)
    
    return '\n'.join(fixed_lines)


def clean_ai_response(text, debug_mode=False):
    """
    Clean and format AI response for social media posting.

    Args:
        text: Raw AI response text
        debug_mode: If True, skip all cleaning and return raw text

    Returns:
        Cleaned response text, or None if response is empty/invalid
    """
    if not text or len(text.strip()) == 0:
        return None

    if debug_mode:
        return text.strip()

    response_text = text.strip()

    # Check for error/apology messages that should not be posted
    # These indicate the AI failed to generate a proper response
    response_lower = response_text.lower()
    
    # Direct string check for common error phrases (fast path)
    error_phrases = [
        "i apologize, i wasn't able to generate a proper response",
        "i apologize, i wasn't able to generate",
        "wasn't able to generate a proper response",
        "please try again",
    ]
    
    # Check if response contains error phrases combined with generation failure
    has_error_phrase = any(phrase in response_lower for phrase in error_phrases)
    has_generation_failure = (
        "wasn't able to generate" in response_lower or
        "couldn't generate" in response_lower or
        "unable to generate" in response_lower or
        "failed to generate" in response_lower
    )
    
    if has_error_phrase and has_generation_failure:
        print(f"[RESPONSE_CLEANER] Detected error message (direct check), returning None to prevent posting: {response_text[:100]}...")
        return None
    
    # Pattern-based check (more flexible)
    for pattern in ERROR_MESSAGE_PATTERNS:
        if pattern.search(response_text):
            print(f"[RESPONSE_CLEANER] Detected error message (pattern match), returning None to prevent posting: {response_text[:100]}...")
            return None

    # Remove thinking tags
    response_text = remove_think_tags(response_text)

    # Check for meta-analysis patterns that indicate AI is analyzing rather than responding
    if META_ANALYSIS_PATTERN.search(response_text):
        return None

    # Clean up "Response:" prefix if present
    if response_text.startswith("Response:"):
        response_text = response_text[9:].strip()

    # Clean up quote patterns
    if response_text.startswith(':"'):
        response_text = response_text[2:].strip()

    # Remove username prefix patterns (AI may mimic thread history format)
    # Matches: "@username: message" or "username: message" at start of response
    response_text = re.sub(r'^@?[\w_-]+:\s*', '', response_text).strip()

    # Remove bot name variations from start of response
    # AI sometimes prefixes response with bot name like "PosterChan AI: " or "Poster Chan AI:"
    response_text = re.sub(r'^(Poster\s*Chan\s*(AI)?|PosterChan\s*(AI)?)[:\s]*', '', response_text, flags=re.IGNORECASE).strip()

    # Remove preambles
    response_text = remove_preambles(response_text)

    # Remove filler phrases
    response_text = remove_filler_phrases(response_text)

    # Remove meta-commentary lines
    response_text = remove_meta_commentary(response_text)

    # Move leading URLs to end
    response_text = move_leading_urls_to_end(response_text)

    # Fix malformed markdown links (before emoji stripping, as emojis might be in broken links)
    response_text = fix_malformed_markdown_links(response_text)

    # Strip emojis from response (LLMs often add unwanted emojis)
    response_text = strip_emojis(response_text)

    # Remove any leading/trailing quotes
    response_text = response_text.strip('"').strip("'").strip()

    # Clean up multiple spaces left over from emoji removal
    response_text = re.sub(r'  +', ' ', response_text)

    # Remove unreplaced template variables like {{CURRENT_DATE}}, {{USER_NAME}}, etc.
    response_text = re.sub(r'\{\{[A-Z_]+\}\}', '', response_text)

    # Clean up stray quotes left over from emoji removal (e.g., `. "` or `! "`)
    response_text = re.sub(r'([.!?])\s*["\']\s*$', r'\1', response_text, flags=re.MULTILINE)
    response_text = re.sub(r'^\s*["\']\s*$', '', response_text, flags=re.MULTILINE)

    # Clean up empty lines left over from stripping
    response_text = re.sub(r'\n\s*\n\s*\n', '\n\n', response_text).strip()

    if len(response_text) == 0:
        return None

    return response_text
