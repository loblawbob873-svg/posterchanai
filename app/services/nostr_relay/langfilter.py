"""Dependency-free language/script filter for relay writes.

No language-detection package is installed (self-contained rule), so we classify by Unicode
script — which maps cleanly to languages for non-Latin writing systems (the actual abuse
cases: Cyrillic/CJK/Arabic/Hebrew/etc. spam). An event is blocked when a blocked language's
share of the *letters* in its content meets a threshold. Latin-script languages aren't
distinguishable by script alone and are intentionally not offered.

`LANGUAGES` (code → UI label) drives the clickable toggles in Admin → Relay.
"""

import re
from collections import defaultdict

# URLs, nostr: URIs and bech32/Lightning entities are long runs of Latin/base32 characters that
# are NOT language — left in, they inflate the letter count and dilute a short non-Latin note
# below the block threshold (e.g. a Japanese line + an image URL reads as 11% Japanese). Strip
# them before detection so the ratio reflects the actual prose.
_NOISE_RE = re.compile(
    r"https?://\S+"
    r"|nostr:\S+"
    r"|\b(?:npub|note|nevent|naddr|nprofile|lnbc|lnurl)1[0-9ac-hj-np-z]+"
    r"|:[a-z0-9_+-]+:",   # custom-emoji shortcodes like :meow_bongo_keyboard:
    re.IGNORECASE,
)

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
    "vi": "Vietnamese",
}

_BLOCK_THRESHOLD = 0.20  # ≥20% of letters in a blocked script → reject
# …OR an absolute run of blocked-script chars — catches bilingual spam (a full CJK sentence + an
# English translation) where the CJK is diluted below the ratio. 6 chars = a real phrase, not a
# stray foreign name/word.
_BLOCK_ABS_MIN = 6

# (lo, hi) inclusive Unicode ranges per script bucket.
_RANGES = {
    "cyrillic":   [(0x0400, 0x052F), (0x2DE0, 0x2DFF), (0xA640, 0xA69F)],
    "greek":      [(0x0370, 0x03FF), (0x1F00, 0x1FFF)],
    "arabic":     [(0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
                   (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)],
    "hebrew":     [(0x0590, 0x05FF), (0xFB1D, 0xFB4F)],
    "han":        [(0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x2A6DF)],
    "kana":       [(0x3040, 0x30FF), (0x31F0, 0x31FF), (0xFF65, 0xFF9F)],  # incl. halfwidth katakana
    "hangul":     [(0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F)],
    "thai":       [(0x0E00, 0x0E7F)],
    "devanagari": [(0x0900, 0x097F)],
    "armenian":   [(0x0530, 0x058F)],
    "georgian":   [(0x10A0, 0x10FF), (0x1C90, 0x1CBF)],
    "latin":      [(0x0041, 0x024F), (0x1E00, 0x1EFF)],
}


# Vietnamese is Latin-script, so it can't be told apart by script the way the others can.
# But its tone-marked vowels (U+1EA0–1EF9) plus đ/ư/ơ/ă are essentially unique to it among
# Latin-using languages, so a couple of them is a strong, low-false-positive signal even when
# the note mixes in English (names, hashtags, "World Cup", …).
_VIET_CHARS = frozenset(
    {0x0102, 0x0103, 0x0110, 0x0111, 0x01A0, 0x01A1, 0x01AF, 0x01B0}  # Ăă Đđ Ơơ Ưư
    | set(range(0x1EA0, 0x1EFA))                                      # tone-marked vowels
)
_VIET_MIN = 2  # this many distinctive chars in a note ⇒ Vietnamese


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
    text = _NOISE_RE.sub(" ", text)  # drop URLs/refs/emoji shortcodes so they don't dilute the ratio
    counts = defaultdict(int)
    letters = 0
    viet = 0
    for ch in text:
        cp = ord(ch)
        if cp in _VIET_CHARS:
            viet += 1
        sc = _script_of(cp)
        if sc is None:
            continue
        counts[sc] += 1
        letters += 1
    if letters == 0:
        return {"vi"} if viet >= _VIET_MIN else set()

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

    found = {code for code, c in langs.items()
             if c and ((c / letters) >= _BLOCK_THRESHOLD or c >= _BLOCK_ABS_MIN)}
    if viet >= _VIET_MIN:
        found.add("vi")
    return found


def blocked_language(content: str, blocked: set) -> str | None:
    """Return the first blocked language code present in `content`, or None if acceptable."""
    if not blocked:
        return None
    hit = detect_languages(content) & set(blocked)
    return next(iter(hit)) if hit else None


def blocked_word(content: str, words) -> str | None:
    """Return the first blocked word/phrase found in `content` (case-insensitive substring),
    or None. `words` should already be lowercased. Used to reject notes containing banned text."""
    if not words or not content:
        return None
    low = content.lower()
    for w in words:
        if w and w in low:
            return w
    return None
