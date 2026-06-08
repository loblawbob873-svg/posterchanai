"""
Shared utility functions for Posterchan.
URL validation, HTML stripping, and prompt parsing.
"""

import html
import ipaddress
import re
from urllib.parse import urlparse


import socket
import logging

_security_logger = logging.getLogger(__name__)

# Blocked hostnames that could be used for SSRF
_BLOCKED_HOSTNAMES = {
    'localhost', 'localhost.localdomain', 'local',
    'internal', 'internal.local',
    'metadata', 'metadata.google.internal',
    '169.254.169.254',  # AWS/GCP metadata
}

# Blocked TLDs for internal services
_BLOCKED_TLDS = {'.local', '.internal', '.localhost', '.lan', '.home'}


def is_safe_url(url, trusted_hosts=None):
    """Validate URL to prevent SSRF attacks.

    Checks:
    - Valid scheme (http/https only)
    - Not a private/loopback/reserved IP
    - Not a blocked hostname (localhost, internal, metadata, etc.)
    - Resolved IP is not private (prevents DNS rebinding)

    `trusted_hosts` is an optional set/iterable of lowercase hostnames that are
    always allowed (e.g. the bot's OWN instance), since a self-hosted instance
    legitimately resolves to a private/LAN IP. Only exact host matches bypass the
    private-IP check — remote/federated URLs are still validated normally.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False

        hostname = parsed.hostname
        if not hostname:
            return False

        # Normalize hostname
        hostname_lower = hostname.lower()

        # The bot's own instance(s) are trusted even on a LAN IP.
        if trusted_hosts and hostname_lower in trusted_hosts:
            return True

        # Check blocked hostnames
        if hostname_lower in _BLOCKED_HOSTNAMES:
            _security_logger.warning(f"[SECURITY] Blocked hostname: {hostname}")
            return False

        # Check blocked TLDs
        for tld in _BLOCKED_TLDS:
            if hostname_lower.endswith(tld):
                _security_logger.warning(f"[SECURITY] Blocked TLD: {hostname}")
                return False

        # Check if it's a direct IP address
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_reserved:
                _security_logger.warning(f"[SECURITY] Blocked private/internal IP: {hostname}")
                return False
        except ValueError:
            # Not a direct IP - resolve the hostname to check the actual IP
            try:
                resolved_ip = socket.gethostbyname(hostname)
                ip = ipaddress.ip_address(resolved_ip)
                if ip.is_private or ip.is_loopback or ip.is_reserved:
                    _security_logger.warning(f"[SECURITY] Blocked: {hostname} resolves to private IP {resolved_ip}")
                    return False
            except (socket.gaierror, socket.herror):
                # DNS resolution failed - could be attacker-controlled domain
                # Allow for now as it will fail on actual request anyway
                pass

        return True
    except Exception:
        return False


def strip_html(html_text):
    """Remove HTML tags from text and unescape entities.

    Line-break and block tags (<br>, </p>, </div>) become newlines first, so the
    original line structure survives — otherwise e.g. a meme caption typed on two
    lines ("armpits<br>Please!") collapses into one smashed run ("armpitsPlease!").
    """
    if not html_text:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", html_text)
    text = re.sub(r"(?i)</(?:p|div)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)  # collapse runs of blank lines
    return text.strip()


def parse_prompt_modifiers(prompt):
    """
    Extract modifiers from prompt: ,anime and ,d0.7

    Returns: (cleaned_prompt, is_anime, denoise_value)
        - is_anime: True if ,anime was found
        - denoise_value: float if ,d0.7 specified, else None (use default)
                         Values > 1 are treated as percentages (e.g., 70 -> 0.7)
                         Result is clamped to range [0.1, 0.95]

    Examples:
        'make her a cat ,anime' -> ('make her a cat', True, None)
        'nude ,d0.7' -> ('nude', False, 0.7)
        'anime style ,anime ,d0.65' -> ('anime style', True, 0.65)
        'test ,d70' -> ('test', False, 0.7)  # 70 converted to 0.7
    """
    cleaned = prompt
    is_anime = False
    denoise = None

    # Check for ,anime
    anime_match = re.search(r',\s*anime\b', cleaned, re.IGNORECASE)
    if anime_match:
        is_anime = True
        cleaned = cleaned[:anime_match.start()] + cleaned[anime_match.end():]

    # Check for ,d0.7 or ,denoise:0.7
    denoise_match = re.search(r',\s*d(?:enoise)?[:\s]*(0?\.\d+|\d+(?:\.\d+)?)', cleaned, re.IGNORECASE)
    if denoise_match:
        try:
            denoise = float(denoise_match.group(1))
            if denoise > 1:
                denoise = denoise / 100
            denoise = max(0.1, min(0.95, denoise))
            cleaned = cleaned[:denoise_match.start()] + cleaned[denoise_match.end():]
        except ValueError:
            pass

    return cleaned.strip(), is_anime, denoise


from config import BAD_WORDS

# Whole-word patterns for the prohibited-word filter (compiled once at import).
_BAD_WORDS_PATTERNS = [re.compile(r'\b' + re.escape(bw.lower()) + r'\b') for bw in BAD_WORDS]


def contains_bad_words(text: str) -> bool:
    """Return True if text contains any prohibited word (whole-word match)."""
    lower_text = text.lower()
    return any(pattern.search(lower_text) for pattern in _BAD_WORDS_PATTERNS)
