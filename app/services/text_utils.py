"""Shared text processing utilities."""
import re
from typing import List


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


# Pre-compiled regex patterns for efficiency
_THINKING_CLOSE_PATTERN = re.compile(r'</think(?:ing)?>', re.IGNORECASE)
_THINKING_OPEN_PATTERN = re.compile(r'^\s*<think(?:ing)?>', re.IGNORECASE)


def strip_thinking_tags_fast(response: str) -> str:
    """Strip thinking tags using pre-compiled regex (faster for repeated calls)."""
    matches = list(_THINKING_CLOSE_PATTERN.finditer(response))
    if matches:
        last_match = matches[-1]
        return response[last_match.end():].strip()

    # Check for unclosed opening tag
    open_match = _THINKING_OPEN_PATTERN.search(response)
    if open_match:
        rest = response[open_match.end():]
        # Try to find where actual content starts (after double newline)
        content_match = re.search(r'\n\n+', rest)
        if content_match:
            return rest[content_match.end():].strip()
        return ""

    return response
