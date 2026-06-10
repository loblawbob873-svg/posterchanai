import json
import logging
import re
import threading
from typing import TYPE_CHECKING, Callable, Optional, Tuple
from datetime import datetime

from sqlalchemy.orm import Session

from app.routers.news import fetch_news_from_source, get_user_news_sources
from app.services.chat_service import ChatService
from app.services.proxy_image_cache import register as proxy_image_register
from app.services.image_factory import generate_image_for_user
from app.services.mail_service import (
    archive_message,
    delete_all_messages,
    delete_message,
    fetch_all_accounts,
    fetch_messages,
    format_folder_list,
    format_message_detail,
    format_message_list,
    forward_message,
    get_attachment,
    get_message_by_id,
    get_user_mail_accounts,
    list_folders,
    reply_to_message,
    search_messages,
    send_email,
)
from app.services.nyaa_service import NyaaResult, format_nyaa_results, search_nyaa
from app.services.search_service import SearchService
from app.services.torrent_service import (
    TorrentResult,
    format_all_categories,
    format_torrent_results,
    scrape_all_categories,
    scrape_torrents,
    search_torrents,
)
from app.services.youtube_service import (
    check_ytdlp_available,
    download_video_and_save_to_storage,
    download_mp3_and_save_to_storage,
    extract_download_urls,
    extract_youtube_urls,
    format_download_result,
    is_youtube_url,
    summarize_youtube,
)

# Lock now handled inside image_factory for fine-grained control

if TYPE_CHECKING:
    from app.models import User

logger = logging.getLogger(__name__)


def _find_firefox() -> Optional[str]:
    """Locate a Firefox binary, or None. Prefers PATH, then common install dirs."""
    import os
    import shutil
    return (
        shutil.which("firefox")
        or shutil.which("firefox-bin")
        or next((c for c in ("/opt/firefox/firefox", "/usr/bin/firefox-bin",
                             "/usr/bin/firefox") if os.path.exists(c)), None)
    )


def _find_chrome() -> Optional[str]:
    """Locate a Chrome/Chromium binary, or None. Prefers PATH, then common dirs."""
    import os
    import shutil
    return (
        shutil.which("google-chrome-stable")
        or shutil.which("google-chrome")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("chrome")
        or next((c for c in ("/opt/google/chrome/google-chrome",
                             "/opt/google/chrome/chrome",
                             "/usr/bin/google-chrome-stable",
                             "/usr/bin/chromium") if os.path.exists(c)), None)
    )


# Serializes browser captures across the whole process: Firefox needs it because it
# uses its default profile for --screenshot (a fresh `-profile` dir hangs headless,
# and the default profile has a single-instance lock); Chrome needs it because each
# headless instance is ~350MB and concurrent captures (user screenshots + the
# nitter/social pollers' card renders) otherwise spike memory and crash launches.
_screenshot_lock = threading.Lock()


# A blank/unmounted page compresses to a tiny PNG; this many real seconds of settle
# after load lets JS-heavy SPAs (Pleroma/Mastodon/Misskey timelines, etc.) hydrate
# before we capture. Overridable via SCREENSHOT_SETTLE_SECONDS.
def _screenshot_settle() -> float:
    import os
    try:
        return max(0.0, float(os.environ.get("SCREENSHOT_SETTLE_SECONDS", "5")))
    except ValueError:
        return 5.0


