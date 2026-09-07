"""Recipients explicitly named by NIP-18 quote tags on text notes."""
import re

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def quote_pubkeys(ev: dict) -> set[str]:
    if ev.get("kind") != 1:
        return set()
    return {t[3] for t in (ev.get("tags") or [])
            if isinstance(t, (list, tuple)) and len(t) >= 4 and t[0] == "q"
            and isinstance(t[1], str) and _HEX64.fullmatch(t[1])
            and isinstance(t[3], str) and _HEX64.fullmatch(t[3])}
