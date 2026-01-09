"""Shared text processing utilities."""
import re


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

    result = cleaned.strip()

    # If everything was stripped, return a fallback message
    if not result:
        return "I apologize, I wasn't able to generate a proper response. Please try again."

    return result
