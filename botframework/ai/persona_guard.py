"""Out-of-character lines never reach a persona bot's post.

A persona bot's reply is published under the character's name. Small models wrap that reply in
their own voice far more often than they refuse it: "This is a post in Judge Dread's likeness.",
"*As an AI, I…*", "(Parody account)", "Disclaimer: …", "— written in the style of …", "Note: …".
Every one of those is the MODEL talking, not the character, and on a timeline it reads as the bot
announcing that it is a bot. The prompt asks the model not to (ai/client.py), and this module is the
deterministic half: whatever the model does, those lines are taken out before anything is posted.

Rules, in order of how much they remove:
  * a bracketed/emphasised aside -- `(...)`, `[...]`, `*...*`, `_..._` -- that is meta is removed whole;
  * otherwise a line is split into sentences and dash clauses, and only the meta pieces go;
  * a line left with nothing but punctuation goes with them.

It REMOVES, never rewrites: the character's own words are kept byte for byte. It is pure (no I/O, no
logging) so the caller decides what an empty result means -- ai/client.py regenerates once and then
posts nothing, because an empty or disclaimer-only post is worse than silence.
"""
import re

_I = re.IGNORECASE

# What the MODEL says about itself or about the post -- never something a character says in a reply.
_AI_NOUN = (r"(?:ai|a\.i\.|artificial\s+intelligence|(?:large\s+)?language\s+model|llm|chat\s*bot|bot"
            r"|ai\s+(?:language\s+model|assistant|model|chatbot)|virtual\s+assistant)")
META_PATTERNS = [re.compile(p, _I) for p in (
    # "As an AI, ...", "I'm just an AI language model"
    r"\bas\s+an?\s+" + _AI_NOUN + r"\b",
    r"\bi(?:'|’)?m\s+(?:just\s+|only\s+|merely\s+)?an?\s+" + _AI_NOUN + r"\b",
    r"\bi\s+am\s+(?:just\s+|only\s+|merely\s+)?an?\s+" + _AI_NOUN + r"\b",
    # "a post in Judge Dread's likeness", "the likeness of"
    r"\b(?:in|of)\s+(?:the\s+)?(?:[\w'’.\-]+\s+){0,4}likeness\b",
    r"\blikeness\s+of\b",
    # "— written in the style of X", "in the voice of X"
    r"\b(?:in|with)\s+the\s+(?:style|voice|persona)\s+of\b",
    r"\b(?:written|posted|composed|presented|crafted|imagined|performed)\s+as\s+(?:if\s+by\s+)?(?:a\s+|an\s+)?"
    r"(?:parody|character|persona|tribute|homage|impression)\b",
    # "This is a parody", "the above is satire", "(Parody account)", "fan account"
    r"\b(?:this|the\s+above|the\s+following|that|it)\s+(?:post\s+|response\s+|reply\s+|message\s+|account\s+|content\s+)?"
    r"(?:is|was)\s+(?:a\s+|an\s+|purely\s+|just\s+|only\s+|meant\s+as\s+(?:a\s+)?|intended\s+as\s+(?:a\s+)?)?"
    r"(?:parody|satire|satirical|fictional|fiction|joke|role-?play|work\s+of\s+fiction|impersonation|in[\s-]character|character\s+post)\b",
    r"\b(?:parody|satire|satirical|fan|role-?play|fictional|impersonation|novelty)\s+"
    r"(?:account|post|bot|content|persona|response|portrayal|piece|reply)\b",
    r"\bis\s+a\s+fictional\s+character\b",
    # "(in character)", "staying in character", "out of character", "OOC"
    r"\b(?:in|out\s+of|breaking|break|staying\s+in|stay\s+in|stays\s+in|remaining\s+in)\s+character\b",
    r"\b(?:role-?playing|portraying|impersonating|playing\s+the\s+(?:role|character|part)\s+of)\b",
    # "Disclaimer: ...", "Note: ...", "Author's note: ...", "OOC: ..."
    r"^\W*(?:disclaimer|note|n\.\s?b\.|nb|author(?:'|’)?s\s+note|editor(?:'|’)?s\s+note|ooc)\s*[:：\-—–]",
    r"\bdisclaimer\b",
    r"\b(?:does|do)\s+not\s+(?:reflect|represent|express)\s+(?:the\s+|my\s+|any\s+)?(?:real|actual|true|personal|views|opinions|beliefs)",
    r"\bnot\s+(?:my|the|anyone(?:'|’)?s)\s+(?:real|actual|personal|own)\s+(?:views|opinions|beliefs)\b",
    r"\bfor\s+(?:entertainment|comedic|comedy|satirical|humou?r|parody)\s+purposes\b",
    r"\bnot\s+(?:affiliated|endorsed|associated)\s+(?:with|by)\b",
)]

