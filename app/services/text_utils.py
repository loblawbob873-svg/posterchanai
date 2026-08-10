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


def inject_no_think(messages: list[dict]) -> list[dict]:
    """Append /no_think to the last user message to disable Qwen3 thinking mode.

    Qwen3 models only respect /no_think when it appears in the user turn, not
    just the system prompt.  This is safe to call unconditionally — non-Qwen3
    models will simply treat it as trailing text.
    """
    messages = [dict(m) for m in messages]
    for i in reversed(range(len(messages))):
        if messages[i].get("role") == "user":
            content = messages[i].get("content", "")
            if isinstance(content, str):
                if "/no_think" not in content:
                    messages[i]["content"] = content.rstrip() + " /no_think"
            elif isinstance(content, list):
                for j in reversed(range(len(content))):
                    part = content[j]
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        if "/no_think" not in text:
                            content[j] = {**part, "text": text.rstrip() + " /no_think"}
                        break
            break
    return messages


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


# ---------------------------------------------------------------------------------------------
# Preamble stripping — enforcing "output ONLY the text" instead of merely asking for it.
#
# Six prompts in this codebase end with some form of "no preamble". None of them enforced it, and
# an instruction is not a guarantee: a small local model complies most of the time and then returns
#
#     Here's one that hits the key points naturally:
#     ---
#     <the actual post>
#
# which is what landed in the composer, verbatim, when someone pressed the composer's AI Enhancer.
# That endpoint's prompt already said *do NOT add a preamble like 'Here is a post'*. Writing it a
# seventh time, more firmly, is not a fix; the contract can only be enforced on the way out.
#
# The whole risk of a cleaner like this is over-reach — a leading line that LOOKS like scaffolding
# but is the user's actual content — so the rules are deliberately narrow:
#
#   * a lead-in must both READ like one (a small vocabulary of meta openers) and be SHORT and END in
#     a colon. Prose merely starting "Here's why the bill matters." is none of those and is untouched;
#   * nothing is ever stripped down to nothing — if the rules would empty the text the original is
#     returned, because a preamble that ate the answer is much worse than a preamble;
#   * a horizontal rule goes only where it was separating removed scaffolding, never from the body.
# ---------------------------------------------------------------------------------------------

# The meta-opener vocabulary. Matched at the START of a candidate lead-in line, case-insensitively.
# Each is something a model says ABOUT the answer it is giving, never something a post opens with —
# and each still has to pass the length and colon tests below before anything is removed.
_LEAD_IN = re.compile(
    r"^\s*(?:"
    r"sure|certainly|absolutely|of course|got it|okay|ok"
    r"|here(?:'|’)?s|here is|here are"
    r"|below is|below are|this is"
    r"|i(?:'|’)?ve|i have|i(?:'|’)?ll|i will|i(?:'|’)?d|let me"
    r"|hope this|happy to"
    r")\b",
    re.I,
)

# A line that is nothing but a horizontal rule.
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,}|={3,})\s*$")

# Trailing "want me to change anything?" offers — the same scaffolding at the other end. Narrower,
# because a real post can end with a question: this must be first-person AND about the text itself.
_TRAILING_OFFER = re.compile(
    r"^\s*(?:let me know|want me to|would you like|feel free to|i can (?:also )?(?:tweak|adjust|shorten|expand|rewrite))\b",
    re.I,
)

_MAX_LEAD_IN_CHARS = 120   # a lead-in is one short sentence; anything longer is somebody's paragraph


def _strip_code_fence(text: str) -> str:
    """Unwrap ```…``` when it wraps the WHOLE answer.

    A model asked for plain prose sometimes returns it fenced. Only a fence that opens the text and
    closes it is removed — a fence around one block INSIDE a post is part of the post.
    """
    lines = text.strip().split("\n")
    if len(lines) < 2 or not lines[0].lstrip().startswith("```"):
        return text
    # The opening fence may carry a language tag; the closing one must be a bare fence.
    if lines[-1].strip() != "```":
        return text
    return "\n".join(lines[1:-1])


def strip_preamble(text: str) -> str:
    """Remove model scaffolding from around an answer that should be prose and nothing else.

    Returns the text unchanged when the rules find nothing, and — importantly — when applying them
    would leave nothing at all.
    """
    if not text:
        return text
    original = text
    out = _strip_code_fence(text).strip()

    # Leading scaffolding, at most a couple of rounds: "Sure!" then "Here's the post:" happens, an
    # endless chain does not, and a loop with no bound would be a way to delete a whole document.
    for _ in range(3):
        lines = out.split("\n")
        if len(lines) < 2:
            break                                   # never strip the only line — that IS the answer
        first = lines[0].strip()
        if not first:
            out = "\n".join(lines[1:]).lstrip("\n")
            continue
        if _RULE.match(first):
            # A rule at the very top is scaffolding on its own: nothing above it to separate.
            out = "\n".join(lines[1:]).lstrip("\n")
            continue
        if (_LEAD_IN.match(first) and first.endswith(":") and len(first) <= _MAX_LEAD_IN_CHARS):
            out = "\n".join(lines[1:]).lstrip("\n")
            continue
        break

    # A trailing offer to revise, plus any rule that was separating it.
    lines = out.split("\n")
    while len(lines) > 1:
        last = lines[-1].strip()
        if not last or _RULE.match(last) or _TRAILING_OFFER.match(last):
            lines.pop()
            continue
        break
    out = "\n".join(lines)

    # Straight quotes wrapped round the entire answer.
    stripped = out.strip()
    if len(stripped) > 1 and stripped[0] == '"' and stripped[-1] == '"' and stripped.count('"') == 2:
        out = stripped[1:-1]

    out = out.strip()
    return out or original.strip()
