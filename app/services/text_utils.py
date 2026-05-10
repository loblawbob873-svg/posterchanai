"""Shared text processing utilities."""
import logging
import re

_log = logging.getLogger(__name__)

# Thinking tag definitions - single source of truth for all tag variants
# Used by streaming filters in chat.py, chat_service.py, and strip_thinking_tags()
THINKING_TAGS = [
    ("<think>", "</think>"),
    ("<thinking>", "</thinking>"),
    ("<thought>", "</thought>"),
    ("<reasoning>", "</reasoning>"),
    ("<internal_thought>", "</internal_thought>"),
    ("<internal-thought>", "</internal-thought>"),
]

# Prefixes for detecting opening tags (covers all variants above)
THINKING_OPEN_PREFIXES = ('<think', '<thought', '<reasoning', '<internal')

# Plain-text thinking section headers used by some fine-tuned models instead of XML tags
# e.g. "**Thinking Process:**\n..." or "Thinking:\n..."
_THINKING_HEADER_RE = re.compile(
    r'^[\s*]*(?:thinking\s+process|thinking|reasoning\s+process|internal\s+thought)[\s*]*:',
    re.IGNORECASE | re.MULTILINE
)

# Compiled regex for detecting closing tags
THINKING_CLOSE_PATTERN = re.compile(
    r'</(?:think(?:ing)?|thought|reasoning|internal[_-]?thought)>',
    re.IGNORECASE
)


def has_thinking_open(text: str) -> bool:
    """Check if text contains any thinking tag opening or plain-text thinking header"""
    lower = text.lower()
    if any(prefix in lower for prefix in THINKING_OPEN_PREFIXES):
        return True
    return bool(_THINKING_HEADER_RE.search(text))


def find_thinking_open(text: str):
    """Find earliest thinking tag opening, return (position, tag_pair) or (-1, None)"""
    text_lower = text.lower()
    earliest_pos = -1
    found_pair = None
    for open_tag, close_tag in THINKING_TAGS:
        pos = text_lower.find(open_tag)
        if pos != -1 and (earliest_pos == -1 or pos < earliest_pos):
            earliest_pos = pos
            found_pair = (open_tag, close_tag)
    return earliest_pos, found_pair


def strip_thinking_tags(response: str) -> str:
    """Strip thinking tags from AI response (used by Qwen and other reasoning models).

    Handles multiple tag variants:
    - <think>...</think> and <thinking>...</thinking> blocks
    - <thought>...</thought> blocks
    - <reasoning>...</reasoning> blocks
    - <internal_thought>...</internal_thought> blocks
    - Unclosed tags (strips from opening tag to end)
    """
    cleaned = response

    # First, remove all properly closed thinking blocks
    # Matches: <think>...</think>, <thinking>...</thinking>
    cleaned = re.sub(r'<think(?:ing)?[^>]*>[\s\S]*?</think(?:ing)?>', '', cleaned, flags=re.IGNORECASE)
    # Matches: <thought>...</thought>
    cleaned = re.sub(r'<thought[^>]*>[\s\S]*?</thought>', '', cleaned, flags=re.IGNORECASE)
    # Matches: <reasoning>...</reasoning>
    cleaned = re.sub(r'<reasoning[^>]*>[\s\S]*?</reasoning>', '', cleaned, flags=re.IGNORECASE)
    # Matches: <internal_thought>...</internal_thought> or <internal-thought>...</internal-thought>
    cleaned = re.sub(r'<internal[_-]?thought[^>]*>[\s\S]*?</internal[_-]?thought>', '', cleaned, flags=re.IGNORECASE)

    # Then handle unclosed tags at the end (model stopped mid-thought)
    cleaned = re.sub(r'<think(?:ing)?[^>]*>[\s\S]*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'<thought[^>]*>[\s\S]*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'<reasoning[^>]*>[\s\S]*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'<internal[_-]?thought[^>]*>[\s\S]*$', '', cleaned, flags=re.IGNORECASE)

    # Strip plain-text thinking sections used by some uncensored fine-tunes:
    # "**Thinking Process:**\n...\n\n**Response:**\n..." → keep only the Response section
    # Also handles: "Thinking:\n...\n\nActual response"
    response_header = re.compile(
        r'\*{0,2}(?:response|answer|reply|output)\*{0,2}\s*:\s*\n',
        re.IGNORECASE
    )
    if _THINKING_HEADER_RE.search(cleaned):
        m = response_header.search(cleaned)
        if m:
            # Explicit "**Response:**\n" section found — take everything after it
            cleaned = cleaned[m.end():]
        elif THINKING_CLOSE_PATTERN.search(cleaned):
            # Model uses plain-text "Thinking Process:" header but closes with </think>
            # Everything after </think> is the actual response
            close_m = THINKING_CLOSE_PATTERN.search(cleaned)
            cleaned = cleaned[close_m.end():]
        else:
            # No explicit response header.
            # This model embeds the response inside the thinking as a bullet:
            #   '-   Response: “Hello!”'  or  '- Final Response: Hi there!'
            # Try quoted form first, then unquoted.
            _bullet_re = re.compile(
                r'[-*]\s+(?:final\s+)?(?:response|answer|reply)\s*:\s*',
                re.IGNORECASE
            )
            bullet_m = _bullet_re.search(cleaned)
            if bullet_m:
                after = cleaned[bullet_m.end():]
                # Quoted: grab content between first pair of double-quotes
                quoted = re.match(r'”([^”]+)”', after)
                if quoted:
                    cleaned = quoted.group(1).strip()
                else:
                    # Unquoted: take rest of line, strip trailing parenthetical comment
                    line = after.split('\n')[0]
                    line = re.sub(r'\s*\([^)]*\)\s*$', '', line)
                    cleaned = line.strip().strip('”')
            else:
                # Fall back: discard all thinking paragraphs, keep clean non-indented ones
                paragraphs = re.split(r'\n\n+', cleaned)
                response_paras = []
                for para in paragraphs:
                    if not para.strip():
                        continue
                    first_line = next((l for l in para.split('\n') if l.strip()), '')
                    stripped_first = first_line.strip()
                    if _THINKING_HEADER_RE.match(stripped_first):
                        continue
                    if re.match(r'\d+\.\s+\*\*', stripped_first):
                        continue
                    if first_line != first_line.lstrip():
                        continue
                    if re.match(r'[-*]\s+', stripped_first):
                        continue
                    response_paras.append(para)
                cleaned = '\n\n'.join(response_paras)

    result = cleaned.strip()

    # If everything was stripped, return a fallback message
    if not result:
        return "I apologize, I wasn't able to generate a proper response. Please try again."

    return result