# Inside an aside a bare word is enough: "(Parody)", "*in character*", "[satire]".
_ASIDE_WORDS = re.compile(r"\b(?:parody|satire|satirical|in[\s-]character|ooc|likeness|disclaimer|fictional)\b", _I)
_ASIDES = re.compile(r"\(([^()\n]{1,300})\)|\[([^\[\]\n]{1,300})\]|\*{1,2}([^*\n]{1,300})\*{1,2}|(?<!\w)_([^_\n]{1,300})_(?!\w)")

# Sentence ends and dash clauses. Kept in the split so the kept pieces are rejoined exactly.
_SEPARATORS = re.compile(r"((?<=[.!?…])\s+|\s+[—–]+\s*|\s+--?\s+|^[—–]+\s*)")

# Nothing a reader could call a reply: punctuation, quotes, emphasis markers, dashes.
_NOTHING_LEFT = re.compile(r"^[\s\W_]*$")

_REFUSAL = re.compile(
    r"\b(?:i\s+(?:can(?:no|'|’)?t|cannot|won(?:'|’)?t|will\s+not|(?:am|'m|’m)\s+(?:not\s+able|unable))\s+"
    r"(?:help|assist|comply|create|write|generate|produce|promote|engage|provide|do\s+that|fulfil)"
    r"|(?:i(?:'|’)?m\s+sorry|i\s+apologi[sz]e),?\s+but\b)", _I)


def is_meta(text: str) -> bool:
    """Is this piece of text the model talking about itself or the post, rather than the character?"""
    t = (text or "").strip()
    return bool(t) and any(p.search(t) for p in META_PATTERNS)


def looks_like_refusal(text: str) -> bool:
    """The model declining the request. Not something to regenerate past -- the caller posts nothing."""
    return bool(_REFUSAL.search(text or ""))


def _drop_asides(line: str):
    dropped = 0

    def sub(m):
        nonlocal dropped
        inner = next(g for g in m.groups() if g is not None)
        if is_meta(inner) or _ASIDE_WORDS.search(inner):
            dropped += 1
            return ""
        return m.group(0)
    return _ASIDES.sub(sub, line), dropped


def _drop_sentences(line: str):
    parts = _SEPARATORS.split(line)
    # parts = [seg, sep, seg, sep, ...]; a separator belongs to the segment AFTER it.
    out, dropped, sep = [], 0, ""
    for i, piece in enumerate(parts):
        if i % 2:
            sep = piece
            continue
        if not piece:
            continue
        if is_meta(piece):
            dropped += 1
        else:
            out.append((sep if out else "") + piece)
        sep = ""
    return "".join(out), dropped


def strip_persona_meta(text: str):
    """(text with every out-of-character piece removed, how many pieces were removed).

    The returned text is "" when nothing the character said is left."""
    if not text:
        return "", 0
    kept, dropped = [], 0
    for line in str(text).split("\n"):
        if not line.strip():
            kept.append("")
            continue
        new, n1 = _drop_asides(line)
        new, n2 = _drop_sentences(new)
        if n1 or n2:
            dropped += n1 + n2
            new = re.sub(r"[ \t]{2,}", " ", new).strip()
            new = re.sub(r"\s+([.,!?;:])", r"\1", new)
            if _NOTHING_LEFT.match(new):
                continue
        kept.append(new)
    out = re.sub(r"\n\s*\n\s*\n+", "\n\n", "\n".join(kept)).strip()
    if _NOTHING_LEFT.match(out):
        out = ""
    return out, dropped
