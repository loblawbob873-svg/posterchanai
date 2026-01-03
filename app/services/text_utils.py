"""Shared text processing utilities."""
import re
from typing import List


def strip_thinking_tags(response: str) -> str:
    """Strip thinking tags from AI response (used by Qwen and other reasoning models)."""
    matches = list(re.finditer(r'</think(?:ing)?>', response, re.IGNORECASE))
    if matches:
        last_match = matches[-1]
        return response[last_match.end():].strip()
    return response


# Pre-compiled regex pattern for efficiency
_THINKING_TAG_PATTERN = re.compile(r'</think(?:ing)?>', re.IGNORECASE)


def strip_thinking_tags_fast(response: str) -> str:
    """Strip thinking tags using pre-compiled regex (faster for repeated calls)."""
    matches = list(_THINKING_TAG_PATTERN.finditer(response))
    if matches:
        last_match = matches[-1]
        return response[last_match.end():].strip()
    return response
