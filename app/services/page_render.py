"""Read a page the way a browser reads it — for any URL, no per-site knowledge.

`SearchService.fetch_url_content` reads what the SERVER sent. On a site that builds its page in
JavaScript that is a SHELL, and the reader gets nothing at all: measured on this deployment,

    https://www.cnn.com/            7260 chars extracted
    https://en.wikipedia.org/…      6878
    https://news.ycombinator.com/   2828
    https://poster.place/              0     <-- an SPA, and every route under it

Zero characters is not "an empty page" to a model — the question survives and it answers from its
own priors, which is where "check this person's posts" became a character study of a famous person
who happened to share a first name. So when extraction comes back empty, we RENDER: headless
Chrome, the page's own scripts, then `document.body.innerText`. That is generic — it is the same
answer a human gets by opening the link — and it needs no knowledge of what the site is.

Driven over CDP directly (websocket-client), NOT Selenium/chromedriver: chromedriver's launch
handshake stalls ~120s on this host while Chrome's own DevTools endpoint is ready in ~1s. Same
launcher as the screenshot command's, which has been in production for a long time.

Everything here BLOCKS (subprocess, sockets, sleeps). Call it from async code with
`asyncio.to_thread`, never directly on the event loop.
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Below this many characters of extracted text, a page has told us nothing and is worth the cost of
# a render. Sized off the measurements above: the thinnest REAL page here extracts ~2800 chars and
# an unrendered shell extracts 0, so anything in between is a wide, quiet margin.
UNRENDERED_TEXT_CHARS = 400

# A render costs a browser launch plus the page's own load. Measured 7.1s on the SPA above, so the
# ceiling has to sit under what the callers allow: chat/openai/telegram each wrap URL fetching in
# asyncio.wait_for(..., timeout=15).
RENDER_TIMEOUT = 11.0

POLL_SECONDS = 0.3
# EVERY page gets at least this long, however quiet it goes. This is the floor, and it is the part
# that makes the output consistent: a quiet interval alone cannot tell "finished" from "between
# bursts", and a client that fetches over a WEBSOCKET does not even move the browser's own
# network-idle signal — so there is no event to wait for, only time. Measured, a flat 7s settle and
# a 3s floor read the same profile at the same size; without a floor the same url came back at 463,
# 9823, 14155 and 15657 chars depending on how busy the box was.
MIN_RENDER_SECONDS = 3.0
# After the floor, stop once the text has stopped growing for this long.
QUIET_SECONDS = 1.2
# …and a page still holding almost nothing is far more likely to be mid-load than to be that empty,
# so it has to stay quiet for longer before we accept it. This is the 463-char reading.
THIN_PAGE_CHARS = 1200
THIN_QUIET_SECONDS = 3.0


def chrome_available() -> bool:
    return find_chrome() is not None


def find_chrome() -> Optional[str]:
    """Locate a Chrome/Chromium binary, or None.

    Shared with the screenshot command rather than copied — a second list of binary names is how
    one surface silently stops finding the browser the other one uses.
    """
    try:
        from app.services.command_service._common import _find_chrome
        return _find_chrome()
    except Exception:
        return (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
                or shutil.which("chromium") or shutil.which("chromium-browser"))


def looks_unrendered(text: str) -> bool:
    """True when extraction produced so little that reading the page in a browser is worth it."""
    return len((text or "").strip()) < UNRENDERED_TEXT_CHARS


def render_page_text(url: str, timeout: float = RENDER_TIMEOUT,
                     is_allowed=None) -> Optional[Tuple[str, str]]:
    """Return (title, visible text) for `url` as a browser would see it, or None.

    None means "could not render" — no browser, a launch failure, or a page that never produced
    text before the deadline. The caller must treat that as NO INFORMATION and never as an empty
    page, which is the same distinction every other reader in this app draws.

    `is_allowed(final_url)` re-runs the caller's SSRF guard on where the page ENDED UP. The fetcher
    checks every HTTP redirect hop itself, but a browser also follows redirects the page performs
    in script — so a public URL can land on the metadata endpoint, and without this the text of
    whatever it found there would be handed back as "the page". Where the guard says no, the render
    is discarded.
    """
    chrome = find_chrome()
    if not chrome:
        return None

    deadline = time.time() + timeout
    tmp_profile = tempfile.mkdtemp(prefix="pagerender_")
    proc = None
    reaped_early = False
    try:
        proc = subprocess.Popen(
            [chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--disable-dev-shm-usage", "--hide-scrollbars",
             "--no-first-run", "--no-default-browser-check",
             "--disable-background-networking", "--disable-component-update",
             "--disable-sync", "--metrics-recording-only", "--mute-audio",
             "--window-size=1280,2000",
             f"--user-data-dir={tmp_profile}",
             # Newer Chrome rejects DevTools websocket connections whose Origin isn't
             # allow-listed; we connect locally, so allow all origins.
             "--remote-allow-origins=*",
             "--remote-debugging-port=0", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,  # own process group → clean kill of all children
        )

        port_file = os.path.join(tmp_profile, "DevToolsActivePort")
        port = None
        while time.time() < deadline:
            if os.path.exists(port_file):
                try:
                    port = int(open(port_file).read().splitlines()[0])
                    break
                except (ValueError, IndexError, OSError):
                    pass
            if proc.poll() is not None:
                reaped_early = True
                logger.warning("page_render: chrome exited before the DevTools port was ready")
                return None
            time.sleep(0.05)
        if not port:
            logger.warning("page_render: chrome did not expose a DevTools port in time")
            return None

        import websocket  # websocket-client (sync)

        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5))
        ws_url = next((t["webSocketDebuggerUrl"] for t in targets
                       if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
        if not ws_url:
            return None

        ws = websocket.create_connection(ws_url, timeout=max(1.0, deadline - time.time()))
        msg_id = [0]

        def cmd(method: str, params: Optional[dict] = None) -> dict:
            msg_id[0] += 1
            mine = msg_id[0]
            ws.send(json.dumps({"id": mine, "method": method, "params": params or {}}))
            while True:
                frame = json.loads(ws.recv())
                if frame.get("id") == mine:
                    return frame

        try:
            cmd("Page.enable")
            cmd("Page.navigate", {"url": url})

            # Poll rather than sleeping a flat settle: a plain page is done in well under a second,
            # while an SPA that opens a websocket for its content needs several.
            #
            # WHEN TO STOP IS THE WHOLE RELIABILITY OF THIS FUNCTION, and the first rule here — "two
            # polls without growth" — was a race. On an idle box it read a profile at a steady
            # ~15000 chars, five runs out of five; on a busy one the SAME url came back at 463,
            # 9823, 14155 and 15657 chars, because a page that is still fetching goes quiet between
            # bursts and 0.8s of quiet is not "finished". The answer the model gives is only as
            # consistent as what it was handed, so the rule is now: quiet for a real interval, and a
            # page still holding almost nothing has to be quiet for MUCH longer before we believe
            # that is all there is.
            best, title, landed = "", "", url
            started = last_growth = time.time()
            while time.time() < deadline:
                time.sleep(POLL_SECONDS)
                try:
                    r = cmd("Runtime.evaluate", {
                        "expression": "[document.title||'', document.body?document.body.innerText:''"
                                      ", location.href||'']",
                        "returnByValue": True})
                    val = ((r.get("result") or {}).get("result") or {}).get("value") or ["", "", ""]
                    title, text = str(val[0] or ""), str(val[1] or "")
                    landed = str(val[2] or "") or landed
                except Exception:
                    continue
                if is_allowed is not None and not is_allowed(landed):
                    logger.warning("page_render: %s ended up at %s, which the guard refuses",
                                   url, landed)
                    return None
                if len(text) > len(best):
                    best, last_growth = text, time.time()
                    continue
                if not best.strip() or time.time() - started < MIN_RENDER_SECONDS:
                    continue
                quiet = time.time() - last_growth
                if quiet >= (THIN_QUIET_SECONDS if len(best) < THIN_PAGE_CHARS else QUIET_SECONDS):
                    break
            if not best.strip():
                return None
            return title.strip(), best
        finally:
            try:
                ws.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning("page_render failed for %s: %s", url, e)
        return None
    finally:
        if proc is not None and not reaped_early:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(tmp_profile, ignore_errors=True)
