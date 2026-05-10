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
            cleaned = cleaned[m.end():]
        else:
            # No explicit response header.
            # Model outputs: "Thinking Process:\n\n1.  **Step:**...\n2.  **Step:**...\n\nResponse"
            # Split on double-newlines and discard:
            #   - the thinking header paragraph
            #   - any paragraph whose first line is a numbered bold step ("1.  **...")
            paragraphs = re.split(r'\n\n+', cleaned)
            response_paras = []
            for para in paragraphs:
                stripped = para.strip()
                if not stripped:
                    continue
                if _THINKING_HEADER_RE.match(stripped):
                    continue
                # Numbered bold thinking steps: "1.  **Step name:**..."
                if re.match(r'\d+\.\s+\*\*', stripped):
                    continue
                response_paras.append(para)
            cleaned = '\n\n'.join(response_paras)

    result = cleaned.strip()

    # If everything was stripped, return a fallback message
    if not result:
        return "I apologize, I wasn't able to generate a proper response. Please try again."

    return result
