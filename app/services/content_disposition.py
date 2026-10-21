"""One Content-Disposition builder for every download this app serves.

Header values are encoded latin-1 by the ASGI layer, so a filename straight from a person -- a
Telegram attachment, a mail attachment, an addressbook called «Семья» -- raised UnicodeEncodeError and
the download became a 500 (`'latin-1' codec can't encode characters in position 18-20`: position 18 is
the first character after `inline; filename="`). RFC 6266/5987 is the way out: an ASCII-only
`filename=` for old clients plus a percent-encoded `filename*=` that every current browser prefers.
"""
import re
from urllib.parse import quote

# Never inside the header: quotes/backslashes end the quoted string, CR/LF/NUL split the header, and a
# path separator would name a directory on the client.
_UNSAFE = re.compile(r'[\x00-\x1f\x7f"\\/]+')


def content_disposition(disp: str, name: str, fallback: str = "download") -> str:
    name = _UNSAFE.sub("_", str(name or "")).strip() or fallback
    ascii_name = name.encode("ascii", "ignore").decode("ascii").strip()
    # A name that was ALL non-ASCII keeps its extension in the fallback, so an old client still
    # opens 写真.png as a picture rather than an anonymous "download".
    if not ascii_name or ascii_name.startswith("."):
        ascii_name = fallback + (ascii_name if ascii_name.startswith(".") else "")
    out = f'{disp}; filename="{ascii_name}"'
    if ascii_name != name:
        out += "; filename*=UTF-8''" + quote(name, safe="")
    return out
