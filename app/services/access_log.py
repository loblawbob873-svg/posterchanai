"""The access log never carries a credential that travels in a URL.

A Concord tunnel is a websocket, and a websocket cannot set headers from a browser, so its member ticket rides the
query string (`/ws/concord-relay?u=...&t=<ticket>`). uvicorn's access log printed that whole path, i.e. a one-hour
credential for every tunnel into the journal (seen 2026-10-28). The value is blanked; the request stays legible.
"""
import logging
import re

SECRET_PARAMS = ("t", "ticket", "token")
_RX = re.compile(r"([?&](?:%s)=)[^&\s\"]*" % "|".join(SECRET_PARAMS))


def redact(path: str) -> str:
    return _RX.sub(r"\1…", path) if "?" in path else path


class RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            args = record.args
            if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
                record.args = args[:2] + (redact(args[2]),) + args[3:]
        except Exception:
            pass   # a log line is never refused for this
        return True


def install() -> None:
    log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactSecrets) for f in log.filters):
        log.addFilter(RedactSecrets())
