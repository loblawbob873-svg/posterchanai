"""Dependency-free language/script filter for relay writes.

No language-detection package is installed (self-contained rule), so we classify by Unicode
script — which maps cleanly to languages for non-Latin writing systems (the actual abuse
cases: Cyrillic/CJK/Arabic/Hebrew/etc. spam). An event is blocked when a blocked language's
share of the *letters* in its content meets a threshold. Latin-script languages aren't
distinguishable by script alone and are intentionally not offered.

`LANGUAGES` (code → UI label) drives the clickable toggles in Admin → Relay.
"""

from collections import defaultdict

# UI-facing toggle set. Codes are stored in the `nostr_relay_blocked_langs` setting (CSV).
LANGUAGES = {
    "ru": "Russian / Cyrillic",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ar": "Arabic",
    "he": "Hebrew",
    "el": "Greek",
    "th": "Thai",
    "hi": "Hindi / Devanagari",
    "hy": "Armenian",
    "ka": "Georgian",
}

_BLOCK_THRESHOLD = 0.20  # ≥20% of letters in a blocked script → reject

# (lo, hi) inclusive Unicode ranges per script bucket.
_RANGES = {
    "cyrillic":   [(0x0400, 0x052F), (0x2DE0, 0x2DFF), (0xA640, 0xA69F)],
    "greek":      [(0x0370, 0x03FF), (0x1F00, 0x1FFF)],
    "arabic":     [(0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
                   (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)],
    "hebrew":     [(0x0590, 0x05FF), (0xFB1D, 0xFB4F)],
    "han":        [(0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x2A6DF)],
    "kana":       [(0x3040, 0x30FF), (0x31F0, 0x31FF)],
    "hangul":     [(0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F)],
    "thai":       [(0x0E00, 0x0E7F)],
    "devanagari": [(0x0900, 0x097F)],
    "armenian":   [(0x0530, 0x058F)],
    "georgian":   [(0x10A0, 0x10FF), (0x1C90, 0x1CBF)],
    "latin":      [(0x0041, 0x024F), (0x1E00, 0x1EFF)],
}


def _script_of(cp: int) -> str | None:
    for script, ranges in _RANGES.items():
        for lo, hi in ranges:
            if lo <= cp <= hi:
                return script
    return None


def detect_languages(text: str) -> set:
    """Return the set of language codes present at/above the block threshold in `text`."""
    if not text:
        return set()
    counts = defaultdict(int)
    letters = 0
    for ch in text:
        sc = _script_of(ord(ch))
        if sc is None:
            continue
        counts[sc] += 1
        letters += 1
    if letters == 0:
        return set()

    # Resolve CJK ambiguity: kana ⇒ Japanese (han counts with it); hangul ⇒ Korean;
    # bare han ⇒ Chinese.
    langs = defaultdict(int)
    langs["ru"] = counts.get("cyrillic", 0)
    langs["el"] = counts.get("greek", 0)
    langs["ar"] = counts.get("arabic", 0)
    langs["he"] = counts.get("hebrew", 0)
    langs["th"] = counts.get("thai", 0)
    langs["hi"] = counts.get("devanagari", 0)
    langs["hy"] = counts.get("armenian", 0)
    langs["ka"] = counts.get("georgian", 0)
    han, kana, hangul = counts.get("han", 0), counts.get("kana", 0), counts.get("hangul", 0)
    if kana:
        langs["ja"] = kana + han
    elif hangul:
        langs["ko"] = hangul
        if han:
            langs["ko"] += han
    elif han:
        langs["zh"] = han
    if hangul and kana:  # mixed: count hangul as Korean too
        langs["ko"] = max(langs["ko"], hangul)

    return {code for code, c in langs.items() if c and (c / letters) >= _BLOCK_THRESHOLD}


def blocked_language(content: str, blocked: set) -> str | None:
    """Return the first blocked language code present in `content`, or None if acceptable."""
    if not blocked:
        return None
    hit = detect_languages(content) & set(blocked)
    return next(iter(hit)) if hit else None
