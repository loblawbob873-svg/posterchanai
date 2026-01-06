"""Shared text processing utilities."""
import re


def strip_thinking_tags(response: str) -> str:
    """Strip thinking tags from AI response (used by Qwen and other reasoning models).

    Handles:
    - <think>...</think> and <thinking>...</thinking> blocks
    - Unclosed <think> or <thinking> tags (strips from opening tag to end or next paragraph)
    """
    # First, try to find closed thinking blocks and return content after
    matches = list(re.finditer(r'</think(?:ing)?>', response, re.IGNORECASE))
    if matches:
        last_match = matches[-1]
        return response[last_match.end():].strip()

    # If no closing tag, check for unclosed opening tag
    # Pattern: <think> or <thinking> at the start (with possible whitespace)
    open_match = re.search(r'^\s*<think(?:ing)?>', response, re.IGNORECASE)
    if open_match:
        # Unclosed thinking tag - look for double newline as end of thinking
        rest = response[open_match.end():]
        # Try to find where actual content starts (after double newline)
        content_match = re.search(r'\n\n+', rest)
        if content_match:
            return rest[content_match.end():].strip()
        # No clear separator - return empty or minimal response
        return ""

    return response