def _url_is_safe_to_fetch(url: str, allowed_hosts: Optional[list] = None) -> bool:
    """SSRF guard for the screenshot command (reachable by untrusted Misskey/Pleroma
    users, not just trusted Matrix/web users).

    Resolves the URL's host and refuses any that maps to a private, loopback,
    link-local, reserved, multicast or unspecified address — including the cloud
    metadata endpoint 169.254.169.254 (covered by link-local). All resolved
    addresses are checked so a host that returns both a public and an internal IP
    is still rejected. Mirrors `mail_service.validate_mail_server`'s approach.

    `allowed_hosts` is the admin-configured allowlist (`screenshot_allowed_hosts`
    setting): the operator's own domains that legitimately resolve to a LAN/private IP
    via split-horizon DNS (e.g. poster.place → 192.168.x.x) and so would otherwise be
    refused. Matched by exact host OR as a parent domain (so `poster.place` also allows
    `www.poster.place`). An allowlisted host bypasses the private-IP check.

    Note: this is resolve-then-check, so it does not fully close a DNS-rebinding
    race where the browser later re-resolves to a different IP — matching the
    existing guard's threat model. Returns True only if every resolved IP is public.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    for allowed in (allowed_hosts or []):
        a = (allowed or "").strip().lower().lstrip("*").lstrip(".").rstrip(".")
        if a and (host == a or host.endswith("." + a)):
            return True  # operator's own domain — trusted despite a private IP
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False  # unresolvable → refuse rather than hand a raw host to the browser
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        # Normalize IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) so the IPv4 checks apply.
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _capture_full_page_chrome(chrome: str, url: str, width: int, timeout: int, tight: bool = False) -> bytes:
    """Render `url` in headless Chrome (driven over CDP) and return a full-page PNG.

    Launches Chrome with a remote-debugging port and talks the DevTools protocol
    directly (websocket-client) instead of using Selenium/chromedriver — chromedriver's
    launch handshake stalls ~120s on this host, while Chrome's own DevTools endpoint is
    ready in ~1s. Navigates, waits a real settle for JS/SPA hydration, then captures
    with `captureBeyondViewport` for the FULL page height (not just the viewport).
    """
    import base64
    import json
    import os
    import shutil
    import signal
    import subprocess
    import tempfile
    import time
    import urllib.request

    import websocket  # websocket-client (sync)

    tmp_profile = tempfile.mkdtemp(prefix="chromeshot_")
    proc = None
    reaped_early = False  # True once proc.poll() below reaps the leader → its pid is freed
    try:
        proc = subprocess.Popen(
            [chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--disable-dev-shm-usage", "--hide-scrollbars",
             "--no-first-run", "--no-default-browser-check",
             "--disable-background-networking", "--disable-component-update",
             "--disable-sync", "--metrics-recording-only", "--mute-audio",
             f"--window-size={width},1200",
             f"--user-data-dir={tmp_profile}",
             # Newer Chrome rejects DevTools websocket connections whose Origin isn't
             # allow-listed; we connect locally so allow all origins.
             "--remote-allow-origins=*",
             "--remote-debugging-port=0", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,  # own process group → clean kill of all children
        )

        # Chrome writes the chosen port to DevToolsActivePort once it's ready (~1s).
        port_file = os.path.join(tmp_profile, "DevToolsActivePort")
        port = None
        for _ in range(150):  # up to ~15s
            if os.path.exists(port_file):
                try:
                    port = int(open(port_file).read().splitlines()[0])
                    break
                except (ValueError, IndexError):
                    pass
            if proc.poll() is not None:
                reaped_early = True  # poll() just reaped the leader; pid no longer ours
                raise RuntimeError("Chrome exited before the DevTools port was ready")
            time.sleep(0.1)
        if not port:
            raise RuntimeError("Chrome did not expose a DevTools port in time")

        # Find the page target's websocket URL.
        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=10))
        ws_url = next((t["webSocketDebuggerUrl"] for t in targets
                       if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
        if not ws_url:
            raise RuntimeError("No Chrome page target to attach to")

        ws = websocket.create_connection(ws_url, timeout=timeout)
        try:
            msg_id = 0

            def cmd(method, params=None):
                nonlocal msg_id
                msg_id += 1
                mid = msg_id
                ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                while True:  # skip async events until our command's reply arrives
                    reply = json.loads(ws.recv())
                    if reply.get("id") == mid:
                        return reply

            cmd("Page.enable")
            # Many sites (CNN, Fox, …) serve a blank/"Unknown Error" body to the
            # default headless UA, which then screenshots as a white page. Strip the
            # "Headless" marker from Chrome's OWN UA string (so it auto-tracks the
            # installed version instead of a hardcoded one that goes stale) and apply
            # it before navigating.
            try:
                ua = ((cmd("Browser.getVersion").get("result") or {}).get("userAgent") or "")
                ua = ua.replace("HeadlessChrome", "Chrome")
                if ua:
                    cmd("Emulation.setUserAgentOverride", {"userAgent": ua})
            except Exception:
                pass  # non-fatal: fall back to the default UA
            cmd("Page.navigate", {"url": url})
            time.sleep(_screenshot_settle())  # real wall-clock for JS/network to settle
            # Full-page (non-tight) capture: size the viewport to the WHOLE page so
            # every section — including lazy-loaded, below-the-fold images — renders in
            # place. We deliberately do NOT scroll: virtualized pages (CNN) drop
            # off-screen content when scrolled back and collapse their height at capture
            # time, and `captureBeyondViewport` alone under-captures them. Height is
            # driven by scrollHeight, NOT getLayoutMetrics/cssContentSize, which
            # under-reports for pages (CNN) whose content overflows a short <body>.
            full_h = 0
            if not tight:
                def _page_height():
                    ev = cmd("Runtime.evaluate", {
                        "expression": "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight,"
                                      "document.body.offsetHeight, document.documentElement.offsetHeight)",
                        "returnByValue": True})
                    return int(((ev.get("result") or {}).get("result") or {}).get("value") or 0)
                try:
                    # Use the real content height (no viewport-padding of short pages),
                    # with a small floor to avoid a degenerate clip if the measurement
                    # comes back ~0, and a cap because GPU surfaces have a max dimension
                    # and a runaway (infinite-scroll) page would produce an unusable image.
                    full_h = min(max(_page_height(), 600), 25000)
                    cmd("Emulation.setDeviceMetricsOverride",
                        {"width": width, "height": full_h, "deviceScaleFactor": 1, "mobile": False})
                    time.sleep(2.0)  # let the now-visible lazy images fire + load
                    full_h = min(max(_page_height(), full_h), 25000)  # may have grown
                except Exception:
                    full_h = 0
            params = {"format": "png", "captureBeyondViewport": True, "fromSurface": True}
            if tight:
                # Opt-in tight capture (used only for self-rendered cards, NOT the
                # screenshot command): clip to the <body> content box so a short card
                # isn't padded out to the viewport height — otherwise the <html>
                # element stretches to the viewport and the image downscales to an
                # unreadable strip. The default path (tight=False) is unchanged.
                ev = cmd("Runtime.evaluate", {
                    "expression": "(()=>{const b=document.body;return [Math.ceil(b.scrollWidth),Math.ceil(b.scrollHeight)];})()",
                    "returnByValue": True,
                })
                dims = ((ev.get("result") or {}).get("result") or {}).get("value")
                if dims and dims[0] and dims[1]:
                    params["clip"] = {"x": 0, "y": 0, "width": dims[0], "height": dims[1], "scale": 1}
            elif full_h:
                params["clip"] = {"x": 0, "y": 0, "width": width, "height": full_h, "scale": 1}
            shot = cmd("Page.captureScreenshot", params)
            data = (shot.get("result") or {}).get("data")
            if not data:
                raise RuntimeError(f"Chrome returned no screenshot: {shot.get('error')}")
            png = base64.b64decode(data)
            if not png:
                raise RuntimeError("Chrome returned an empty screenshot")
            return png
        finally:
            ws.close()
    finally:
        if proc and not reaped_early:
            # start_new_session=True made `proc` the leader of its own process group,
            # so its pid IS the group id. We have NOT reaped it here (reaped_early is
            # only set on the pre-port early-exit path), so the pid is still ours —
            # killing the group by pid is safe from PID-reuse. SIGKILL the whole group:
            # a Chrome crash on a heavy/JS-heavy page (cnn.com et al.) exits the main
            # process while leaving orphaned zygote/gpu/crashpad children behind; the
            # original `if proc.poll() is None` guard skipped cleanup in exactly that
            # case, so orphans piled up until the host ran out of memory/PIDs and every
            # later capture failed opaquely. No graceful SIGTERM — headless, throwaway
            # profile, nothing to flush.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        elif proc:
            # Leader already reaped during launch (Chrome never reached the DevTools
            # port). Its pid may have been recycled, so do NOT killpg it — a failed
            # launch leaves no live render/gpu children to clean up anyway.
            try:
                proc.wait(timeout=1)
            except Exception:
                pass
        shutil.rmtree(tmp_profile, ignore_errors=True)


def _capture_full_page_firefox(firefox: str, url: str, width: int, timeout: int) -> bytes:
    """Fallback: full-page PNG via Firefox's built-in `--screenshot` (subprocess).

    Kept for hosts without Chrome. Firefox captures at the page `load` event with no
    wait flag, so JS-heavy pages may come out blank — Chrome (above) is preferred.
    """
    import os
    import shutil
    import subprocess
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="ffshot_")
    out = os.path.join(tmpdir, "shot.png")
    try:
        with _screenshot_lock:
            # Width only (no height) → Firefox captures the FULL page height at this width.
            proc = subprocess.run(
                [firefox, "--headless", "--new-instance",
                 f"--window-size={width}", "--screenshot", out, url],
                timeout=timeout, capture_output=True,
            )
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            err = (proc.stderr or b"").decode("utf-8", "ignore").strip()
            raise RuntimeError(f"Firefox produced no screenshot. {err[-300:]}")
        with open(out, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _capture_full_page(url: str, width: int = 1280, timeout: int = 60, tight: bool = False) -> bytes:
    """Render `url` and return a full-page PNG (blocking).

    Prefers headless Chrome driven over the DevTools protocol (waits for JS so SPAs
    aren't blank, true full-page via CDP); falls back to Firefox's `--screenshot` if
    Chrome is absent. Raises RuntimeError if neither browser is available or capture fails.

    `tight=True` (Chrome only) clips the capture to the <body> content box instead of
    padding short pages to the viewport height — used for self-rendered cards.
    """
    chrome = _find_chrome()
    if chrome:
        # Serialize captures: each headless Chrome is ~350MB, and user screenshots can
        # coincide with the nitter/social pollers' card renders (same path). Unbounded
        # concurrency spiked memory and crashed launches. Acquire with a timeout so a
        # wedged capture (whose worker thread an asyncio wait_for cancellation can't
        # kill) can't stall the queue forever — callers get a clear "busy" instead.
        if not _screenshot_lock.acquire(timeout=90):
            raise RuntimeError("Screenshot busy — another capture is still running; try again shortly.")
        try:
            return _capture_full_page_chrome(chrome, url, width, timeout, tight=tight)
        except Exception as e:
            logger.warning(f"Chrome screenshot failed ({e}); falling back to Firefox if present")
            if not _find_firefox():
                raise
        finally:
            _screenshot_lock.release()
    firefox = _find_firefox()
    if firefox:
        return _capture_full_page_firefox(firefox, url, width, timeout)
    raise RuntimeError("No headless browser found on the server (install google-chrome-stable)")


def _render_post_card_png(display_name: str, handle: str, text: str,
                          timestamp: str = "", media_data_uri: str = "",
                          avatar_data_uri: str = "", width: int = 600) -> bytes:
    """Render a tweet-style "post card" as HTML and screenshot it via the existing
    headless-browser path (`_capture_full_page`).

    Built entirely from data we control (author/text/media passed in), so it works
    even when the source page serves nothing — e.g. Nitter instances whose RSS still
    works but whose status pages return an empty body, defeating link previews. The
    media (if any) is passed pre-fetched as a data: URI so the render needs no network
    and is deterministic. Returns PNG bytes; raises if no browser is installed.
    """
    import html as _html
    import os
    import tempfile

    # Keep cards readable: clients downscale the attached image to fit their column,
    # so an over-long tweet (premium long-form, quote chains) would shrink the text to
    # an illegible size. Cap the text length, and cap media height in CSS below, so the
    # card stays a sane aspect ratio (~600px wide, bounded height).
    text = text or ""
    if len(text) > 600:
        text = text[:597].rstrip() + "…"

    name = _html.escape(display_name or handle or "")
    handle_esc = _html.escape(handle or "")
    body_text = _html.escape(text).replace("\n", "<br>")
    ts = _html.escape(timestamp or "")
    # Avatar: prefer the real (pre-fetched) profile picture; otherwise a letter
    # initial. The letter fallback needs no emoji font, so it never tofu-boxes.
    # Escape the data: URIs too — their content-type segment originates from a remote
    # server's Content-Type header (passed through by the bot), so an unescaped value
    # like image/png"><img src=file:///… could otherwise break out of the attribute
    # and inject markup into this headless render.
    if avatar_data_uri:
        avatar_block = f'<img class="avatar" src="{_html.escape(avatar_data_uri)}" alt="">'
    else:
        initial = _html.escape(((display_name or handle or "?").strip()[:1] or "?").upper())
        avatar_block = f'<div class="avatar">{initial}</div>'
    media_block = (f'<img class="media" src="{_html.escape(media_data_uri)}" alt="">'
                   if media_data_uri else "")
    ts_block = f'<div class="ts">{ts}</div>' if ts else ""

    doc = f"""<!doctype html><html><head><meta charset="utf-8"><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ width:{width}px; background:#15202b;
    font-family:-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,'Noto Color Emoji',sans-serif; }}
  .card {{ padding:20px 22px; color:#f7f9f9; }}
  .head {{ display:flex; align-items:center; margin-bottom:12px; }}
  .avatar {{ width:48px; height:48px; border-radius:50%; margin-right:12px; flex:0 0 auto; }}
  div.avatar {{ background:#1d9bf0; display:flex; align-items:center;
    justify-content:center; font-size:22px; font-weight:700; color:#fff; }}
  img.avatar {{ object-fit:cover; }}
  .name {{ font-weight:700; font-size:16px; line-height:1.25; }}
  .handle {{ color:#8899a6; font-size:15px; }}
  .text {{ font-size:19px; line-height:1.45; word-wrap:break-word; white-space:pre-wrap; }}
  .media {{ display:block; max-width:100%; max-height:520px; width:auto; height:auto;
    object-fit:contain; border-radius:14px; margin-top:14px; border:1px solid #38444d; }}
  .ts {{ color:#8899a6; font-size:14px; margin-top:14px; }}
</style></head><body><div class="card">
  <div class="head">{avatar_block}
    <div><div class="name">{name}</div><div class="handle">@{handle_esc}</div></div></div>
  <div class="text">{body_text}</div>
  {media_block}
  {ts_block}
</div></body></html>"""

    tmp = tempfile.NamedTemporaryFile(prefix="postcard_", suffix=".html",
                                      delete=False, mode="w", encoding="utf-8")
    try:
        tmp.write(doc)
        tmp.close()
        return _capture_full_page(f"file://{tmp.name}", width=width, tight=True)
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


# Cache for torrent results (per user, per category) - thread-safe with locks
_torrent_cache: dict[int, dict[str, list[TorrentResult]]] = {}
_nyaa_cache: dict[int, list[NyaaResult]] = {}


def _format_bt_list_from_dicts(torrents: list[dict]) -> str:
    """Format torrent list from remote API response (no libtorrent import needed)."""
    if not torrents:
        return "No torrents."

    lines = ["**Torrents:**\n"]
    for i, t in enumerate(torrents, 1):
        bar_len = 10
        progress = t.get("progress", 0)
        filled = int(progress / 100 * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        download_rate = t.get("download_rate", 0)
        upload_rate = t.get("upload_rate", 0)
        down = f"{download_rate / 1024:.1f} KB/s" if download_rate > 0 else "-"
        up = f"{upload_rate / 1024:.1f} KB/s" if upload_rate > 0 else "-"

        size = t.get("size", 0)
        size_mb = size / (1024 * 1024)
        size_str = f"{size_mb:.1f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"

        state = t.get("state", "unknown")
        is_paused = t.get("is_paused", False)

        # Clear status with icon AND text
        if is_paused or state == "paused":
            status = "⏸️ **PAUSED**"
        elif state == "downloading":
            status = "⬇️ **DOWNLOADING**"
        elif state == "seeding":
            status = "⬆️ **SEEDING**"
        elif state == "finished":
            status = "✅ **FINISHED**"
        elif state == "checking":
            status = "🔍 **CHECKING**"
        elif state == "metadata":
            status = "📥 **FETCHING METADATA**"
        else:
            status = f"❓ **{state.upper()}**"

        name = t.get("name", "Unknown")
        seeders = t.get("seeders", 0)
        peers = t.get("peers", 0)

        # Action buttons - clear labels
        if is_paused or state == "paused":
            toggle_btn = f"[▶ Resume](cmd:torrents resume {i})"
        else:
            toggle_btn = f"[⏸ Pause](cmd:torrents pause {i})"
        delete_btn = f"[🗑 Remove](cmd:torrents rm {i})"

        lines.append(
            f"**{i}. {name}**\n"
            f"   Status: {status}\n"
            f"   [{bar}] {progress:.1f}% | {size_str}\n"
            f"   ↓{down} ↑{up} | {seeders}S/{peers}P\n"
            f"   {toggle_btn} | {delete_btn}"
        )

    return "\n".join(lines)


_cache_lock = threading.Lock()


class CommandService:
    COMMANDS = {
        "files": "Search for files in your storage",
        "help": "Show this help message",
        "search": "Web search: search <query>",
        "images": "Image search: images <query>",
        "geni": "Generate image: geni <prompt>",
        "yt": "YouTube search: yt <query>",
        "ytdl": "Download YouTube or X: ytdl <url> (MP3 default), ytdl mp3/video <url>. For video, add clip <start> <end> and/or compress, e.g. ytdl video <url> clip 0:10 0:30 compress",
        "torrents": "Torrent search: torrents <query>",
        "nyaa": "Anime torrents: nyaa <query>",
        "dailynews": "Web news: dailynews <source>",
        "logs": "View system logs",
        "mail": "Email: mail <to> [subject] <body>",
        "translate": "Translate: translate <text> to <lang>",
        "4chan": "4chan browser: 4chan [g|pol|h] - view catalog",
        "compress": "Compress attached image(s) or video(s)",
        "clip": "Clip an attached video: clip <start> <end> (e.g. clip 0:10 0:30)",
        "convert": "Convert image(s) to PDF or a PDF to images",
        "meme": "Add outlined white meme text to an attached image: meme <text>",
        "dildo": "Scatter dildos all over an attached image: dildo",
        "poo": "Scatter poop all over an attached image: poo",
        "cum": "Scatter cum all over an attached image: cum",
        "blood": "Splatter blood all over an attached image: blood",
        "bullethole": "Punch bullet holes all over an attached image: bullethole",
        "fire": "Set an attached image on fire: fire",
        "gay": "Stamp a big red GAY rubber stamp on an attached image: gay",
        "blacked": "Slap the BLACKED logo on an attached image: blacked",
        "kosher": "Stamp a 100% KOSHER certification seal on an attached image: kosher",
        "barked": "Drop a smirking dog and #BARKED on an attached image: barked",
        "alive": "Make an attached photo come alive with 3D parallax motion: alive [subtle(default)|normal|strong]",
        "glow": "Make an attached image stand out — gentle motion, colour pop and a sweeping light: glow",
        "hava": "Turn an attached image into a 6s MP4 set to Hava Nagila: hava",
        "indian": "Turn an attached image into a 6s MP4 set to an Indian song: indian",
        "yakety": "Turn an attached image into a 9s MP4 set to Yakety Sax: yakety",
        "yamete": "Turn an attached image into a 6s MP4 set to the yamete clip: yamete",
        "curb": "Turn an attached image into an MP4 set to the Curb Your Enthusiasm theme: curb",
        "depressing": "Turn an attached image into a 10s MP4 set to a depressing track: depressing",
        "fahh": "Turn an attached image into a short MP4 set to the fahh clip: fahh",
        "helpme": "Turn an attached image into a 5s MP4 set to the helpme clip: helpme",
        "gong": "Turn an attached image into a short MP4 set to the gong clip: gong",
        "fbi": "Turn an attached image into a short MP4 set to the FBI open up clip: fbi",
        "redeem": "Turn an attached image into a short MP4 set to the do not redeem clip: redeem",
        "gigity": "Turn an attached image into a short MP4 set to the giggity clip: gigity",
        "beavis": "Turn an attached image into a short MP4 set to the Beavis laugh: beavis",
        "smell": "Turn an attached image into a short MP4 set to the can you imagine the smell clip: smell",
        "hood": "Turn an attached image into a 10s MP4 set to the hood clip: hood",
        "akbar": "Turn an attached image into a short MP4 set to the akbar clip: akbar",
        "retard": "Turn an attached image into a short MP4 set to the retard-alert clip: retard",
        "whoabuddy": "Turn an attached image into a short MP4 set to the whoa buddy clip: whoabuddy",
        "feliz": "Turn an attached image into a short MP4 set to the feliz clip: feliz",
        "sopranos": "Turn an attached image into an MP4 set to the Sopranos theme clip: sopranos",
        "cheers": "Turn an attached image into an MP4 set to the Cheers theme clip: cheers",
        "munsters": "Turn an attached image into an MP4 set to the Munsters theme clip: munsters",
        "happydays": "Turn an attached image into an MP4 set to the Happy Days theme clip: happydays",
        "dontwanttowait": "Turn an attached image into an MP4 set to the Dawson's Creek theme clip: dontwanttowait",
        "strangerthings": "Turn an attached image into an MP4 set to the Stranger Things theme clip: strangerthings",
        "adamsfamily": "Turn an attached image into an MP4 set to the Addams Family theme clip: adamsfamily",
        "xmen": "Turn an attached image into an MP4 set to the X-Men theme clip: xmen",
        "futurama": "Turn an attached image into an MP4 set to the Futurama theme clip: futurama",
        "charliesangles": "Turn an attached image into an MP4 set to the Charlie's Angels theme clip: charliesangles",
        "differentstroke": "Turn an attached image into an MP4 set to the Diff'rent Strokes theme clip: differentstroke",
        "seinfeld": "Turn an attached image into an MP4 set to the Seinfeld theme clip: seinfeld",
        "onepiece": "Turn an attached image into an MP4 set to the One Piece theme clip: onepiece",
        "overtaken": "Turn an attached image into an MP4 set to the overtaken clip: overtaken",
        "freebird": "Turn an attached image into an MP4 set to the Free Bird solo: freebird",
        "kanye": "Turn an attached image into an MP4 set to the Kanye clip: kanye",
        "darkness": "Turn an attached image into an MP4 set to the darkness clip: darkness",
        "bike": "Turn an attached image into an MP4 set to the bike clip: bike",
        "jobs": "Turn an attached image into an MP4 set to the they-took-our-jobs clip: jobs",
        "ree": "Turn an attached image into an MP4 set to the REEEE clip: ree",
        "liberal": "Turn an attached image into an MP4 set to the liberal clip: liberal",
        "moving": "Turn an attached image into an MP4 set to the moving clip: moving",
        "harlem": "Turn an attached image into an MP4 set to the Harlem Shake clip: harlem",
        "chimp": "Overlay the animated chimp gif on the lower third of an attached image: chimp",
        "consider": "Overlay the 'consider the following' cutout on an attached image: consider",
        "clay": "Overlay the background-removed Clay Davis 'Shiiiit' clip on an image: clay",
        "wasteland": "Turn an attached image into an MP4 set to the Teenage Wasteland intro: wasteland",
        "mixalot": "Turn an attached image into an MP4 set to the Baby Got Back clip: mixalot",
        "thug": "Turn an attached image into an MP4 set to the THUG LIFE clip: thug",
        "feltedtables": "Turn an attached image into an MP4 set to the felted-tables clip: feltedtables",
        "prayer": "Turn an attached image into an MP4 set to the prayer clip: prayer",
        "node": "Remote node mgmt: node <name> <cmd> | node all <cmd> | node agent <name> <goal> | node agent all <goal> | node list | node jobs | node log <id> | node kill <id>",
        "budget": "Show your budget summary (income, unpaid bills, remaining)",
        "bills": "List your bills: bills (unpaid) | bills all | bills paid",
        "pay": "Pay a bill by name: pay <bill name>",
        "addbill": "Add a bill: addbill <name> <amount> [income]",
        "screenshot": "Full-page screenshot of a website: screenshot <url>",
        "poll": "Create a poll (Matrix): poll <question> | <option 1> | <option 2> — 2 to 20 options, separated by |",
    }
    # Command aliases (alias -> canonical command)
    COMMAND_ALIASES = {
        "torrent": "torrents",
        "bt": "torrents",
        "yt-dlp": "ytdl",
        "ytdlp": "ytdl",
        "youtube": "yt",
        "nodes": "node",
        "finance": "budget",
        "shot": "screenshot",
        "ss": "screenshot",
    }

    # Effects that accept a trailing motion arg (`zoom` Ken Burns pan-out or
    # `shake` camera shake of the output).
    MOTION_EFFECTS = {
        "meme", "dildo", "poo", "cum", "blood", "bullethole", "fire", "gay",
        "blacked", "kosher", "barked", "hava", "indian", "yakety", "yamete",
        "curb", "depressing", "fahh", "helpme", "gong", "fbi", "redeem",
        "gigity", "beavis", "smell", "hood", "akbar", "retard", "whoabuddy",
        "sopranos", "cheers", "munsters", "happydays", "dontwanttowait", "strangerthings", "adamsfamily", "xmen", "futurama", "charliesangles", "differentstroke", "seinfeld", "onepiece", "overtaken", "freebird", "kanye", "darkness", "bike", "jobs", "ree", "liberal", "moving",
        "harlem", "chimp", "consider", "clay", "wasteland", "mixalot", "thug",
        "feltedtables", "glow", "prayer", "alive", "feliz",
    }

    # Trailing motion tokens an effect accepts (zoom pan-out, full camera shake,
    # a gentler `medshake`, `beginshake` which shakes hard then settles, and
    # `trippy` psychedelic hue-cycle).
    MOTION_ARGS = ("zoom", "shake", "medshake", "beginshake", "trippy", "pulse", "glow", "alive")

    # Effects whose output is ALREADY animated (e.g. the chimp gif overlay). The
    # zoom/shake motions freeze-and-pan a single still frame (they extract frame 1 of
    # the effect video), which would kill a real animation — so skip them here. A
    # `meme` caption is still fine (ffmpeg drawtext preserves the motion).
    ANIMATED_EFFECTS = {"chimp", "clay"}

    # Natural language phrases that map directly to commands with arguments
    # Format: "phrase" -> ("command", "argument")
    PHRASE_COMMANDS = {}

    def __init__(self, db: Session, user: Optional["User"] = None):
        self.db = db
        self.user = user
        self.search_service = SearchService(db)
        self.chat_service = ChatService(db, user=user)

    def parse_command(self, message: str) -> Tuple[Optional[str], str]:
        """Parse message for commands, return (command, argument)"""
        # Remove emojis and other unicode symbols that might interfere with matching
        import re
        # Remove common emojis and symbols (✏️, 🔄, etc.) but keep the text
        cleaned_message = re.sub(r'[✏️🔄📅📆🗓️➕➖✕×]', '', message)
        lower = cleaned_message.lower().strip()

        # Check natural language phrases first (exact match)
        if lower in self.PHRASE_COMMANDS:
            cmd, arg = self.PHRASE_COMMANDS[lower]
            return cmd, arg

        # Video downloads
        for prefix in ["download this video ", "download video "]:
            if lower.startswith(prefix):
                url = message[len(prefix):].strip()
                return "ytdl", f"video {url}"
        
        # Generic download with YouTube URL
        if lower.startswith("download ") and ("youtube" in lower or "youtu.be" in lower):
            url = message[9:].strip()
            return "ytdl", url

        # Check canonical commands
        for cmd in self.COMMANDS:
            if lower.startswith(f"{cmd} "):
                return cmd, message[len(cmd) + 1 :].strip()
            if lower == cmd:
                return cmd, ""

        # Check aliases
        for alias, canonical in self.COMMAND_ALIASES.items():
            if lower.startswith(f"{alias} "):
                return canonical, message[len(alias) + 1 :].strip()
            if lower == alias:
                return canonical, ""

        return None, message

    async def execute_command(
        self,
        command: str,
        arg: str,
        last_prompt: Optional[str] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        attachments: Optional[list] = None,
        node_notify: Optional[Callable] = None,
    ) -> dict:
        """Execute a command, then shrink any oversized video outputs via the shared
        `compress` feature before returning (so effects don't hand back 10 MB clips)."""
        result = await self._execute_command_inner(
            command, arg, last_prompt, stop_check, attachments, node_notify,
        )
        # Only auto-compress EFFECT outputs — not the `compress`/`clip`/`convert`/`ytdl`
        # media tools, where the user controls quality (and `compress` already ran).
        if (command in self.MOTION_EFFECTS and isinstance(result, dict)
                and result.get("type") == "files" and result.get("files")):
            import asyncio
            from app.services import media_service
            result["files"] = await asyncio.to_thread(
                media_service.compress_output_videos, result["files"],
            )
        return result

    async def _execute_command_inner(
        self,
        command: str,
        arg: str,
        last_prompt: Optional[str] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        attachments: Optional[list] = None,
        node_notify: Optional[Callable] = None,
    ) -> dict:
        """Execute a command and return the result.

        Args:
            command: The command name
            arg: Command arguments
            last_prompt: Last image generation prompt (for regeneration)
            stop_check: Callable to check if execution should stop
            attachments: List of (filename, data_bytes, content_type) tuples for mail
        """
        # Resolve aliases (e.g. "shot" → "screenshot") centrally so callers that match
        # commands literally (Telegram) accept them just like the web UI's parse_command.
        command = self.COMMAND_ALIASES.get(command, command)

        # Trailing subcommands on an effect, applied to its output in order:
        #   <effect> [zoom|shake] [meme <text>]
        # e.g. `dildo zoom meme top text`. `meme <text>` consumes the rest as the
        # caption; zoom/shake is a single token before it. Re-enter with all the
        # trailing parts stripped so the base effect renders untouched, then
        # transform the files (motion first, caption last so it sits on top).
        if command in self.MOTION_EFFECTS and arg:
            _toks = arg.split()
            _low = [t.lower() for t in _toks]
            _meme_text = None
            # `meme` only acts as a trailing subcommand for OTHER effects — not for
            # the meme effect itself (its whole arg is the caption) nor thug (which
            # bakes its own "THUG LIFE" text).
            if command not in ("meme", "thug") and "meme" in _low:
                _i = _low.index("meme")
                _meme_text = " ".join(_toks[_i + 1:]).strip()
                _toks, _low = _toks[:_i], _low[:_i]
            # Trailing motion cluster: at most one geometry motion (zoom/shake/
            # medshake/beginshake/pulse) plus the `trippy` colour pass, in either
            # order, at the very END of the arg. Only TRAILING tokens are consumed
            # (cap 2), so a caption word like "trippy" mid-text — e.g. `meme so
            # trippy bro` — is never mistaken for a motion. Geometry motions don't
            # stack (they'd fight over the crop); trippy layers on top.
            _motion = None
            _trippy = False
            for _ in range(2):
                if not _low or _low[-1] not in self.MOTION_ARGS:
                    break
                _t = _low.pop()
                _toks.pop()
                if _t == "trippy":
                    _trippy = True
                elif _motion is None:
                    _motion = _t
            if _motion or _trippy or _meme_text:
                import asyncio
                from app.services import effects_service
                inner = await self._execute_command_inner(
                    command, " ".join(_toks), last_prompt, stop_check, attachments, node_notify,
                )
                if isinstance(inner, dict) and inner.get("type") == "files" and inner.get("files"):
                    files = inner["files"]
                    # A geometry motion would freeze an already-animated effect (it
                    # pans a single still frame) — skip it for those, keep the rest.
                    if _motion and command not in self.ANIMATED_EFFECTS:
                        _apply = {
                            "zoom": effects_service.apply_zoom,
                            "shake": effects_service.apply_shake,
                            "medshake": effects_service.apply_medshake,
                            "beginshake": effects_service.apply_beginshake,
                            "pulse": effects_service.apply_pulse,
                            "glow": effects_service.apply_glow,
                            "alive": effects_service.apply_alive,
                        }.get(_motion, effects_service.apply_zoom)
                        files = await asyncio.to_thread(_apply, files)
                    # trippy recolours frame-by-frame (keeps motion) → safe to layer
                    # on top of a geometry motion, and even on animated effects.
                    if _trippy:
                        files = await asyncio.to_thread(effects_service.apply_trippy, files)
                    if _meme_text:
                        files = await asyncio.to_thread(effects_service.apply_meme_text, files, _meme_text)
                    inner["files"] = files
                return inner

        if command == "help":
            return await self._help_command()
        elif command == "search":
            return await self._search_command(arg)
        elif command == "images":
            return await self._images_command(arg)
        elif command == "files":
            return await self._files_command(arg)
        elif command == "geni":
            return await self._geni_command(arg, stop_check)
        elif command == "yt":
            return await self._youtube_command(arg)
        elif command == "ytdl":
            return await self._youtube_download_command(arg)
        elif command == "torrents":
            return await self._torrents_command(arg)
        elif command == "nyaa":
            return await self._nyaa_command(arg)
        elif command == "news":
            return await self._news_command(arg)
        elif command == "dailynews":
            return await self._dailynews_command(arg)
        elif command == "logs":
            return await self._logs_command(arg, notify=node_notify)
        elif command == "mail":
            return await self._mail_command(arg, attachments=attachments)
        elif command == "todo":
            return await self._todo_command(arg)
        elif command == "translate":
            return await self._translate_command(arg, attachments=attachments)
        elif command == "4chan":
            return await self._4chan_command(arg)
        elif command == "compress":
            return await self._compress_command(attachments)
        elif command == "clip":
            return await self._clip_command(arg, attachments)
        elif command == "convert":
            return await self._convert_command(arg, attachments)
        elif command == "meme":
            return await self._meme_command(arg, attachments)
        elif command == "dildo":
            return await self._dildo_command(attachments)
        elif command == "poo":
            return await self._poo_command(attachments)
        elif command == "cum":
            return await self._cum_command(attachments)
        elif command == "blood":
            return await self._blood_command(attachments)
        elif command == "bullethole":
            return await self._bullethole_command(attachments)
        elif command == "fire":
            return await self._fire_command(attachments)
        elif command == "alive":
            return await self._alive_command(arg, attachments)
        elif command == "glow":
            return await self._glow_command(arg, attachments)
        elif command == "prayer":
            return await self._prayer_command(attachments)
        elif command == "gay":
            return await self._gay_command(attachments)
        elif command == "blacked":
            return await self._blacked_command(attachments)
        elif command == "kosher":
            return await self._kosher_command(attachments)
        elif command == "barked":
            return await self._barked_command(attachments)
        elif command == "hava":
            return await self._hava_command(attachments)
        elif command == "indian":
            return await self._indian_command(attachments)
        elif command == "yakety":
            return await self._yakety_command(attachments)
        elif command == "yamete":
            return await self._yamete_command(attachments)
        elif command == "curb":
            return await self._curb_command(attachments)
        elif command == "depressing":
            return await self._depressing_command(attachments)
        elif command == "fahh":
            return await self._fahh_command(attachments)
        elif command == "helpme":
            return await self._helpme_command(attachments)
        elif command == "gong":
            return await self._gong_command(attachments)
        elif command == "fbi":
            return await self._fbi_command(attachments)
        elif command == "redeem":
            return await self._redeem_command(attachments)
        elif command == "gigity":
            return await self._gigity_command(attachments)
        elif command == "beavis":
            return await self._beavis_command(attachments)
        elif command == "smell":
            return await self._smell_command(attachments)
        elif command == "hood":
            return await self._hood_command(attachments)
        elif command == "akbar":
            return await self._akbar_command(attachments)
        elif command == "retard":
            return await self._retard_command(attachments)
        elif command == "whoabuddy":
            return await self._whoabuddy_command(attachments)
        elif command == "feliz":
            return await self._feliz_command(attachments)
        elif command == "sopranos":
            return await self._sopranos_command(attachments)
        elif command == "cheers":
            return await self._cheers_command(attachments)
        elif command == "munsters":
            return await self._munsters_command(attachments)
        elif command == "happydays":
            return await self._happydays_command(attachments)
        elif command == "dontwanttowait":
            return await self._dontwanttowait_command(attachments)
        elif command == "strangerthings":
            return await self._strangerthings_command(attachments)
        elif command == "adamsfamily":
            return await self._adamsfamily_command(attachments)
        elif command == "xmen":
            return await self._xmen_command(attachments)
        elif command == "futurama":
            return await self._futurama_command(attachments)
        elif command == "charliesangles":
            return await self._charliesangles_command(attachments)
        elif command == "differentstroke":
            return await self._differentstroke_command(attachments)
        elif command == "seinfeld":
            return await self._seinfeld_command(attachments)
        elif command == "onepiece":
            return await self._onepiece_command(attachments)
        elif command == "overtaken":
            return await self._overtaken_command(attachments)
        elif command == "freebird":
            return await self._freebird_command(attachments)
        elif command == "kanye":
            return await self._kanye_command(attachments)
        elif command == "darkness":
            return await self._darkness_command(attachments)
        elif command == "bike":
            return await self._bike_command(attachments)
        elif command == "jobs":
            return await self._jobs_command(attachments)
        elif command == "ree":
            return await self._ree_command(attachments)
        elif command == "liberal":
            return await self._liberal_command(attachments)
        elif command == "moving":
            return await self._moving_command(attachments)
        elif command == "harlem":
            return await self._harlem_command(attachments)
        elif command == "chimp":
            return await self._chimp_command(attachments)
        elif command == "consider":
            return await self._consider_command(attachments)
        elif command == "clay":
            return await self._clay_command(attachments)
        elif command == "wasteland":
            return await self._wasteland_command(attachments)
        elif command == "mixalot":
            return await self._mixalot_command(attachments)
        elif command == "thug":
            return await self._thug_command(attachments)
        elif command == "feltedtables":
            return await self._feltedtables_command(attachments)
        elif command == "node":
            return await self._node_command(arg, notify=node_notify)
        elif command == "budget":
            return await self._budget_command()
        elif command == "bills":
            return await self._bills_command(arg)
        elif command == "pay":
            return await self._pay_command(arg)
        elif command == "addbill":
            return await self._addbill_command(arg)
        elif command == "screenshot":
            return await self._screenshot_command(arg)
        else:
            return {"type": "text", "content": f"Unknown command: {command}"}

    async def _help_command(self) -> dict:
        """Show available commands and plugins"""
        help_text = "## Available Commands\n\n"

        # Built-in commands
        for cmd, desc in self.COMMANDS.items():
            help_text += f"**{cmd}** - {desc}\n"

        # Motion/colour modifiers — appended to any effect, not standalone commands.
        help_text += (
            "\n**Effect modifiers** (append to any effect): "
            "`zoom` `shake` `medshake` `beginshake` `pulse` motion, and/or `trippy` "
            "colours — e.g. `dildo zoom trippy`, `whoabuddy pulse trippy`.\n"
        )

        return {"type": "text", "content": help_text}

    # --- Finance (Budget Manager) commands ---------------------------------

    async def _budget_command(self) -> dict:
        from app.services import finance_service
        try:
            base, key = finance_service.get_config(self.db, self.user)
            summary = await finance_service.get_summary(base, key)
        except finance_service.FinanceError as e:
            return {"type": "text", "content": f"💰 {e}"}
        return {"type": "text", "content": finance_service.format_summary(summary)}

    async def _bills_command(self, arg: str) -> dict:
        from app.services import finance_service
        arg = (arg or "").strip().lower()
        status = None if arg == "all" else (arg if arg in ("paid", "unpaid") else "unpaid")
        header = {"paid": "Paid bills", "unpaid": "Unpaid bills", None: "All bills"}.get(status, "Unpaid bills")
        try:
            base, key = finance_service.get_config(self.db, self.user)
            bills = await finance_service.get_bills(base, key, status=status)
        except finance_service.FinanceError as e:
            return {"type": "text", "content": f"💰 {e}"}
        return {"type": "text", "content": finance_service.format_bills(bills, header=header)}

    async def _pay_command(self, arg: str) -> dict:
        from app.services import finance_service
        name = (arg or "").strip()
        if not name:
            return {"type": "text", "content": "Usage: pay <bill name>"}
        try:
            base, key = finance_service.get_config(self.db, self.user)
            result = await finance_service.pay_bill(base, key, name)
        except finance_service.FinanceError as e:
            return {"type": "text", "content": f"💰 {e}"}
        return {"type": "text", "content": f"✅ {result.get('message', 'Paid.')}"}

    async def _addbill_command(self, arg: str) -> dict:
        from app.services import finance_service
        try:
            name, amount, is_income = finance_service.parse_add_bill_arg(arg or "")
            base, key = finance_service.get_config(self.db, self.user)
            result = await finance_service.add_bill(base, key, name, amount, is_income=is_income)
        except finance_service.FinanceError as e:
            return {"type": "text", "content": f"💰 {e}"}
        return {"type": "text", "content": f"✅ {result.get('message', 'Added.')}"}

    async def _search_command(self, query: str) -> dict:
        if not query:
            return {"type": "text", "content": "Please provide a search query. Example: `search latest AI news`"}

        clean_query, categories, time_range = self.search_service.detect_search_intent(query)
        results = await self.search_service.web_search(
            clean_query, limit=5, categories=categories, time_range=time_range
        )
        # Fall back to a plain general search if a category search came up empty.
        if not results and (categories or time_range):
            results = await self.search_service.web_search(clean_query, limit=5)
        if not results:
            return {"type": "text", "content": f"No results found for: {query}"}

        scope = f" ({categories})" if categories else ""
        # Format results for AI summarization
        context = f"Search results for '{clean_query}'{scope}:\n\n"
        for i, r in enumerate(results, 1):
            context += f"{i}. **{r['title']}**\n{r['url']}\n{r['content']}\n\n"

        # Get AI summary
        messages = [
            {
                "role": "system",
                "content": "You are a helpful assistant. Summarize the search results concisely and highlight key information.",
            },
            {"role": "user", "content": context},
        ]
        summary = await self.chat_service.chat(messages)

        return {"type": "search", "content": summary, "results": results}

    async def _images_command(self, query: str) -> dict:
        if not query:
            return {"type": "text", "content": "Please provide a search query. Example: `images cute cats`"}

        results = await self.search_service.image_search(query, limit=10)
        if not results:
            return {"type": "text", "content": f"No images found for: {query}"}
        results = results[:10]
        # For Android: limit to 5 items and send both thumb_id and img_src so payload fits and direct fallback works.
        # (10 items + img_src truncates; 10 items without img_src = proxy fails = 0 images. 5 + img_src = 5 images.)
        images_payload = []
        for r in results[:5]:
            thumb_url = (r.get("img_src") or "").strip()
            if not thumb_url:
                continue
            page_url = (r.get("url") or thumb_url).strip()
            title = (r.get("title") or "Image")[:200]
            try:
                thumb_id = proxy_image_register(thumb_url, self.db)
                images_payload.append({"title": title, "url": page_url, "thumb_id": thumb_id, "img_src": thumb_url})
            except Exception:
                images_payload.append({"title": title, "url": page_url, "img_src": thumb_url})
        return {"type": "images", "content": f"Found {len(images_payload)} images for: {query}", "images": images_payload}

    async def _files_command(self, query: str) -> dict:
        """Search for files in user's storage."""
        if not query:
            return {"type": "text", "content": "Please provide a search query. Example: `files image` or `files document.pdf`"}
        
        return await self._search_files_internal(query)
    
    async def _search_files_internal(self, query: str) -> dict:
        """Internal file search function - handles storage proxy correctly."""
        from pathlib import Path
        from app.services.storage_service import get_storage_service
        from app.models import Setting
        import httpx

        # Check if using remote storage
        storage_setting = self.db.query(Setting).filter(Setting.key == "storage_server_url").first()
        if storage_setting and storage_setting.value and storage_setting.value.startswith(('http://', 'https://')):
            # Use remote storage API with async httpx (same as files router)
            url = storage_setting.value.strip()
            try:
                headers = {
                    "X-Posterchanai-Load-Balanced": "true"
                }
                
                # Try both endpoints (same as files router)
                search_urls = [
                    f"{url.rstrip('/')}/api/files/search",
                    f"{url.rstrip('/')}/api/storage/search"
                ]
                
                response = None
                async with httpx.AsyncClient(timeout=60.0) as client:
                    for search_url in search_urls:
                        try:
                            response = await client.get(
                                search_url,
                                params={"query": query, "username": self.user.username},
                                headers=headers
                            )
                            if response.status_code == 200:
                                break
                        except Exception as e:
                            logger.debug(f"Tried {search_url}, got error: {e}")
                            continue
                    
                    if response and response.status_code == 200:
                        data = response.json()
                        results = data.get('results', [])
                        return {
                            "type": "files",
                            "content": f"Found {len(results)} file(s) matching '{query}'",
                            "files": results[:50],  # Limit to 50 results
                            "query": query
                        }
                    else:
                        logger.warning(f"Storage server search failed, falling back to local search")
            except Exception as e:
                logger.warning(f"Error searching remote files: {e}, falling back to local search")

        # Local storage search (or fallback if remote search failed)
        storage = get_storage_service(self.db)
        user_path = storage.get_user_path(self.user.username)

        results = []
        query_lower = query.lower()

        try:
            # Recursively search through user's files
            for item in user_path.rglob('*'):
                try:
                    if item.is_dir():
                        continue

                    filename = item.name.lower()
                    relative_path = str(item.relative_to(user_path)).lower()

                    if query_lower in filename or query_lower in relative_path:
                        stat = item.stat()
                        results.append({
                            "name": item.name,
                            "path": str(item.relative_to(user_path)),
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                        })
                except Exception as e:
                    logger.warning(f"Error processing file {item}: {e}")
                    continue

            # Sort by modified time (newest first)
            results.sort(key=lambda x: x.get('modified', 0), reverse=True)

            return {
                "type": "files",
                "content": f"Found {len(results)} file(s) matching '{query}'",
                "files": results[:50],  # Limit to 50 results
                "query": query
            }
        except Exception as e:
            logger.error(f"Error searching files locally: {e}", exc_info=True)
            return {"type": "text", "content": f"Error searching files: {str(e)}"}

    async def _geni_command(self, prompt: str, stop_check: Optional[callable] = None) -> dict:
        if not prompt:
            return {
                "type": "text",
                "content": "Please provide a prompt. Example: `geni a beautiful sunset over mountains`",
            }

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

        # Generate image with load balancing support
        # Lock is handled inside image_factory for local generation only
        # Remote requests (load balanced or custom user endpoint) run in parallel
        try:
            logger.info(f"Generating image with prompt: {prompt[:100]}...")
            image_data = await generate_image_for_user(
                db=self.db,
                user=self.user,
                prompt=prompt,
            )
        except Exception as e:
            logger.error(f"Image generation exception: {e}", exc_info=True)
            return {"type": "text", "content": f"Image generation error: {str(e)}\n\nCheck logs for details."}

        if stop_check and stop_check():
            return {"type": "text", "content": "Generation cancelled."}

        if not image_data:
            # Get backend info for better error message
            from app.services.image_factory import get_image_backend_info
            backend_info = get_image_backend_info(self.db)
            backend_type = backend_info.get("backend", "unknown")
            
            error_msg = "## ❌ Image Generation Failed\n\n"
            
            if backend_type == "comfyui":
                comfyui_url = backend_info.get("comfyui_url", "")
                if not comfyui_url:
                    error_msg += "**ComfyUI URL not configured.**\n\n"
                    error_msg += "Go to Admin → Services → Image Generation and set the ComfyUI URL.\n"
                else:
                    error_msg += f"**ComfyUI backend configured** (`{comfyui_url}`)\n\n"
                    error_msg += "Possible issues:\n"
                    error_msg += "- ComfyUI server is not running\n"
                    error_msg += "- ComfyUI server is not accessible at the configured URL\n"
                    error_msg += "- Check server logs for errors\n"
            elif backend_type == "native":
                error_msg += "**Native diffusers backend**\n\n"
                error_msg += "Possible issues:\n"
                error_msg += "- Model not loaded (check VRAM availability)\n"
                error_msg += "- Generation failed (check logs)\n"
                error_msg += "- GPU/XPU not available\n"
            else:
                error_msg += "**Image backend not properly configured.**\n\n"
                error_msg += "Go to Admin → Services → Image Generation to configure.\n"
            
            error_msg += "\n**Prompt:** " + prompt
            logger.warning(f"Image generation returned None for prompt: {prompt[:100]}...")
            
            return {"type": "text", "content": error_msg}

        # Don't save automatically - just display the image with a save button
        return {
            "type": "generated_image",
            "content": f"Generated image for: {prompt}",
            "image": image_data,
            "prompt": prompt,
        }

    async def _screenshot_command(self, arg: str) -> dict:
        """Capture a full-page screenshot of a website via headless Chrome.

        Returns the shared `generated_image` shape so every channel renders it the
        same way: inline in the web UI (with a save button), a photo/document on
        Telegram, and an uploaded image in Matrix.
        """
        import asyncio
        import base64

        url = arg.strip().split()[0] if arg.strip() else ""
        if not url:
            return {"type": "text", "content": "Usage: `screenshot <url>` — e.g. `screenshot example.com`"}
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url

        # SSRF guard: refuse internal/private targets before handing the URL to the
        # browser. Resolved off the event loop since it does a blocking DNS lookup.
        # `screenshot_allowed_hosts` (admin setting) lets the operator's own domains
        # that resolve to a LAN IP via split-horizon DNS (e.g. poster.place) through.
        from app.models import Setting
        allow_setting = self.db.query(Setting).filter(Setting.key == "screenshot_allowed_hosts").first()
        allowed_hosts = re.split(r"[\s,]+", allow_setting.value.strip()) if (allow_setting and allow_setting.value) else []
        if not await asyncio.to_thread(_url_is_safe_to_fetch, url, allowed_hosts):
            return {"type": "text", "content": f"🚫 Refusing to capture {url} — it resolves to a private or internal address."}

        import subprocess
        try:
            # Backstop above the browser's own timeout (+ settle) so the handler
            # always replies even if the page render stalls.
            png = await asyncio.wait_for(asyncio.to_thread(_capture_full_page, url), timeout=100)
        except (asyncio.TimeoutError, subprocess.TimeoutExpired):
            return {"type": "text", "content": f"📸 Timed out capturing {url} — the page took too long to render."}
        except Exception as e:
            logger.error(f"[screenshot] {url}: {e}", exc_info=True)
            msg = str(e)
            if "no headless browser" in msg.lower():
                return {"type": "text", "content": f"📸 Couldn't capture {url}: no headless browser installed on the server (install google-chrome-stable)."}
            first_line = next((ln for ln in msg.splitlines() if ln.strip()), "unknown error")
            return {"type": "text", "content": f"📸 Couldn't capture {url}: {first_line}"}

        return {
            "type": "generated_image",
            "content": f"📸 {url}",
            "image": base64.b64encode(png).decode("ascii"),
            "prompt": url,
            # Telegram compresses photos (tiny/unreadable for tall pages) — deliver as a
            # full-resolution document instead. Ignored by the web UI / Matrix renderers.
            "prefer_document": True,
        }

    def _format_size(self, size_bytes: int) -> str:
        """Format bytes to human readable size"""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} PB"

    async def _youtube_command(self, arg: str) -> dict:
        """Summarize a YouTube video transcript"""
        if not arg:
            return {
                "type": "text",
                "content": """## YouTube Commands

**Summarize a video:**
`yt <url>` - Get AI summary of video transcript

**Download:**
- `ytdl <url>` - Download as MP3 to Music (default)
- `ytdl mp3 <url>` - Download as MP3 to Music
- `ytdl video <url>` - Download as video (MP4) to YouTube Videos

Example: `yt https://youtube.com/watch?v=...`""",
            }

        # Extract URL
        urls = extract_youtube_urls(arg)
        if not urls:
            return {"type": "text", "content": "Could not find a valid YouTube URL."}

        target_url = urls[0]
        success, result = await summarize_youtube(target_url, self.chat_service)
        return {"type": "text", "content": result}

    async def _youtube_download_command(self, arg: str) -> dict:
        """Download a YouTube video (audio or video) to storage"""

        if not arg:
            return {
                "type": "text",
                "content": """## YouTube / X (Twitter) Download

**Usage:**
- `ytdl <url>` - Download as MP3 to Music (default)
- `ytdl mp3 <url>` - Download as MP3 to Music
- `ytdl video <url>` - Download as video (MP4) to folder

**Supported:** YouTube, X.com (Twitter) links.

**Examples:**
- `ytdl https://youtube.com/watch?v=...` - Download as MP3
- `ytdl video https://x.com/i/status/123...` - Download X video
- `ytdl https://x.com/user/status/123...` - Download as MP3

Files are saved to your Storage.""",
            }

        # Check if yt-dlp is available
        if not check_ytdlp_available():
            return {"type": "text", "content": "❌ yt-dlp not installed. Install with: `pip install yt-dlp`"}

        # Parse: "ytdl video <url>" | "ytdl mp3 <url>" | "ytdl <url>" (default = MP3)
        parts = arg.strip().split(maxsplit=1)
        first = parts[0].lower()
        if first == "video":
            url_arg = parts[1] if len(parts) > 1 else ""
            if not url_arg:
                return {"type": "text", "content": "Usage: `ytdl video <url>`\n\nExample: `ytdl video https://youtube.com/watch?v=...`"}
            as_mp3 = False
        elif first == "mp3":
            url_arg = parts[1] if len(parts) > 1 else ""
            if not url_arg:
                return {"type": "text", "content": "Usage: `ytdl mp3 <url>`\n\nExample: `ytdl mp3 https://youtube.com/watch?v=...`"}
            as_mp3 = True
        else:
            url_arg = arg
            as_mp3 = True  # default: MP3

        urls = extract_download_urls(url_arg)
        if not urls:
            return {"type": "text", "content": "Could not find a valid YouTube or X (Twitter) URL. Example: `ytdl https://x.com/i/status/123` or `ytdl https://youtube.com/watch?v=...`"}

        target_url = urls[0]
        if as_mp3:
            logger.info(f"[ytdl] Command: mp3 url={target_url!r} user_id={self.user.id}")
            result = await download_mp3_and_save_to_storage(
                url=target_url,
                user_id=self.user.id,
                db=self.db,
                subfolder="Music",
            )
        else:
            logger.info(f"[ytdl] Command: video url={target_url!r} user_id={self.user.id}")
            result = await download_video_and_save_to_storage(
                url=target_url,
                user_id=self.user.id,
                db=self.db,
                subfolder="YouTube Videos",
            )

        return {"type": "text", "content": format_download_result(result)}

    def _get_remote_bt_url(self):
        """Get remote torrent server URL if configured."""
        from app.models import Setting

        server_url = self.db.query(Setting).filter(Setting.key == "bt_server_url").first()
        return server_url.value if server_url and server_url.value else None

    async def _remote_bt_request(self, endpoint: str, method: str = "GET", json_body: dict = None):
        """Make request to remote torrent server."""
        import httpx

        from app.models import Setting

        server_url = self._get_remote_bt_url()
        if not server_url:
            return None

        # Server-to-server requests don't need authentication
        url = f"{server_url.rstrip('/')}/api/torrent{endpoint}"
        headers = {
            "X-Posterchanai-Load-Balanced": "true"
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info(f"[TORRENT] TUI request to {url} (load-balanced)")
                if method == "GET":
                    response = await client.get(url, headers=headers)
                else:
                    response = await client.post(url, headers=headers, json=json_body)

                logger.info(f"[TORRENT] Remote response: {response.status_code}")

                if response.status_code == 200:
                    try:
                        return response.json()
                    except Exception as e:
                        logger.error(f"[TORRENT] Failed to parse JSON: {e}, body: {response.text[:500]}")
                        return {"error": "Remote server returned invalid response"}
                else:
                    # Try to get error detail from JSON, fall back to text
                    try:
                        error = response.json().get("detail", "Remote server error")
                    except Exception:
                        error = response.text[:200] if response.text else f"HTTP {response.status_code}"
                    logger.error(f"[TORRENT] Remote error: {response.status_code} - {error}")
                    return {"error": error}
        except httpx.RequestError as e:
            logger.error(f"Failed to connect to remote torrent server: {e}")
            return {"error": f"Cannot reach remote torrent server: {e}"}

    def _get_bt_service(self):
        """Get built-in torrent service if enabled, or None. Returns (service, error_msg)."""
        from app.models import Setting

        # Check for remote server first
        if self._get_remote_bt_url():
            return "remote", None  # Special marker for remote server

        bt_enabled = self.db.query(Setting).filter(Setting.key == "bt_enabled").first()
        if not bt_enabled or bt_enabled.value.lower() != "true":
            return None, "Built-in torrent client is disabled. Enable it in Admin Settings."

        def get_setting(key: str, default: str = "") -> str:
            s = self.db.query(Setting).filter(Setting.key == key).first()
            return s.value if s and s.value else default

        proxy_host = get_setting("bt_proxy_host")
        if not proxy_host:
            return None, "HTTP Proxy Host not configured. Set it in Admin Settings (required for torrenting)."

        try:
            from app.services.libtorrent_service import LibtorrentService

            service = LibtorrentService.get_instance(
                download_dir=get_setting("bt_download_dir", "/var/lib/posterchanai/torrents"),
                proxy_host=proxy_host,
                proxy_port=int(get_setting("bt_proxy_port", "8118")),
                listen_port=int(get_setting("bt_listen_port", "6881")),
            )
            return service, None
        except ImportError as e:
            return None, f"libtorrent not installed: {e}. Run: pip install libtorrent"
        except Exception as e:
            return None, f"Failed to start torrent service: {e}"

    async def _torrents_command(self, arg: str) -> dict:
        """Browse torrents and manage downloads."""
        global _torrent_cache

        # Import formatting functions - use local fallback if libtorrent not installed
        try:
            from app.services.libtorrent_service import format_torrent_list, format_torrent_list_from_dicts
        except Exception as e:
            logger.warning(f"Could not import libtorrent formatting: {e}")
            format_torrent_list = lambda torrents: _format_bt_list_from_dicts(
                [
                    {
                        "name": t.name,
                        "size": t.size,
                        "progress": t.progress,
                        "download_rate": t.download_rate,
                        "upload_rate": t.upload_rate,
                        "state": t.state,
                        "seeders": t.seeders,
                        "peers": t.peers,
                        "is_paused": getattr(t, "is_paused", False),
                    }
                    for t in torrents
                ]
            )
            format_torrent_list_from_dicts = _format_bt_list_from_dicts

        parts = arg.strip().split()
        subcommand = parts[0].lower() if parts else ""
        categories = ("movies", "tv", "music", "anime", "search")

        # Get built-in service (None if disabled or not configured)
        bt_service, bt_error = self._get_bt_service()

        # Client management subcommands - require built-in client or remote server
        if subcommand in ("list", "ls"):
            if not bt_service:
                return {"type": "text", "content": bt_error}
            if bt_service == "remote":
                result = await self._remote_bt_request("/list")
                if result and "error" in result:
                    return {"type": "text", "content": result["error"]}
                if result and "torrents" in result:
                    return {"type": "text", "content": _format_bt_list_from_dicts(result["torrents"])}
                return {"type": "text", "content": "No response from remote server"}
            torrents = bt_service.list_torrents()
            return {"type": "text", "content": format_torrent_list(torrents)}

        elif subcommand == "add" and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            magnet = parts[1]
            if not magnet.startswith("magnet:"):
                return {"type": "text", "content": "Please provide a magnet link starting with `magnet:`"}
            if bt_service == "remote":
                result = await self._remote_bt_request("/add", method="POST", json_body={"magnet": magnet})
                if result and "error" in result:
                    return {"type": "text", "content": result["error"]}
                if result and "info_hash" in result:
                    return {
                        "type": "text",
                        "content": f"Added torrent: `{result['info_hash']}`\n\nUse `torrents list` to check progress.",
                    }
                return {"type": "text", "content": "Failed to add torrent to remote server"}
            info_hash = bt_service.add_magnet(magnet)
            return {
                "type": "text",
                "content": f"Added torrent: `{info_hash}`\n\nUse `torrents list` to check progress.",
            }

        elif subcommand in ("start", "resume") and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            try:
                num = int(parts[1])
                if bt_service == "remote":
                    result = await self._remote_bt_request("/resume", method="POST", json_body={"num": num})
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    # Return updated list after action
                    list_result = await self._remote_bt_request("/list")
                    if list_result and "torrents" in list_result:
                        return {
                            "type": "text",
                            "content": f"▶️ Started torrent #{num}\n\n"
                            + format_torrent_list_from_dicts(list_result["torrents"]),
                        }
                    return {"type": "text", "content": f"▶️ Started torrent #{num}"}
                info_hash = bt_service.get_hash_by_number(num)
                if info_hash and bt_service.resume(info_hash):
                    # Return updated list after action
                    torrents = bt_service.list_torrents()
                    torrent_dicts = [
                        {
                            "name": t.name,
                            "size": t.size,
                            "progress": t.progress,
                            "download_rate": t.download_rate,
                            "upload_rate": t.upload_rate,
                            "state": t.state,
                            "seeders": t.seeders,
                            "peers": t.peers,
                            "is_paused": t.is_paused,
                        }
                        for t in torrents
                    ]
                    return {
                        "type": "text",
                        "content": f"▶️ Started torrent #{num}\n\n" + format_torrent_list_from_dicts(torrent_dicts),
                    }
                return {"type": "text", "content": f"Torrent #{num} not found"}
            except ValueError:
                return {"type": "text", "content": "Usage: `torrents resume <number>`"}

        elif subcommand in ("stop", "pause") and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            try:
                num = int(parts[1])
                if bt_service == "remote":
                    result = await self._remote_bt_request("/pause", method="POST", json_body={"num": num})
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    # Return updated list after action
                    list_result = await self._remote_bt_request("/list")
                    if list_result and "torrents" in list_result:
                        return {
                            "type": "text",
                            "content": f"⏸️ Paused torrent #{num}\n\n"
                            + format_torrent_list_from_dicts(list_result["torrents"]),
                        }
                    return {"type": "text", "content": f"⏸️ Paused torrent #{num}"}
                info_hash = bt_service.get_hash_by_number(num)
                if info_hash and bt_service.pause(info_hash):
                    # Return updated list after action
                    torrents = bt_service.list_torrents()
                    torrent_dicts = [
                        {
                            "name": t.name,
                            "size": t.size,
                            "progress": t.progress,
                            "download_rate": t.download_rate,
                            "upload_rate": t.upload_rate,
                            "state": t.state,
                            "seeders": t.seeders,
                            "peers": t.peers,
                            "is_paused": t.is_paused,
                        }
                        for t in torrents
                    ]
                    return {
                        "type": "text",
                        "content": f"⏸️ Paused torrent #{num}\n\n" + format_torrent_list_from_dicts(torrent_dicts),
                    }
                return {"type": "text", "content": f"Torrent #{num} not found"}
            except ValueError:
                return {"type": "text", "content": "Usage: `torrents pause <number>`"}

        elif subcommand in ("del", "delete", "rm") and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            try:
                num = int(parts[1])
                if bt_service == "remote":
                    result = await self._remote_bt_request(
                        "/remove", method="POST", json_body={"num": num, "delete_files": False}
                    )
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    # Return updated list after action
                    list_result = await self._remote_bt_request("/list")
                    if list_result and "torrents" in list_result:
                        return {
                            "type": "text",
                            "content": f"🗑️ Removed torrent #{num} (files kept)\n\n"
                            + format_torrent_list_from_dicts(list_result["torrents"]),
                        }
                    return {"type": "text", "content": f"🗑️ Removed torrent #{num} (files kept)"}
                info_hash = bt_service.get_hash_by_number(num)
                if info_hash and bt_service.remove(info_hash, delete_files=False):
                    # Return updated list after action
                    torrents = bt_service.list_torrents()
                    torrent_dicts = [
                        {
                            "name": t.name,
                            "size": t.size,
                            "progress": t.progress,
                            "download_rate": t.download_rate,
                            "upload_rate": t.upload_rate,
                            "state": t.state,
                            "seeders": t.seeders,
                            "peers": t.peers,
                            "is_paused": t.is_paused,
                        }
                        for t in torrents
                    ]
                    return {
                        "type": "text",
                        "content": f"🗑️ Removed torrent #{num} (files kept)\n\n"
                        + format_torrent_list_from_dicts(torrent_dicts),
                    }
                return {"type": "text", "content": f"Torrent #{num} not found"}
            except ValueError:
                return {"type": "text", "content": "Usage: `torrents rm <number>`"}

        elif subcommand == "purge" and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            try:
                num = int(parts[1])
                if bt_service == "remote":
                    result = await self._remote_bt_request(
                        "/remove", method="POST", json_body={"num": num, "delete_files": True}
                    )
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    # Return updated list after action
                    list_result = await self._remote_bt_request("/list")
                    if list_result and "torrents" in list_result:
                        return {
                            "type": "text",
                            "content": f"🗑️ Purged torrent #{num} (files deleted)\n\n"
                            + format_torrent_list_from_dicts(list_result["torrents"]),
                        }
                    return {"type": "text", "content": f"🗑️ Purged torrent #{num} (files deleted)"}
                info_hash = bt_service.get_hash_by_number(num)
                if info_hash and bt_service.remove(info_hash, delete_files=True):
                    # Return updated list after action
                    torrents = bt_service.list_torrents()
                    torrent_dicts = [
                        {
                            "name": t.name,
                            "size": t.size,
                            "progress": t.progress,
                            "download_rate": t.download_rate,
                            "upload_rate": t.upload_rate,
                            "state": t.state,
                            "seeders": t.seeders,
                            "peers": t.peers,
                            "is_paused": t.is_paused,
                        }
                        for t in torrents
                    ]
                    return {
                        "type": "text",
                        "content": f"🗑️ Purged torrent #{num} (files deleted)\n\n"
                        + format_torrent_list_from_dicts(torrent_dicts),
                    }
                return {"type": "text", "content": f"Torrent #{num} not found"}
            except ValueError:
                return {"type": "text", "content": "Usage: `torrents purge <number>`"}

        elif subcommand == "info" and len(parts) > 1:
            if not bt_service:
                return {"type": "text", "content": bt_error}
            try:
                num = int(parts[1])
                if bt_service == "remote":
                    result = await self._remote_bt_request(f"/info/{num}")
                    if result and "error" in result:
                        return {"type": "text", "content": result["error"]}
                    if not result or "info_hash" not in result:
                        return {"type": "text", "content": f"Torrent #{num} not found"}
                    # Format remote response
                    files = result.get("files", [])
                    file_list = "\n".join([f"  - {f['path']} ({f['size'] / 1024 / 1024:.1f} MB)" for f in files[:10]])
                    if len(files) > 10:
                        file_list += f"\n  ... and {len(files) - 10} more files"
                    info = f"""## {result["name"]}

**Hash:** `{result["info_hash"]}`
**Status:** {result["state"]} {"(paused)" if result.get("is_paused") else ""}
**Progress:** {result["progress"]:.1f}%
**Size:** {result["size"] / 1024 / 1024:.1f} MB
**Downloaded:** {result["downloaded"] / 1024 / 1024:.1f} MB
**Uploaded:** {result["uploaded"] / 1024 / 1024:.1f} MB
**Speed:** ↓{result["download_rate"] / 1024:.1f} KB/s ↑{result["upload_rate"] / 1024:.1f} KB/s
**Peers:** {result["seeders"]} seeders, {result["peers"]} peers
**Save Path:** {result["save_path"]}

**Files:**
{file_list}
"""
                    return {"type": "text", "content": info}
                info_hash = bt_service.get_hash_by_number(num)
                if not info_hash:
                    return {"type": "text", "content": f"Torrent #{num} not found"}

                t = bt_service.get_torrent(info_hash)
                if not t:
                    return {"type": "text", "content": f"Torrent #{num} not found"}

                files = bt_service.get_files(info_hash)
                file_list = "\n".join([f"  - {f['path']} ({f['size'] / 1024 / 1024:.1f} MB)" for f in files[:10]])
                if len(files) > 10:
                    file_list += f"\n  ... and {len(files) - 10} more files"

                info = f"""## {t.name}

**Hash:** `{t.info_hash}`
**Status:** {t.state} {"(paused)" if t.is_paused else ""}
**Progress:** {t.progress:.1f}%
**Size:** {t.size / 1024 / 1024:.1f} MB
**Downloaded:** {t.downloaded / 1024 / 1024:.1f} MB
**Uploaded:** {t.uploaded / 1024 / 1024:.1f} MB
**Speed:** ↓{t.download_rate / 1024:.1f} KB/s ↑{t.upload_rate / 1024:.1f} KB/s
**Peers:** {t.seeders} seeders, {t.peers} peers
**Save Path:** {t.save_path}

**Files:**
{file_list}
"""
                return {"type": "text", "content": info}
            except ValueError:
                return {"type": "text", "content": "Usage: `torrents info <number>`"}

        # Handle download subcommand: torrents download <category> <number>
        if subcommand in ("download", "dl", "get"):
            if len(parts) < 3:
                return {
                    "type": "text",
                    "content": "Usage: `torrents download <category> <number>`\n\nExample: `torrents download anime 5`",
                }

            category = parts[1].lower()
            if category not in categories:
                return {
                    "type": "text",
                    "content": f"Unknown category: `{category}`\n\nAvailable: movies, tv, music, anime, search",
                }

            try:
                num = int(parts[2])
            except ValueError:
                return {
                    "type": "text",
                    "content": "Please provide a valid number. Example: `torrents download anime 5`",
                }

            # Get cached results for this category
            user_id = self.user.id if self.user else 0
            user_cache = _torrent_cache.get(user_id, {})
            cached = user_cache.get(category, [])

            if not cached:
                return {
                    "type": "text",
                    "content": f"No {category} results cached. Run `torrents` first to load results.",
                }

            if num < 1 or num > len(cached):
                return {"type": "text", "content": f"Invalid number. Choose between 1 and {len(cached)}."}

            torrent = cached[num - 1]
            magnet = torrent.magnet

            if not self.user:
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\n**Magnet:** `{magnet[:100]}...`\n\nLogin required to download.",
                }

            if not bt_service:
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\n**Magnet:** `{magnet[:100]}...`\n\n{bt_error}",
                }

            if bt_service == "remote":
                result = await self._remote_bt_request("/add", method="POST", json_body={"magnet": magnet})
                if result and "error" in result:
                    return {"type": "text", "content": f"**Selected:** {torrent.title}\n\n{result['error']}"}
                if result and "info_hash" in result:
                    return {
                        "type": "text",
                        "content": f"**Downloading:** {torrent.title}\n\nAdded: `{result['info_hash']}`\n\nUse `torrents list` to check progress.",
                    }
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\nFailed to add torrent to remote server",
                }

            info_hash = bt_service.add_magnet(magnet)
            return {
                "type": "text",
                "content": f"**Downloading:** {torrent.title}\n\nAdded: `{info_hash}`\n\nUse `torrents list` to check progress.",
            }

        # Handle search subcommand
        if subcommand in ("search", "s") and len(parts) > 1:
            query = " ".join(parts[1:])
            try:
                import asyncio

                # Add timeout to prevent hanging
                results = await asyncio.wait_for(search_torrents(self.db, query, limit=15), timeout=20)

                if not results:
                    return {"type": "text", "content": f"No results found for '{query}' on torrent site"}

                # Cache results for download command
                user_id = self.user.id if self.user else 0
                _torrent_cache[user_id] = {"search": results}

                formatted = format_torrent_results(results, category="search", title=f"SEARCH: {query.upper()}")
                return {"type": "text", "content": formatted}
            except asyncio.TimeoutError:
                logger.error(f"Torrent search timed out for query: {query}")
                return {"type": "text", "content": f"Search timed out. The torrent site may be slow or unavailable."}
            except ValueError as e:
                msg = str(e)
                suffix = "\n\nConfigure proxy in Admin → Site Settings → BitTorrent Client → HTTP Proxy Host" if "requires http proxy" in msg.lower() else ""
                return {"type": "text", "content": f"{msg}{suffix}"}
            except Exception as e:
                logger.error(f"Torrent search error: {e}")
                return {"type": "text", "content": f"Error searching torrents: {str(e)}"}

        # No subcommand - show all categories overview
        if not subcommand:
            try:
                all_results = await scrape_all_categories(self.db, limit_per_category=10)

                # Cache all results by category
                user_id = self.user.id if self.user else 0
                _torrent_cache[user_id] = all_results

                formatted = format_all_categories(all_results)
                return {"type": "text", "content": formatted}
            except ValueError as e:
                msg = str(e)
                suffix = "\n\nConfigure proxy in Admin → Site Settings → BitTorrent Client → HTTP Proxy Host" if "requires http proxy" in msg.lower() else ""
                return {"type": "text", "content": f"{msg}{suffix}"}
            except Exception as e:
                logger.error(f"Torrents command error: {e}")
                return {"type": "text", "content": f"Error fetching torrents: {str(e)}"}

        # Handle category browsing
        category = subcommand
        if category not in categories:
            return {
                "type": "text",
                "content": f"Unknown category: `{subcommand}`\n\nAvailable: `torrents movies`, `torrents tv`, `torrents music`, `torrents anime`",
            }

        try:
            results = await scrape_torrents(self.db, category, limit=10)

            if not results:
                return {
                    "type": "text",
                    "content": f"No {category} torrents found. The site may be unavailable or not configured.\n\nAdmin can set `torrent_site_url` in settings.",
                }

            # Cache results for download command
            user_id = self.user.id if self.user else 0
            if user_id not in _torrent_cache:
                _torrent_cache[user_id] = {}
            _torrent_cache[user_id][category] = results

            formatted = format_torrent_results(results, category)
            return {"type": "text", "content": formatted}

        except ValueError as e:
            msg = str(e)
            suffix = "\n\nConfigure proxy in Admin → Site Settings → BitTorrent Client → HTTP Proxy Host" if "requires http proxy" in msg.lower() else ""
            return {"type": "text", "content": f"{msg}{suffix}"}
        except Exception as e:
            logger.error(f"Torrents command error: {e}")
            return {"type": "text", "content": f"Error fetching torrents: {str(e)}"}

    async def _nyaa_command(self, arg: str) -> dict:
        """Search nyaa.si for anime torrents"""
        global _nyaa_cache

        parts = arg.strip().split()
        if not parts:
            return {"type": "text", "content": "Usage: `nyaa <search query>`\n\nExample: `nyaa one piece 1080p`"}

        subcommand = parts[0].lower()

        # Handle download subcommand
        if subcommand in ("download", "dl", "get"):
            if len(parts) < 2:
                return {"type": "text", "content": "Usage: `nyaa download <number>`\nFirst search with `nyaa <query>`."}

            try:
                num = int(parts[1])
            except ValueError:
                return {"type": "text", "content": "Please provide a valid number. Example: `nyaa download 3`"}

            # Get cached results
            user_id = self.user.id if self.user else 0
            cached = _nyaa_cache.get(user_id, [])

            if not cached:
                return {"type": "text", "content": "No nyaa results cached. Search first with `nyaa <query>`."}

            if num < 1 or num > len(cached):
                return {"type": "text", "content": f"Invalid number. Choose between 1 and {len(cached)}."}

            torrent = cached[num - 1]
            magnet = torrent.magnet

            if not self.user:
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\n**Magnet:** `{magnet[:100]}...`\n\nLogin required to download.",
                }

            # Use built-in torrent client
            bt_service, bt_error = self._get_bt_service()
            if not bt_service:
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\n**Magnet:** `{magnet[:100]}...`\n\n{bt_error}",
                }

            if bt_service == "remote":
                result = await self._remote_bt_request("/add", method="POST", json_body={"magnet": magnet})
                if result and "error" in result:
                    return {"type": "text", "content": f"**Selected:** {torrent.title}\n\n{result['error']}"}
                if result and "info_hash" in result:
                    return {
                        "type": "text",
                        "content": f"**Downloading:** {torrent.title}\n\nAdded: `{result['info_hash']}`\n\nUse `torrents list` to check progress.",
                    }
                return {
                    "type": "text",
                    "content": f"**Selected:** {torrent.title}\n\nFailed to add torrent to remote server",
                }

            info_hash = bt_service.add_magnet(magnet)
            return {
                "type": "text",
                "content": f"**Downloading:** {torrent.title}\n\nAdded: `{info_hash}`\n\nUse `torrents list` to check progress.",
            }

        # Search query
        query = arg.strip()

        try:
            results = await search_nyaa(query, limit=20)

            if not results:
                return {"type": "text", "content": f"No results found for '{query}' on nyaa.si"}

            # Cache results for download command
            user_id = self.user.id if self.user else 0
            _nyaa_cache[user_id] = results

            formatted = format_nyaa_results(results, query)

            return {"type": "text", "content": formatted}

        except ValueError as e:
            msg = str(e)
            suffix = "\n\nConfigure proxy in Admin → Site Settings → BitTorrent Client → HTTP Proxy Host" if "requires http proxy" in msg.lower() else ""
            return {"type": "text", "content": f"{msg}{suffix}"}
        except Exception as e:
            logger.error(f"Nyaa command error: {e}")
            return {"type": "text", "content": f"Error searching nyaa.si: {str(e)}"}


    async def _news_command(self, arg: str) -> dict:
        """Get news from configured web sources"""
        return await self._dailynews_command(arg)

    def _add_copy_buttons_to_news(self, markdown: str) -> str:
        """Add copy buttons to news article links in markdown."""
        import re

        # Match markdown links in bullet points: - [title](url)
        # Add [Copy](cmd:tui-copy url) after each link
        def add_copy_button(match):
            title = match.group(1)
            url = match.group(2)
            # Return the link with a copy button
            return f"- [{title}]({url}) [Copy](cmd:tui-copy {url})"

        # Pattern: - [title](url)
        pattern = r"- \[([^\]]+)\]\(([^)]+)\)"
        result = re.sub(pattern, add_copy_button, markdown)

        return result

    async def _dailynews_command(self, arg: str) -> dict:
        """Get news from configured web sources (CNN, NPR, etc.)"""
        from datetime import datetime

        if not self.user:
            return {"type": "text", "content": "Please log in to use Daily News."}

        try:
            # Get news sources (user's custom sources or admin defaults)
            all_sources = get_user_news_sources(self.user, self.db)

            if not all_sources:
                return {"type": "text", "content": "No news sources configured. Add sources in User Settings."}

            # If arg provided, filter to matching source
            if arg.strip():
                arg_lower = arg.strip().lower()
                sources = [s for s in all_sources if arg_lower in s["url"].lower() or arg_lower in s["name"].lower()]
                if not sources:
                    source_names = ", ".join(s["name"] for s in all_sources)
                    return {"type": "text", "content": f"No news source matching '{arg.strip()}'. Available sources: {source_names}"}
            else:
                sources = all_sources

            # Fetch news from sources concurrently with timeout
            import asyncio

            async def fetch_single_source(source):
                try:
                    # Add timeout per source to prevent hanging
                    async with asyncio.timeout(45):  # 45 second timeout per source (fetch + AI summary)
                        markdown = await fetch_news_from_source(source["url"], source["name"], self.db)
                        return markdown
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout fetching news from {source['name']}")
                    return f"**{source['name']}:** ⚠️ Timeout fetching headlines (took too long)"
                except Exception as e:
                    logger.error(f"Error fetching news from {source['name']}: {e}")
                    return f"**{source['name']}:** ❌ Error fetching headlines: {str(e)[:100]}"

            results = await asyncio.gather(*[fetch_single_source(s) for s in sources], return_exceptions=True)
            # Filter out any exception results
            results = [r if not isinstance(r, Exception) else f"Error: {str(r)}" for r in results]

            # Format response
            today = datetime.now().strftime("%B %d, %Y %H:%M")
            if len(sources) == 1:
                content = f"## {sources[0]['name']} - {today}\n\n" + results[0] if results else "No headlines found."
            else:
                content = f"## Daily News - {today}\n\n" + "\n\n---\n\n".join(results)

            return {"type": "text", "content": content}

        except Exception as e:
            logger.error(f"Daily news command error: {e}")
            return {"type": "text", "content": f"Error fetching daily news: {str(e)}"}

    async def _logs_command(self, arg: str, notify: Optional[Callable] = None) -> dict:
        """Run the agentic system health report and store it in the Logs chat (admin only).

        Delegates entirely to logs_scheduler.run_logs_for_admin (shared by the scheduler), which
        drives node_service.run_agent across the configured nodes. `notify`, when given, streams
        the per-command play-by-play to the originating channel (web UI / Telegram)."""
        if not self.user:
            return {"type": "text", "content": "Please log in to use the logs command."}

        # Admin only (user ID 1)
        if self.user.id != 1:
            return {"type": "text", "content": "The logs command is only available to administrators."}

        try:
            from app.services.logs_scheduler import run_logs_for_admin
            text = await run_logs_for_admin(return_text=True, notify=notify, deliver_telegram=False)
            return {"type": "text", "content": text or "No report generated."}
        except Exception as e:
            logger.error(f"Logs command error: {e}")
            return {"type": "text", "content": f"Error generating health report: {str(e)}"}

    async def _node_command(self, arg: str, notify: Optional[Callable] = None) -> dict:
        """Run OS commands on configured nodes (SSH or local) as background jobs, with an
        optional agentic mode. Gated by admin Settings (enabled + user allowlist).

        `notify`, when given, is an async callback the caller supplies to deliver a
        finished job's output back to the channel the command came from (web UI
        conversation or Telegram chat). It must not rely on the request's DB session,
        which is closed by the time a long-running job finishes."""
        from app.services import node_service

        if not node_service.user_allowed(self.db, self.user):
            return {"type": "text", "content": "⛔ Remote node management is disabled or you are not authorized. An admin can enable it in Admin → Services → Remote Node Management."}

        parts = arg.strip().split(maxsplit=2)
        sub = parts[0].lower() if parts else ""
        nodes = node_service.get_nodes(self.db)

        def _fmt_nodes() -> str:
            if not nodes:
                return "No nodes configured. Add them in Admin → Services → Remote Node Management (one per line: `name|user@host`)."
            lines = ["**Configured nodes:**"]
            for name, target in nodes.items():
                where = "this host" if target == "local" else target
                lines.append(f"- `{name}` → {where}")
            return "\n".join(lines)

        def _result_for(job, header: str) -> dict:
            """Render a finished job. Short output goes inline; long output shows a tail
            preview inline and attaches the full output as a .txt (delivered as a Telegram
            document or a web-UI download link by the existing `type=='files'` handlers)."""
            out = (job.output or "(no output)").strip()
            preview = f"{header}\n\n```\n{node_service.tail(out, node_service.INLINE_LIMIT)}\n```"
            if len(out) > node_service.INLINE_LIMIT:
                return {
                    "type": "files",
                    "content": preview,
                    "files": [{"filename": f"node-{job.node}-job{job.id}.txt", "data": out.encode("utf-8", "replace")}],
                }
            return {"type": "text", "content": preview}

        # --- management subcommands ---
        if sub in ("", "list", "ls", "help"):
            usage = (
                "**Remote node management**\n\n"
                "- `node <name> <command>` — run a command (long ones run in the background)\n"
                "- `node all <command>` — run a command on every node\n"
                "- `node agent <name> <goal>` — let the AI run commands toward a goal\n"
                "- `node jobs` — list your recent jobs\n"
                "- `node log <id>` — show a job's output\n"
                "- `node kill <id>` — stop a running job\n\n"
                f"{_fmt_nodes()}"
            )
            return {"type": "text", "content": usage}

        if sub == "jobs":
            jobs = node_service.list_jobs(user_id=self.user.id if self.user else None)
            if not jobs:
                return {"type": "text", "content": "No jobs yet."}
            icon = {"running": "⏳", "done": "✅", "failed": "❌", "killed": "🛑"}
            lines = ["**Your node jobs:**"]
            for j in jobs:
                lines.append(f"- {icon.get(j.status, '•')} #{j.id} `{j.node}`: `{j.command[:60]}` — {j.status}")
            lines.append("\nUse `node log <id>` for output.")
            return {"type": "text", "content": "\n".join(lines)}

        if sub == "log":
            if len(parts) < 2 or not parts[1].isdigit():
                return {"type": "text", "content": "Usage: `node log <id>`"}
            job = node_service.get_job(int(parts[1]), user_id=self.user.id if self.user else None)
            if not job:
                return {"type": "text", "content": f"Job #{parts[1]} not found."}
            return _result_for(job, f"**Job #{job.id}** `{job.node}` — {job.status} (exit {job.exit_code})\n`{job.command}`")

        if sub == "kill":
            if len(parts) < 2 or not parts[1].isdigit():
                return {"type": "text", "content": "Usage: `node kill <id>`"}
            _uid = self.user.id if self.user else None
            job = node_service.get_job(int(parts[1]), user_id=_uid)
            if not job:
                return {"type": "text", "content": f"Job #{parts[1]} not found."}
            ok = node_service.kill_job(int(parts[1]), user_id=_uid)
            return {"type": "text", "content": f"{'🛑 Killed' if ok else 'Could not kill (already finished?)'} job #{parts[1]}."}

        # --- fan-out: run the same command on every node ---
        if sub == "all":
            import asyncio
            command = arg.strip()[len(parts[0]):].strip()
            if not command:
                return {"type": "text", "content": "Usage: `node all <command>`"}
            if not nodes:
                return {"type": "text", "content": _fmt_nodes()}
            jobs = {
                name: node_service.start_job(
                    self.db, name, target, command,
                    user_id=self.user.id if self.user else None,
                )
                for name, target in nodes.items()
            }
            await asyncio.gather(*(node_service.await_job(j, wait=10.0) for j in jobs.values()))
            icon = {"done": "✅", "failed": "❌", "killed": "🛑"}
            lines = [f"## `{command}` on {len(jobs)} node(s)"]
            for name, j in jobs.items():
                if j.done:
                    out = (j.output or "(no output)").strip()
                    lines.append(f"\n**{icon.get(j.status, 'ℹ️')} {name}** (exit {j.exit_code})\n```\n{node_service.tail(out, 1200)}\n```")
                else:
                    # Still running — deliver its output to this channel when it finishes.
                    node_service.notify_on_done(j, notify)
                    lines.append(f"\n**⏳ {name}** — still running (job #{j.id}, `node log {j.id}`)")
            return {"type": "text", "content": "\n".join(lines)}

        # --- agentic mode ---
        if sub == "agent":
            if len(parts) < 3:
                return {"type": "text", "content": "Usage: `node agent <name> <goal>` (or `node agent all <goal>`)"}
            name, goal = parts[1], parts[2]

            # `node agent all <goal>` — run the agent on every node toward the same goal.
            # Sequential (not parallel): they share one DB session, and LLM inference serializes
            # on the GPU lock anyway, so parallelism wouldn't help and risks interleaved state.
            if name == "all":
                sections = []
                for _n, _t in nodes.items():
                    _nfy = (lambda txt, _p=_n: notify(f"[{_p}] {txt}")) if notify else None
                    try:
                        sections.append(await node_service.run_agent(
                            self.db, self.user, _n, _t, goal, self.chat_service, notify=_nfy))
                    except Exception as e:
                        logger.error(f"[node] agent error on {_n}: {e}", exc_info=True)
                        sections.append(f"## Agent on `{_n}` — goal: {goal}\n\n**⚠️ Error:** {e}")
                return {"type": "text", "content": "\n\n---\n\n".join(sections)}

            if name not in nodes:
                return {"type": "text", "content": f"Unknown node `{name}`.\n\n{_fmt_nodes()}"}
            try:
                summary = await node_service.run_agent(self.db, self.user, name, nodes[name], goal, self.chat_service, notify=notify)
                return {"type": "text", "content": summary}
            except Exception as e:
                logger.error(f"[node] agent error: {e}", exc_info=True)
                return {"type": "text", "content": f"Agent error: {e}"}

        # --- direct command: node <name> <command...> ---
        name = sub
        if name not in nodes:
            return {"type": "text", "content": f"Unknown node `{name}`.\n\n{_fmt_nodes()}"}
        # Everything after the node name is the command (preserve original spacing/casing).
        command = arg.strip()[len(parts[0]):].strip()
        if not command:
            return {"type": "text", "content": f"Usage: `node {name} <command>`"}

        job = node_service.start_job(
            self.db, name, nodes[name], command,
            user_id=self.user.id if self.user else None,
        )
        await node_service.await_job(job, wait=8.0)
        if job.done:
            icon = {"done": "✅", "failed": "❌", "killed": "🛑"}.get(job.status, "ℹ️")
            return _result_for(job, f"{icon} `{name}` exit {job.exit_code}")
        # Still running — deliver its output to this channel when it finishes.
        node_service.notify_on_done(job, notify)
        return {"type": "text", "content": f"⏳ Started job #{job.id} on `{name}` (still running).\nI'll post the output here when it's done — or check with `node log {job.id}` / stop with `node kill {job.id}`."}

    async def check_youtube_url(self, message: str) -> Optional[dict]:
        """Check if message contains a YouTube URL and summarize it"""
        if not is_youtube_url(message):
            return None

        # Don't auto-summarize if user wants to download
        lower = message.lower()
        download_keywords = ["download", "ytdl", "mp3", "save", "get song", "get video", "download song", "download video"]
        if any(kw in lower for kw in download_keywords):
            return None

        urls = extract_youtube_urls(message)
        if not urls:
            return None

        # Summarize the first YouTube URL found
        success, result = await summarize_youtube(urls[0], self.chat_service)
        # Return result whether success or failure (so user sees error messages)
        return {"type": "text", "content": result}

    async def _mail_command(self, arg: str, attachments: Optional[list] = None) -> dict:
        """Email commands - inbox, read, reply, delete, send"""
        if not self.user:
            return {"type": "text", "content": "Please log in to use the mail command."}

        accounts = get_user_mail_accounts(self.user.id, self.db)
        if not accounts:
            return {"type": "text", "content": "No email accounts configured. Add accounts in User Settings > Mail."}

        parts = arg.strip().split(maxsplit=3)
        subcommand = parts[0].lower() if parts else "inbox"

        try:
            if subcommand in ("inbox", ""):
                # List recent messages from all accounts
                # Wrap in asyncio timeout to prevent hanging
                import asyncio
                try:
                    messages = await asyncio.wait_for(
                        asyncio.to_thread(fetch_all_accounts, self.user.id, self.db, limit_per_account=10),
                        timeout=20.0  # 20 second total timeout
                    )
                    if not messages:
                        messages = []  # Ensure it's a list
                    return {"type": "text", "content": format_message_list(messages)}
                except asyncio.TimeoutError:
                    logger.warning("Mail fetch timed out after 20 seconds")
                    return {"type": "text", "content": "Mail fetch timed out. The mail server may be slow or unreachable. Please try again."}

            elif subcommand == "unread":
                # List unread messages only
                messages = fetch_all_accounts(self.user.id, self.db, limit_per_account=20, unread_only=True)
                if not messages:
                    return {"type": "text", "content": "No unread messages."}
                return {"type": "text", "content": format_message_list(messages)}

            elif subcommand == "folders":
                # List folders for an account
                if len(parts) < 2:
                    # Show account selection buttons
                    lines = ["## Select Account\n"]
                    for acc in accounts:
                        account_short = acc.email.split("@")[0]
                        cmd = f"mail folders {account_short}"
                        lines.append(f"[{acc.email}](cmd:{cmd})")
                    return {"type": "text", "content": "\n\n".join(lines)}

                account_hint = parts[1]
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                folders = list_folders(self.user.id, self.db, account_email)
                if not folders:
                    return {"type": "text", "content": f"No folders found for {account_email}."}

                return {"type": "text", "content": format_folder_list(folders, account_email)}

            elif subcommand == "folder":
                # Browse a specific folder
                if len(parts) < 3:
                    return {
                        "type": "text",
                        "content": "Usage: `mail folder <account> <folder>`\n\nExample: `mail folder work INBOX.Sent`",
                    }

                account_hint = parts[1]
                # Get folder name (may contain spaces)
                folder_parts = arg.strip().split(maxsplit=2)
                folder_name = folder_parts[2] if len(folder_parts) > 2 else ""

                if not folder_name:
                    return {"type": "text", "content": "Please provide a folder name."}

                # Find matching account
                account_email = None
                account = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        account = acc
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                messages = fetch_messages(account, folder=folder_name, limit=20)
                if not messages:
                    return {"type": "text", "content": f"No messages in folder '{folder_name}'."}

                return {
                    "type": "text",
                    "content": format_message_list(messages, folder=folder_name, account_email=account_email),
                }

            elif subcommand == "sum":
                # Summarize all inbox messages
                account_hint = parts[1] if len(parts) > 1 else None

                if account_hint:
                    # Find matching account
                    account_email = None
                    for acc in accounts:
                        if account_hint.lower() in acc.email.lower():
                            account_email = acc.email
                            messages = fetch_messages(acc, limit=20)
                            break
                    if not account_email:
                        return {"type": "text", "content": f"Account '{account_hint}' not found."}
                else:
                    # Fetch from all accounts
                    messages = fetch_all_accounts(self.user.id, self.db, limit_per_account=10)

                if not messages:
                    return {"type": "text", "content": "No messages to summarize."}

                # Build summary of all messages for AI
                msg_list = []
                for msg in messages:
                    msg_list.append(
                        f"- From: {msg.sender} | Subject: {msg.subject} | Date: {msg.date.strftime('%b %d')}"
                    )

                # Use AI to summarize
                ai_messages = [
                    {
                        "role": "system",
                        "content": "Summarize this inbox. Group by sender or topic. Highlight urgent items, action items, and important dates. Be concise.",
                    },
                    {"role": "user", "content": f"Inbox ({len(messages)} messages):\n" + "\n".join(msg_list)},
                ]
                summary = await self.chat_service.chat(ai_messages)
                return {"type": "text", "content": f"## Inbox Summary ({len(messages)} messages)\n\n{summary}"}

            elif subcommand == "search":
                # Support both: mail search <query> (default account) or mail search <account> <query>
                if len(parts) < 2:
                    return {
                        "type": "text",
                        "content": "Usage: `mail search <query>` or `mail search <account> <query>`\n\nExample: `mail search invoice` or `mail search yummy invoice`",
                    }

                # Check if parts[1] looks like an account hint (contains @ or matches an account)
                potential_account = parts[1]
                account_email = None
                for acc in accounts:
                    if potential_account.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if account_email and len(parts) >= 3:
                    # mail search <account> <query>
                    query_parts = arg.strip().split(maxsplit=2)
                    query = query_parts[2] if len(query_parts) > 2 else ""
                else:
                    # mail search <query> - use first account
                    account_email = accounts[0].email
                    query_parts = arg.strip().split(maxsplit=1)
                    query = query_parts[1] if len(query_parts) > 1 else ""

                if not query:
                    return {"type": "text", "content": "Please provide a search query."}

                messages = search_messages(self.user.id, self.db, account_email, query)
                if not messages:
                    return {"type": "text", "content": f"No messages found matching '{query}'."}
                return {
                    "type": "text",
                    "content": f"## ◈ SEARCH: {query.upper()} ◈\n\n" + format_message_list(messages, show_header=False),
                }

            elif subcommand == "read":
                # Support both: mail read <id> (default account) or mail read <account> <id>
                if len(parts) < 2:
                    return {
                        "type": "text",
                        "content": "Usage: `mail read <id>` or `mail read <account> <id>`\n\nExample: `mail read 123` or `mail read verita84 INBOX.Archive:123`",
                    }

                # Check if parts[1] is numeric (id) or account hint
                # Strip # prefix if present (e.g., "#2" -> "2")
                test_val = parts[1].lstrip("#")
                if len(parts) == 2 or test_val.isdigit() or re.match(r"^\d+$|^[\w.-]+:\d+$", test_val):
                    # mail read <id> - use first account
                    account_hint = None
                    uid_part = parts[1].lstrip("#")
                else:
                    # mail read <account> <id>
                    if len(parts) < 3:
                        return {"type": "text", "content": "Usage: `mail read <id>` or `mail read <account> <id>`"}
                    account_hint = parts[1]
                    uid_part = parts[2]

                # Parse folder:uid format (e.g., "INBOX.Archive:123")
                folder = None
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)
                else:
                    uid = uid_part

                # Sanitize UID - extract only numeric portion (strip emojis/extra chars)
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account or use first account
                account_email = None
                if account_hint:
                    for acc in accounts:
                        if account_hint.lower() in acc.email.lower():
                            account_email = acc.email
                            break
                    if not account_email:
                        return {"type": "text", "content": f"Account '{account_hint}' not found."}
                else:
                    # Default to first account
                    account_email = accounts[0].email

                msg = get_message_by_id(self.user.id, self.db, account_email, uid, folder=folder)
                if not msg:
                    return {"type": "text", "content": f"Message {uid} not found."}

                return {"type": "text", "content": format_message_detail(msg, folder=folder)}

            elif subcommand == "summary":
                # Support both: mail summary <id> (default account) or mail summary <account> <id>
                if len(parts) < 2:
                    return {
                        "type": "text",
                        "content": "Usage: `mail summary <id>` or `mail summary <account> <id>`\n\nExample: `mail summary 123` or `mail summary work 456`",
                    }

                # Check if parts[1] is numeric (id) or account hint
                test_val = parts[1].lstrip("#")
                if len(parts) == 2 or test_val.isdigit() or re.match(r"^\d+$|^[\w.-]+:\d+$", test_val):
                    # mail summary <id> - use first account
                    account_hint = None
                    uid_part = parts[1].lstrip("#")
                else:
                    # mail summary <account> <id>
                    if len(parts) < 3:
                        return {
                            "type": "text",
                            "content": "Usage: `mail summary <id>` or `mail summary <account> <id>`",
                        }
                    account_hint = parts[1]
                    uid_part = parts[2]

                # Parse folder:uid format
                folder = None
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)
                else:
                    uid = uid_part

                # Sanitize UID - extract only numeric portion (strip emojis/extra chars)
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account or use first account
                account_email = None
                if account_hint:
                    for acc in accounts:
                        if account_hint.lower() in acc.email.lower():
                            account_email = acc.email
                            break
                    if not account_email:
                        return {"type": "text", "content": f"Account '{account_hint}' not found."}
                else:
                    account_email = accounts[0].email

                msg = get_message_by_id(self.user.id, self.db, account_email, uid, folder=folder)
                if not msg:
                    return {"type": "text", "content": f"Message {uid} not found."}

                # Use AI to summarize
                messages = [
                    {
                        "role": "system",
                        "content": "Summarize this email concisely. Include key points, action items, and important dates if any.",
                    },
                    {"role": "user", "content": f"From: {msg.sender}\nSubject: {msg.subject}\n\n{msg.body_text}"},
                ]
                summary = await self.chat_service.chat(messages)
                return {"type": "text", "content": f"## Summary of: {msg.subject}\n\n{summary}"}

            elif subcommand == "translate":
                # Support both: mail translate <id> [language] or mail translate <account> <id> [language]
                # Language defaults to English if not specified
                if len(parts) < 2:
                    return {
                        "type": "text",
                        "content": "Usage: `mail translate <id> [language]` or `mail translate <account> <id> [language]`\n\nExamples:\n- `mail translate 123` - translates to English\n- `mail translate 123 spanish` - translates to Spanish\n- `mail translate work 123 japanese` - translates to Japanese",
                    }

                # Check if parts[1] is numeric (id) or account hint
                test_val = parts[1].lstrip("#")
                if test_val.isdigit() or re.match(r"^\d+$|^[\w.-]+:\d+$", test_val):
                    # mail translate <id> [language] - use first account
                    account_hint = None
                    uid_part = parts[1].lstrip("#")
                    language = parts[2] if len(parts) > 2 else "English"
                else:
                    # mail translate <account> <id> [language]
                    if len(parts) < 3:
                        return {
                            "type": "text",
                            "content": "Usage: `mail translate <id> [language]` or `mail translate <account> <id> [language]`",
                        }
                    account_hint = parts[1]
                    uid_part = parts[2]
                    language = parts[3] if len(parts) > 3 else "English"

                # Parse folder:uid format
                folder = None
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)
                else:
                    uid = uid_part

                # Sanitize UID - extract only numeric portion (strip emojis/extra chars)
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account or use first account
                account_email = None
                if account_hint:
                    for acc in accounts:
                        if account_hint.lower() in acc.email.lower():
                            account_email = acc.email
                            break
                    if not account_email:
                        return {"type": "text", "content": f"Account '{account_hint}' not found."}
                else:
                    account_email = accounts[0].email

                msg = get_message_by_id(self.user.id, self.db, account_email, uid, folder=folder)
                if not msg:
                    return {"type": "text", "content": f"Message {uid} not found."}

                # Use AI to translate
                messages = [
                    {
                        "role": "system",
                        "content": f"You are a translator. Translate the ENTIRE email below to {language}. CRITICAL: You MUST translate every single word, sentence, and paragraph completely. Do NOT summarize. Do NOT skip any content. Do NOT add commentary. Preserve all original formatting. Output ONLY the complete translated text.",
                    },
                    {"role": "user", "content": f"From: {msg.sender}\nSubject: {msg.subject}\n\n{msg.body_text}"},
                ]
                translation = await self.chat_service.chat(messages)
                return {"type": "text", "content": f"## {msg.subject} ({language})\n\n{translation}"}

            elif subcommand == "extract-event":
                return {"type": "text", "content": "⚠️ Calendar event extraction is temporarily unavailable."}

            elif subcommand == "extract-bill":
                return {"type": "text", "content": "⚠️ Bill extraction is temporarily unavailable."}

            elif subcommand == "reply":
                if len(parts) < 4:
                    return {
                        "type": "text",
                        "content": "Usage: `mail reply <account> [folder:]<id> <message>`\n\nExample: `mail reply verita84 123 Thanks for the info!` or `mail reply verita84 INBOX.Archive:456 Thanks!`",
                    }

                account_hint = parts[1]
                uid_part = parts[2]
                reply_body = parts[3]
                if len(parts) < 4:
                    return {
                        "type": "text",
                        "content": "Usage: `mail reply <account> [folder:]<id> <message>`\n\nExample: `mail reply verita84 123 Thanks for the info!` or `mail reply verita84 INBOX.Archive:456 Thanks!`",
                    }

                account_hint = parts[1]
                uid_part = parts[2]
                reply_body = parts[3]

                # Parse folder:uid format (e.g., "INBOX.Archive:456")
                folder = "INBOX"
                uid = uid_part
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)

                # Sanitize UID - extract only numeric portion (strip emojis/extra chars)
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                import asyncio

                # Pass attachments if available
                success = await asyncio.to_thread(
                    reply_to_message, self.user.id, self.db, account_email, uid, reply_body, 
                    reply_all=False, attachments=attachments, folder=folder
                )
                if success:
                    attachment_note = f" with {len(attachments)} attachment(s)" if attachments else ""
                    return {"type": "text", "content": f"Reply sent successfully{attachment_note}."}
                else:
                    return {"type": "text", "content": "Failed to send reply."}

            elif subcommand == "forward":
                if len(parts) < 4:
                    return {
                        "type": "text",
                        "content": "Usage: `mail forward <account> [folder:]<id> <recipient> [message]`\n\n**Examples:**\n- `mail forward verita84 123 john@example.com` - Forward message #123 to john@example.com\n- `mail forward verita84 123 john@example.com Check this out!` - Forward with custom message\n- `mail forward verita84 123 john@example.com \"case #12345\" Hello, here is my info:` - Forward with multi-line message\n\n**Note:** The message body can be multi-line. Original message attachments are automatically included.",
                    }

                account_hint = parts[1]
                uid_part = parts[2]
                # parts[3] contains recipient and optionally body text (due to maxsplit=3 in mail command handler)
                recipient_and_body = parts[3].strip()
                
                # Extract recipient - look for email pattern (contains @) or take first word
                # Handle quoted recipients and extract email address
                recipient = None
                forward_body = ""
                
                # Try to find an email address pattern in the string
                # Email pattern: word characters, dots, hyphens, plus signs, followed by @, then domain
                email_pattern = r'\b[\w\.\-+]+@[\w\.\-]+\.[a-zA-Z]{2,}\b'
                email_match = re.search(email_pattern, recipient_and_body)
                
                if email_match:
                    # Found an email address - extract it and everything after it is the body
                    email_start = email_match.start()
                    email_end = email_match.end()
                    recipient = email_match.group(0).strip('"\'')  # Remove quotes if present
                    # Get body text after the email (skip any spaces immediately after)
                    body_start = email_end
                    while body_start < len(recipient_and_body) and recipient_and_body[body_start] in ' \t':
                        body_start += 1
                    if body_start < len(recipient_and_body):
                        forward_body = recipient_and_body[body_start:].strip()
                else:
                    # No email pattern found - try to extract first word/token as recipient
                    # Remove quotes if present
                    tokens = recipient_and_body.split(maxsplit=1)
                    recipient = tokens[0].strip('"\'')
                    if len(tokens) > 1:
                        forward_body = tokens[1].strip()
                
                # Sanitize recipient - remove newlines, quotes, and other invalid characters for email headers
                if recipient:
                    recipient = recipient.replace("\n", " ").replace("\r", "").strip()
                    # Remove surrounding quotes if present
                    recipient = recipient.strip('"\'')
                else:
                    recipient = ""
                
                # Basic email validation - check if it looks like an email address
                # Must contain @ and have a domain part (something after @)
                if not recipient:
                    return {"type": "text", "content": "No recipient email address provided. Usage: `mail forward <account> <id> <recipient> [message]`"}
                
                if "@" not in recipient:
                    return {"type": "text", "content": f"Invalid recipient: `{recipient}`. Please provide a valid email address (must contain @). Example: `mail forward verita84 123 user@example.com`"}
                
                # Check that there's a domain part after @
                email_parts = recipient.split("@")
                if len(email_parts) != 2 or not email_parts[1] or "." not in email_parts[1]:
                    return {"type": "text", "content": f"Invalid email address: `{recipient}`. Email must have a domain (e.g., user@example.com)."}

                # Parse folder:uid format (e.g., "INBOX.Archive:456")
                folder = "INBOX"
                uid = uid_part
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)

                # Sanitize UID - extract only numeric portion (strip emojis/extra chars)
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                import asyncio

                # Pass attachments if available
                success = await asyncio.to_thread(
                    forward_message, self.user.id, self.db, account_email, uid, recipient, forward_body, 
                    attachments=attachments, folder=folder
                )
                if success:
                    attachment_note = f" with {len(attachments)} attachment(s)" if attachments else ""
                    return {"type": "text", "content": f"Email forwarded to {recipient} successfully{attachment_note}."}
                else:
                    return {"type": "text", "content": "Failed to forward email."}

            elif subcommand == "delete":
                # Support both: mail delete <id> (default account) or mail delete <account> <id>
                if len(parts) < 2:
                    return {
                        "type": "text",
                        "content": "Usage: `mail delete <id>` or `mail delete <account> [folder:]<id>`\n\nExample: `mail delete 123` or `mail delete verita84 INBOX.Archive:456`",
                    }

                # Check if parts[1] is numeric (id) or account hint
                # Strip # prefix if present (e.g., "#2" -> "2")
                test_val = parts[1].lstrip("#")
                if len(parts) == 2 or test_val.isdigit() or re.match(r"^\d+$|^[\w.-]+:\d+$", test_val):
                    # mail delete <id> - use first account
                    account_hint = None
                    uid_part = parts[1].lstrip("#")
                else:
                    # mail delete <account> <id>
                    if len(parts) < 3:
                        return {"type": "text", "content": "Usage: `mail delete <id>` or `mail delete <account> <id>`"}
                    account_hint = parts[1]
                    uid_part = parts[2]

                # Parse folder:uid format (e.g., "INBOX.Archive:456")
                folder = "INBOX"
                uid = uid_part
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)

                # Sanitize UID - extract only numeric portion (strip emojis/extra chars)
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account or use first account
                account_email = None
                if account_hint:
                    for acc in accounts:
                        if account_hint.lower() in acc.email.lower():
                            account_email = acc.email
                            break
                    if not account_email:
                        return {"type": "text", "content": f"Account '{account_hint}' not found."}
                else:
                    # Default to first account
                    account_email = accounts[0].email

                import asyncio

                success = await asyncio.to_thread(delete_message, self.user.id, self.db, account_email, uid, folder)
                if success:
                    return {"type": "text", "content": f"Message {uid} deleted from {folder}."}
                else:
                    return {"type": "text", "content": f"Failed to delete message {uid} from {folder}."}

            elif subcommand in ("deleteall", "purge", "clear"):
                if len(parts) < 2:
                    return {
                        "type": "text",
                        "content": "Usage: `mail deleteall <account>`\n\nExample: `mail deleteall verita84`\n\n**Warning:** This will delete ALL messages in the inbox!",
                    }

                account_hint = parts[1]

                # Find matching account
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                import asyncio

                count = await asyncio.to_thread(delete_all_messages, self.user.id, self.db, account_email)
                if count >= 0:
                    return {"type": "text", "content": f"🗑️ Deleted {count} messages from {account_email}"}
                else:
                    return {"type": "text", "content": f"Failed to delete messages from {account_email}."}

            elif subcommand == "archive":
                # Support both: mail archive <id> (default account) or mail archive <account> [folder:]<id>
                if len(parts) < 2:
                    return {
                        "type": "text",
                        "content": "Usage: `mail archive <id>` or `mail archive <account> [folder:]<id>`\n\nExample: `mail archive 123` or `mail archive verita84 456`",
                    }

                # Check if parts[1] is numeric (id) or account hint
                # Strip # prefix if present (e.g., "#2" -> "2")
                test_val = parts[1].lstrip("#")
                if len(parts) == 2 or test_val.isdigit():
                    # mail archive <id> - use first account
                    account_hint = None
                    uid_part = parts[1].lstrip("#")
                else:
                    # mail archive <account> <id>
                    if len(parts) < 3:
                        return {
                            "type": "text",
                            "content": "Usage: `mail archive <id>` or `mail archive <account> [folder:]<id>`",
                        }
                    account_hint = parts[1]
                    uid_part = parts[2]

                # Parse folder:uid format (e.g., "INBOX.Archive:456")
                folder = "INBOX"
                uid = uid_part
                if ":" in uid_part:
                    folder, uid = uid_part.rsplit(":", 1)

                # Sanitize UID - extract only numeric portion (strip emojis/extra chars)
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account or use first account
                account_email = None
                if account_hint:
                    for acc in accounts:
                        if account_hint.lower() in acc.email.lower():
                            account_email = acc.email
                            break
                    if not account_email:
                        return {"type": "text", "content": f"Account '{account_hint}' not found."}
                else:
                    # Default to first account
                    account_email = accounts[0].email

                import asyncio

                success = await asyncio.to_thread(
                    archive_message, self.user.id, self.db, account_email, uid, folder=folder
                )
                if success:
                    return {"type": "text", "content": f"📦 Message {uid} archived."}
                else:
                    return {"type": "text", "content": f"Failed to archive message {uid}."}

            elif subcommand == "attachment":
                # Download and open attachment: mail attachment <account> <uid> <index>
                if len(parts) < 4:
                    return {"type": "text", "content": "Usage: `mail attachment <account> <uid> <index>`"}

                account_hint = parts[1]
                uid = parts[2]
                try:
                    att_index = int(parts[3])
                except ValueError:
                    return {"type": "text", "content": "Invalid attachment index. Must be a number."}

                # Sanitize UID
                uid_match = re.search(r"^(\d+)", uid)
                if not uid_match:
                    return {"type": "text", "content": f"Invalid message ID: `{uid}`. Must be a number."}
                uid = uid_match.group(1)

                # Find matching account
                account_email = None
                for acc in accounts:
                    if account_hint.lower() in acc.email.lower():
                        account_email = acc.email
                        break

                if not account_email:
                    return {"type": "text", "content": f"Account '{account_hint}' not found."}

                # Get the attachment
                attachment = get_attachment(self.user.id, self.db, account_email, uid, att_index)
                if not attachment:
                    return {"type": "text", "content": f"Attachment not found."}

                if not attachment.data:
                    return {"type": "text", "content": f"Attachment too large or couldn't be downloaded."}

                # Don't save automatically - just display the attachment with a save button
                # Encode attachment data as base64 for display
                import base64
                attachment_base64 = base64.b64encode(attachment.data).decode('utf-8')
                
                # Determine MIME type
                import mimetypes
                mime_type, _ = mimetypes.guess_type(attachment.filename)
                if not mime_type:
                    mime_type = 'application/octet-stream'
                
                # Return attachment data for display (image preview if it's an image, otherwise download button)
                if mime_type.startswith('image/'):
                    return {
                        "type": "mail_attachment",
                        "content": f"📎 **{attachment.filename}** ({attachment.size / 1024:.1f} KB)",
                        "filename": attachment.filename,
                        "data": attachment_base64,
                        "mime_type": mime_type,
                        "size": attachment.size,
                        "account": account_email,
                        "uid": uid,
                        "index": att_index,
                    }
                else:
                    return {
                        "type": "mail_attachment",
                        "content": f"📎 **{attachment.filename}** ({attachment.size / 1024:.1f} KB)",
                        "filename": attachment.filename,
                        "data": attachment_base64,
                        "mime_type": mime_type,
                        "size": attachment.size,
                        "account": account_email,
                        "uid": uid,
                        "index": att_index,
                    }

            elif subcommand == "send":
                # Explicit send: mail send [account] <recipient> ["subject"] <message>
                if len(parts) < 3:
                    return {
                        "type": "text",
                        "content": 'Usage: `mail send [account] <recipient> ["subject"] <message>`\n\nExamples:\n- `mail send linda Hey!` - auto-generate subject\n- `mail send linda "Meeting" Can we meet tomorrow?` - with subject\n- `mail send work linda Hey!` - uses \'work\' account',
                    }

                # Check if parts[1] is an account hint or recipient
                from_account = None
                recipient_idx = 1

                # Check if first arg matches an account
                for acc in accounts:
                    if parts[1].lower() in acc.email.lower():
                        from_account = acc
                        recipient_idx = 2
                        break

                if recipient_idx == 2 and len(parts) < 4:
                    return {
                        "type": "text",
                        "content": 'Usage: `mail send <account> <recipient> ["subject"] <message>`\n\nExample: `mail send work linda@example.com Hey, how are you?`',
                    }

                recipient = parts[recipient_idx]

                # Re-split to get full text after recipient
                full_parts = arg.strip().split(maxsplit=recipient_idx + 1)
                rest = full_parts[recipient_idx + 1] if len(full_parts) > recipient_idx + 1 else ""

                # Check for quoted subject
                subject = None
                message_body = rest
                if rest.startswith('"'):
                    # Find closing quote
                    end_quote = rest.find('"', 1)
                    if end_quote > 0:
                        subject = rest[1:end_quote]
                        message_body = rest[end_quote + 1 :].strip()

                return await self._send_new_mail(
                    accounts, recipient, message_body, attachments, from_account=from_account, subject=subject
                )

            else:
                # Check if this is a shorthand send: mail <recipient> ["subject"] <message>
                # First word is not a known subcommand, treat as recipient
                if len(parts) >= 2:
                    recipient = parts[0]
                    # Get the full text after the recipient
                    full_parts = arg.strip().split(maxsplit=1)
                    rest = full_parts[1] if len(full_parts) > 1 else ""

                    # Check for quoted subject
                    subject = None
                    message_body = rest
                    if rest.startswith('"'):
                        # Find closing quote
                        end_quote = rest.find('"', 1)
                        if end_quote > 0:
                            subject = rest[1:end_quote]
                            message_body = rest[end_quote + 1 :].strip()

                    return await self._send_new_mail(accounts, recipient, message_body, attachments, subject=subject)

                return {
                    "type": "text",
                    "content": 'Usage:\n- `mail` - Recent messages\n- `mail folders` - Browse IMAP folders\n- `mail folder <account> <folder>` - View folder contents\n- `mail sum <account>` - AI summary of inbox\n- `mail search <account> <query>` - Search messages\n- `mail send [account] <contact> ["subject"] <message>` - Send email\n- `mail read <account> [folder:]<id>` - Read message\n- `mail reply <account> [folder:]<id> <message>` - Reply\n- `mail translate <account> [folder:]<id>` - Translate message\n- `mail archive <account> <id>` - Archive\n- `mail delete <account> [folder:]<id>` - Delete',
                }

        except Exception as e:
            logger.error(f"Mail command error: {e}")
            return {"type": "text", "content": f"Error: {str(e)}"}

    async def _send_new_mail(
        self,
        accounts: list,
        recipient: str,
        message_body: str,
        attachments: Optional[list] = None,
        from_account=None,
        subject: Optional[str] = None,
    ) -> dict:
        """Send a new email, resolving contact name to email if needed."""
        import re

        if not message_body:
            return {"type": "text", "content": "Please provide a message. Example: `mail linda Hey, how are you?`"}

        # Determine if recipient is an email or a contact name
        to_email = None
        contact_name = None

        if "@" in recipient:
            # It's already an email address
            to_email = recipient
        else:
            # Require full email address since contacts feature is removed
            return {
                "type": "text",
                "content": f"Please provide a full email address. Example: `mail linda@example.com hello`",
            }

        # Use specified account or first configured account
        if from_account is None:
            from_account = accounts[0]

        # Use provided subject or generate from first part of message
        if subject:
            subject_text = subject
        else:
            # Auto-generate subject from first part of message (up to 50 chars or first sentence)
            subject_text = message_body[:50].split(".")[0].split("!")[0].split("?")[0]
            if len(subject_text) < len(message_body):
                subject_text = subject_text.strip() + "..."
            else:
                subject_text = subject_text.strip()

        success = send_email(from_account, to_email, subject_text, message_body, attachments=attachments)

        if success:
            attachment_note = f" with {len(attachments)} attachment(s)" if attachments else ""
            if contact_name:
                return {"type": "text", "content": f"✅ Email sent to **{contact_name}** ({to_email}){attachment_note}"}
            else:
                return {"type": "text", "content": f"✅ Email sent to {to_email}{attachment_note}"}
        else:
            return {"type": "text", "content": f"❌ Failed to send email to {to_email}"}

    # Cache for todo UIDs (for rm command)
    async def _todo_command(self, arg: str) -> dict:
        """Todo command - DISABLED (CalDAV removed)"""
        return {"type": "text", "content": "⚠️ The todo feature is temporarily unavailable."}

    async def _translate_command(self, arg: str, attachments: Optional[list] = None) -> dict:
        """Translate an uploaded image/PDF (OCR), or the last response, or an email."""
        if not self.user:
            return {"type": "text", "content": "Please log in to use translate."}

        # Uploaded image/PDF wins: OCR it and translate the whole thing.
        if attachments:
            return await self._translate_attachments(arg, attachments)

        # A URL → fetch the page's real text and translate it (reliable; no OCR).
        _url_match = re.search(r'https?://\S+', arg)
        if _url_match:
            return await self._translate_url(arg, _url_match.group(0).rstrip('.,)>'))

        parts = arg.strip().split()
        if not parts:
            return {
                "type": "text",
                "content": "Usage:\n- `translate <language>` - Translate last response\n- `translate email <language>` - Translate last email\n\nExamples: `translate spanish`, `translate email japanese`",
            }

        # Check if translating email
        if parts[0].lower() == "email":
            language = parts[1] if len(parts) > 1 else "English"
            # Get last email from conversation context
            # For now, suggest using mail translate command
            return {
                "type": "text",
                "content": f"To translate an email, use:\n`mail translate <account> <id> {language}`\n\nFirst check your mail with `mail` to get the email ID.",
            }

        def _unquote(s: str) -> str:
            s = (s or "").strip()
            if len(s) >= 2 and s[0] in "\"'“‘" and s[-1] in "\"'”’":
                return s[1:-1].strip()
            return s

        # Inline form `translate <text> [from <src>] to <lang>` (the documented syntax): translate
        # the GIVEN text, not the last response. Requires non-empty text and a target language, so
        # `translate spanish` / `translate to spanish` still fall through to last-response translation.
        # The target is 1-2 words after "to"; any trailing instruction ("... and explain") is dropped;
        # surrounding quotes on the text are stripped.
        _inline = re.match(
            r'^(.+?)(?:\s+from\s+([A-Za-z][A-Za-z\- ]*?))?\s+to\s+([A-Za-z][A-Za-z\- ]*?)(?:\s+and\s+.*)?$',
            arg.strip(), re.IGNORECASE)
        if _inline and _unquote(_inline.group(1)) and _inline.group(3).strip():
            _src = (_inline.group(2) or "").strip().title() or None
            return await self._translate_text(
                _unquote(_inline.group(1)), _inline.group(3).strip().title(), source=_src)

        # No `to <lang>`. If the whole arg is just a known language name, translate the LAST
        # response into it ("translate spanish"). Otherwise the arg is TEXT to translate to English
        # ("translate dame desuyo") — do NOT treat it as a language for the last response, which
        # mis-translated the previous command's output (e.g. a nyaa listing) instead of the words.
        _known_langs = {
            "english", "spanish", "french", "german", "italian", "portuguese", "dutch", "russian",
            "japanese", "chinese", "mandarin", "cantonese", "korean", "arabic", "hindi", "bengali",
            "punjabi", "urdu", "turkish", "vietnamese", "thai", "indonesian", "malay", "tagalog",
            "filipino", "polish", "ukrainian", "czech", "slovak", "romanian", "hungarian", "greek",
            "hebrew", "swedish", "norwegian", "danish", "finnish", "icelandic", "latin", "persian",
            "farsi", "swahili", "tamil", "telugu", "gujarati", "marathi", "serbian", "croatian",
            "bulgarian", "catalan", "esperanto", "welsh", "irish", "latvian", "lithuanian",
            "estonian", "slovenian", "albanian", "macedonian", "georgian", "armenian", "mongolian",
        }
        _norm = re.sub(r"^to\s+", "", _unquote(arg), flags=re.IGNORECASE).strip().lower()
        if _norm not in _known_langs:
            return await self._translate_text(_unquote(arg), "English")

        # Translate the last assistant response.
        language = self._parse_language(arg)
        from app.models import Conversation, Message
        conversation = (
            self.db.query(Conversation)
            .filter(Conversation.user_id == self.user.id)
            .order_by(Conversation.updated_at.desc())
            .first()
        )
        if not conversation:
            return {"type": "text", "content": "No conversation found to translate."}
        last_msg = (
            self.db.query(Message)
            .filter(Message.conversation_id == conversation.id, Message.role == "assistant")
            .order_by(Message.created_at.desc())
            .first()
        )
        if not last_msg or not last_msg.content:
            return {"type": "text", "content": "No previous response to translate."}
        return await self._translate_text(last_msg.content, language)

    @staticmethod
    def _parse_language(arg: str) -> str:
        """'spanish' / 'to spanish' / '' → 'Spanish' / 'Spanish' / 'English'."""
        lang = (arg or "").strip()
        if lang.lower().startswith("to "):
            lang = lang[3:].strip()
        return (lang or "English").title()

    async def _translate_text(self, text: str, language: str, *, kind: str = "text",
                              source: Optional[str] = None) -> dict:
        """Translate `text` into `language`, raising the output budget so long content
        isn't cut off. `kind` labels the prompt ('text' / 'web page text'); `source` is an optional
        known source language. Shared by the last-response, URL and attachment translate paths."""
        _from = f" from {source}" if source else ""
        messages = [
            {"role": "system", "content": (
                f"Translate the following {kind}{_from} to {language}. Translate ALL of it — every "
                "line and list item — do not summarize, omit, or stop early. Preserve the "
                "original line breaks and formatting. Output only the translation.")},
            {"role": "user", "content": (text or "")[:24000]},
        ]
        # Output is about as long as the input; the default ~2048 cap stops long pages early.
        _orig_np = self.chat_service.num_predict
        self.chat_service.num_predict = max(_orig_np, 8192)
        try:
            translation = await self.chat_service.chat(messages)
            return {"type": "text", "content": f"## Translation ({language})\n\n{translation}"}
        except Exception as e:
            logger.error(f"Translation error: {e}")
            return {"type": "text", "content": f"Translation failed: {str(e)}"}
        finally:
            self.chat_service.num_predict = _orig_np

    async def _translate_url(self, arg: str, url: str) -> dict:
        """Fetch a web page's text and translate the whole thing (no OCR).

        `translate <url>` (→ English) or `translate <url> to <language>`.
        """
        language = self._parse_language(arg.replace(url, ""))
        try:
            fetched = await self.search_service.fetch_urls([url], max_urls=1)
        except Exception as e:
            return {"type": "text", "content": f"Couldn't fetch {url}: {e}"}
        if not fetched or fetched[0].get("error") or not fetched[0].get("content"):
            err = (fetched[0].get("error") if fetched else None) or "no readable text found"
            return {"type": "text", "content": f"Couldn't fetch text from {url}: {err}"}
        title = fetched[0].get("title", "")
        body = (f"Title: {title}\n\n" if title else "") + fetched[0]["content"]
        return await self._translate_text(body, language, kind="web page text")

    async def _translate_attachments(self, arg: str, attachments: list) -> dict:
        """OCR uploaded image(s)/PDF(s) and translate the FULL extracted text.

        Shared by the web UI, Telegram and Matrix (`translate <lang>` + an upload).
        Returns an `error: 'no_text'` field when nothing could be extracted (e.g. a
        Telegram-compressed photo) so callers can show a tailored hint.
        """
        import base64 as _b64
        from app.services.document_service import extract_image_text, extract_pdf_text
        from app.services.media_service import is_image, is_pdf

        language = self._parse_language(arg)
        parts = []
        for fn, data, ct in attachments:
            try:
                b64 = _b64.b64encode(data).decode()
            except Exception:
                continue
            if is_pdf(fn, ct):
                parts.append(extract_pdf_text(b64) or "")
            elif is_image(fn, ct):
                parts.append(extract_image_text(b64) or "")
        src = "\n\n".join(p for p in parts if p).strip()
        if not src:
            return {"type": "text", "error": "no_text",
                    "content": "Couldn't extract any text to translate from the upload."}
        return await self._translate_text(src, language)

    async def _compress_command(self, attachments: Optional[list]) -> dict:
        """Compress attached image(s) or video(s) and return the smaller files."""
        if not attachments:
            return {
                "type": "text",
                "content": "Attach an image or video, then send `compress` to shrink it.",
            }
        import asyncio
        from app.services.media_service import compress_attachments

        # ffmpeg transcodes can block; run off the event loop.
        outputs, summary = await asyncio.to_thread(compress_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _clip_command(self, arg: str, attachments: Optional[list]) -> dict:
        """Trim an attached video to a [start, end] span: `clip <start> <end>`.

        Times accept seconds or M:SS / H:MM:SS. Telegram drives an interactive
        flow; the web UI and Matrix pass both times in the command argument.
        """
        from app.services.media_service import clip_attachment, parse_timecode, is_video

        if not attachments:
            return {
                "type": "text",
                "content": "Attach a video, then send `clip <start> <end>` — e.g. `clip 0:10 0:30`.",
            }
        if not any(is_video(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "No video attachment found to clip."}

        parts = (arg or "").split()
        if len(parts) < 2:
            return {
                "type": "text",
                "content": "Usage: `clip <start> <end>` — e.g. `clip 0:10 0:30` or `clip 90 120`.",
            }
        start = parse_timecode(parts[0])
        end = parse_timecode(parts[1])
        if start is None or end is None:
            return {
                "type": "text",
                "content": "Couldn't read those times. Use seconds or M:SS / H:MM:SS, e.g. `clip 0:10 1:30`.",
            }
        if end <= start:
            return {"type": "text", "content": "The end time must be after the start time."}

        import asyncio
        # ffmpeg clipping can block; run it off the event loop.
        outputs, summary = await asyncio.to_thread(clip_attachment, attachments, start, end)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _convert_command(self, arg: str, attachments: Optional[list]) -> dict:
        """Convert attached image(s) to a PDF, or a PDF to images."""
        if not attachments:
            return {
                "type": "text",
                "content": (
                    "Attach file(s) then send `convert`:\n"
                    "- image(s) → a single PDF\n"
                    "- a PDF → one PNG per page"
                ),
            }
        import asyncio
        from app.services.media_service import convert_attachments

        outputs, summary = await asyncio.to_thread(convert_attachments, attachments, arg)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _meme_command(self, arg: str, attachments: Optional[list]) -> dict:
        """Add outlined white meme text to an attached image: `meme <text>`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {
                "type": "text",
                "content": "Attach an image, then send `meme <text>` to caption it.",
            }
        if not (arg or "").strip():
            return {"type": "text", "content": "Usage: `meme <text>` — the caption to add."}

        import asyncio
        from app.services.effects_service import meme_attachments

        # Pillow text rendering is light, but keep it off the event loop for big images.
        outputs, summary = await asyncio.to_thread(meme_attachments, attachments, arg)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _dildo_command(self, attachments: Optional[list]) -> dict:
        """Scatter dildos all over an attached image: `dildo` (no text needed)."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {
                "type": "text",
                "content": "Attach an image, then send `dildo` to decorate it.",
            }

        import asyncio
        from app.services.effects_service import dildo_attachments

        # Pillow compositing is light, but keep it off the event loop for big images.
        outputs, summary = await asyncio.to_thread(dildo_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _poo_command(self, attachments: Optional[list]) -> dict:
        """Scatter poop all over an attached image: `poo` (no text needed)."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {
                "type": "text",
                "content": "Attach an image, then send `poo` to decorate it.",
            }

        import asyncio
        from app.services.effects_service import poo_attachments

        # Pillow compositing is light, but keep it off the event loop for big images.
        outputs, summary = await asyncio.to_thread(poo_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _cum_command(self, attachments: Optional[list]) -> dict:
        """Scatter cum all over an attached image: `cum` (no text needed)."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {
                "type": "text",
                "content": "Attach an image, then send `cum` to decorate it.",
            }

        import asyncio
        from app.services.effects_service import cum_attachments

        # Pillow compositing is light, but keep it off the event loop for big images.
        outputs, summary = await asyncio.to_thread(cum_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _blood_command(self, attachments: Optional[list]) -> dict:
        """Splatter blood all over an attached image: `blood` (no text needed)."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {
                "type": "text",
                "content": "Attach an image, then send `blood` to decorate it.",
            }

        import asyncio
        from app.services.effects_service import blood_attachments

        # Pillow compositing is light, but keep it off the event loop for big images.
        outputs, summary = await asyncio.to_thread(blood_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _bullethole_command(self, attachments: Optional[list]) -> dict:
        """Punch bullet holes all over an attached image: `bullethole` (no text)."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `bullethole`."}

        import asyncio
        from app.services.effects_service import bullethole_attachments

        outputs, summary = await asyncio.to_thread(bullethole_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _fire_command(self, attachments: Optional[list]) -> dict:
        """Set an attached image on fire: `fire` (no text needed)."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `fire`."}

        import asyncio
        from app.services.effects_service import fire_attachments

        outputs, summary = await asyncio.to_thread(fire_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _alive_command(self, arg: str, attachments: Optional[list]) -> dict:
        """Make an attached photo come alive with 3D parallax motion:
        `alive [subtle|normal|strong]` (default normal)."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach a photo, then send `alive [subtle|normal|strong]`."}

        import asyncio
        from app.services.parallax_service import alive_attachments

        outputs, summary = await asyncio.to_thread(alive_attachments, attachments, arg or "")
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _glow_command(self, arg: str, attachments: Optional[list]) -> dict:
        """Generic "make it stand out": with an attached image → breathing zoom + colour
        pop + a sweeping light (`glow`). With NO image but text → a glowing neon text-card
        post (`glow <text>`)."""
        from app.services.media_service import is_image
        import asyncio

        has_image = attachments and any(is_image(fn, ct) for fn, _, ct in attachments)
        if has_image:
            from app.services.effects_service import glow_attachments
            outputs, summary = await asyncio.to_thread(glow_attachments, attachments)
            if not outputs:
                return {"type": "text", "content": summary}
            return {"type": "files", "content": summary, "files": outputs}

        # No image: render the text as a glowing neon card (a "glowing text post").
        if (arg or "").strip():
            from app.services.effects_service import render_glow_text_card
            png = await asyncio.to_thread(render_glow_text_card, arg.strip())
            return {"type": "files", "content": "## ✨ Glow", "files": [
                {"filename": "glow.png", "data": png, "content_type": "image/png"},
            ]}
        return {"type": "text", "content": "Attach an image, or send `glow <text>` for a glowing text post."}

    async def _gay_command(self, attachments: Optional[list]) -> dict:
        """Stamp a big red GAY rubber stamp on an attached image: `gay`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `gay`."}

        import asyncio
        from app.services.effects_service import gay_attachments

        outputs, summary = await asyncio.to_thread(gay_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _blacked_command(self, attachments: Optional[list]) -> dict:
        """Slap the BLACKED logo on an attached image: `blacked`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `blacked`."}

        import asyncio
        from app.services.effects_service import blacked_attachments

        outputs, summary = await asyncio.to_thread(blacked_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _kosher_command(self, attachments: Optional[list]) -> dict:
        """Stamp a 100% KOSHER certification seal on an attached image: `kosher`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `kosher`."}

        import asyncio
        from app.services.effects_service import kosher_attachments

        outputs, summary = await asyncio.to_thread(kosher_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _barked_command(self, attachments: Optional[list]) -> dict:
        """Drop a smirking dog and #BARKED on an attached image: `barked`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `barked`."}

        import asyncio
        from app.services.effects_service import barked_attachments

        outputs, summary = await asyncio.to_thread(barked_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _hava_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a 6s MP4 set to Hava Nagila: `hava`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `hava`."}

        import asyncio
        from app.services.effects_service import hava_attachments

        outputs, summary = await asyncio.to_thread(hava_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _indian_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a 6s MP4 set to an Indian song: `indian`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `indian`."}

        import asyncio
        from app.services.effects_service import indian_attachments

        outputs, summary = await asyncio.to_thread(indian_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _yakety_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a 6s MP4 set to Yakety Sax: `yakety`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `yakety`."}

        import asyncio
        from app.services.effects_service import yakety_attachments

        outputs, summary = await asyncio.to_thread(yakety_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _yamete_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a 6s MP4 set to the yamete clip: `yamete`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `yamete`."}

        import asyncio
        from app.services.effects_service import yamete_attachments

        outputs, summary = await asyncio.to_thread(yamete_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _curb_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Curb Your Enthusiasm theme: `curb`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `curb`."}

        import asyncio
        from app.services.effects_service import curb_attachments

        outputs, summary = await asyncio.to_thread(curb_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _depressing_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a 10s MP4 set to a depressing track: `depressing`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `depressing`."}

        import asyncio
        from app.services.effects_service import depressing_attachments

        outputs, summary = await asyncio.to_thread(depressing_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _fahh_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the fahh clip: `fahh`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `fahh`."}

        import asyncio
        from app.services.effects_service import fahh_attachments

        outputs, summary = await asyncio.to_thread(fahh_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _helpme_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a 5s MP4 set to the helpme clip: `helpme`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `helpme`."}

        import asyncio
        from app.services.effects_service import helpme_attachments

        outputs, summary = await asyncio.to_thread(helpme_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _gong_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the gong clip: `gong`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `gong`."}

        import asyncio
        from app.services.effects_service import gong_attachments

        outputs, summary = await asyncio.to_thread(gong_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _fbi_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the FBI open up clip: `fbi`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `fbi`."}

        import asyncio
        from app.services.effects_service import fbi_attachments

        outputs, summary = await asyncio.to_thread(fbi_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _redeem_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the do not redeem clip: `redeem`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `redeem`."}

        import asyncio
        from app.services.effects_service import redeem_attachments

        outputs, summary = await asyncio.to_thread(redeem_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _gigity_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the giggity clip: `gigity`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `gigity`."}

        import asyncio
        from app.services.effects_service import gigity_attachments

        outputs, summary = await asyncio.to_thread(gigity_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _beavis_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the Beavis laugh: `beavis`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `beavis`."}

        import asyncio
        from app.services.effects_service import beavis_attachments

        outputs, summary = await asyncio.to_thread(beavis_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _smell_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the smell clip: `smell`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `smell`."}

        import asyncio
        from app.services.effects_service import smell_attachments

        outputs, summary = await asyncio.to_thread(smell_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _hood_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a 10s MP4 set to the hood clip: `hood`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `hood`."}

        import asyncio
        from app.services.effects_service import hood_attachments

        outputs, summary = await asyncio.to_thread(hood_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _akbar_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the akbar clip: `akbar`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `akbar`."}

        import asyncio
        from app.services.effects_service import akbar_attachments

        outputs, summary = await asyncio.to_thread(akbar_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _retard_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the retard-alert clip: `retard`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `retard`."}

        import asyncio
        from app.services.effects_service import retard_attachments

        outputs, summary = await asyncio.to_thread(retard_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _whoabuddy_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the whoa buddy clip: `whoabuddy`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `whoabuddy`."}

        import asyncio
        from app.services.effects_service import whoabuddy_attachments

        outputs, summary = await asyncio.to_thread(whoabuddy_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _feliz_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the feliz clip: `feliz`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `feliz`."}

        import asyncio
        from app.services.effects_service import feliz_attachments

        outputs, summary = await asyncio.to_thread(feliz_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _prayer_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into a short MP4 set to the prayer clip: `prayer`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `prayer`."}

        import asyncio
        from app.services.effects_service import prayer_attachments

        outputs, summary = await asyncio.to_thread(prayer_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _sopranos_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Sopranos theme clip: `sopranos`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `sopranos`."}

        import asyncio
        from app.services.effects_service import sopranos_attachments

        outputs, summary = await asyncio.to_thread(sopranos_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _cheers_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Cheers theme clip: `cheers`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `cheers`."}

        import asyncio
        from app.services.effects_service import cheers_attachments

        outputs, summary = await asyncio.to_thread(cheers_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _munsters_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Munsters theme clip: `munsters`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `munsters`."}

        import asyncio
        from app.services.effects_service import munsters_attachments

        outputs, summary = await asyncio.to_thread(munsters_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _happydays_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Happy Days theme clip: `happydays`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `happydays`."}

        import asyncio
        from app.services.effects_service import happydays_attachments

        outputs, summary = await asyncio.to_thread(happydays_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _dontwanttowait_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Dawson's Creek theme clip: `dontwanttowait`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `dontwanttowait`."}

        import asyncio
        from app.services.effects_service import dontwanttowait_attachments

        outputs, summary = await asyncio.to_thread(dontwanttowait_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _strangerthings_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Stranger Things theme clip: `strangerthings`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `strangerthings`."}

        import asyncio
        from app.services.effects_service import strangerthings_attachments

        outputs, summary = await asyncio.to_thread(strangerthings_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _adamsfamily_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Addams Family theme clip: `adamsfamily`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `adamsfamily`."}

        import asyncio
        from app.services.effects_service import adamsfamily_attachments

        outputs, summary = await asyncio.to_thread(adamsfamily_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _xmen_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the X-Men theme clip: `xmen`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `xmen`."}

        import asyncio
        from app.services.effects_service import xmen_attachments

        outputs, summary = await asyncio.to_thread(xmen_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _futurama_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Futurama theme clip: `futurama`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `futurama`."}

        import asyncio
        from app.services.effects_service import futurama_attachments

        outputs, summary = await asyncio.to_thread(futurama_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _charliesangles_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Charlie's Angels theme clip: `charliesangles`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `charliesangles`."}

        import asyncio
        from app.services.effects_service import charliesangles_attachments

        outputs, summary = await asyncio.to_thread(charliesangles_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _differentstroke_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Diff'rent Strokes theme clip: `differentstroke`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `differentstroke`."}

        import asyncio
        from app.services.effects_service import differentstroke_attachments

        outputs, summary = await asyncio.to_thread(differentstroke_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _seinfeld_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Seinfeld theme clip: `seinfeld`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `seinfeld`."}

        import asyncio
        from app.services.effects_service import seinfeld_attachments

        outputs, summary = await asyncio.to_thread(seinfeld_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _onepiece_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the One Piece theme clip: `onepiece`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `onepiece`."}

        import asyncio
        from app.services.effects_service import onepiece_attachments

        outputs, summary = await asyncio.to_thread(onepiece_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _overtaken_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the overtaken clip: `overtaken`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `overtaken`."}

        import asyncio
        from app.services.effects_service import overtaken_attachments

        outputs, summary = await asyncio.to_thread(overtaken_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _freebird_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Free Bird solo: `freebird`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `freebird`."}

        import asyncio
        from app.services.effects_service import freebird_attachments

        outputs, summary = await asyncio.to_thread(freebird_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _kanye_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Kanye clip: `kanye`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `kanye`."}

        import asyncio
        from app.services.effects_service import kanye_attachments

        outputs, summary = await asyncio.to_thread(kanye_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _darkness_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the darkness clip: `darkness`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `darkness`."}

        import asyncio
        from app.services.effects_service import darkness_attachments

        outputs, summary = await asyncio.to_thread(darkness_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _bike_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the bike clip: `bike`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `bike`."}

        import asyncio
        from app.services.effects_service import bike_attachments

        outputs, summary = await asyncio.to_thread(bike_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _jobs_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the they-took-our-jobs clip: `jobs`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `jobs`."}

        import asyncio
        from app.services.effects_service import jobs_attachments

        outputs, summary = await asyncio.to_thread(jobs_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _ree_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the REEEE clip: `ree`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `ree`."}

        import asyncio
        from app.services.effects_service import ree_attachments

        outputs, summary = await asyncio.to_thread(ree_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _liberal_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the liberal clip: `liberal`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `liberal`."}

        import asyncio
        from app.services.effects_service import liberal_attachments

        outputs, summary = await asyncio.to_thread(liberal_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _moving_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the moving clip: `moving`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `moving`."}

        import asyncio
        from app.services.effects_service import moving_attachments

        outputs, summary = await asyncio.to_thread(moving_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _harlem_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Harlem Shake clip: `harlem`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `harlem`."}

        import asyncio
        from app.services.effects_service import harlem_attachments

        outputs, summary = await asyncio.to_thread(harlem_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _chimp_command(self, attachments: Optional[list]) -> dict:
        """Overlay the animated chimp gif on the lower third of an image: `chimp`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `chimp`."}

        import asyncio
        from app.services.effects_service import chimp_attachments

        outputs, summary = await asyncio.to_thread(chimp_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _consider_command(self, attachments: Optional[list]) -> dict:
        """Overlay the 'consider the following' cutout on an attached image: `consider`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `consider`."}

        import asyncio
        from app.services.effects_service import consider_attachments

        outputs, summary = await asyncio.to_thread(consider_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _clay_command(self, attachments: Optional[list]) -> dict:
        """Overlay the background-removed Clay Davis clip on an image: `clay`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `clay`."}

        import asyncio
        from app.services.effects_service import clay_attachments

        outputs, summary = await asyncio.to_thread(clay_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _wasteland_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Teenage Wasteland intro: `wasteland`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `wasteland`."}

        import asyncio
        from app.services.effects_service import wasteland_attachments

        outputs, summary = await asyncio.to_thread(wasteland_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _mixalot_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the Baby Got Back clip: `mixalot`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `mixalot`."}

        import asyncio
        from app.services.effects_service import mixalot_attachments

        outputs, summary = await asyncio.to_thread(mixalot_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _thug_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the THUG LIFE clip: `thug`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `thug`."}

        import asyncio
        from app.services.effects_service import thug_attachments

        outputs, summary = await asyncio.to_thread(thug_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _feltedtables_command(self, attachments: Optional[list]) -> dict:
        """Turn an attached image into an MP4 set to the felted-tables clip: `feltedtables`."""
        from app.services.media_service import is_image

        if not attachments or not any(is_image(fn, ct) for fn, _, ct in attachments):
            return {"type": "text", "content": "Attach an image, then send `feltedtables`."}

        import asyncio
        from app.services.effects_service import feltedtables_attachments

        outputs, summary = await asyncio.to_thread(feltedtables_attachments, attachments)
        if not outputs:
            return {"type": "text", "content": summary}
        return {"type": "files", "content": summary, "files": outputs}

    async def _4chan_command(self, arg: str) -> dict:
        """Open 4chan catalog browser. Optional board: g, pol, a, or h."""
        allowed_boards = ("g", "pol", "a", "h")
        board = (arg or "g").strip().lower()
        if board not in allowed_boards:
            board = "g"
        return {
            "type": "4chan",
            "content": f"Opening 4chan /{board}/ catalog.",
            "board": board,
        }


def get_command_service(db: Session) -> CommandService:
    return CommandService(db)
