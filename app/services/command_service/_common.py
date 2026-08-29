"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
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
from app.services import music_factory
from app.services.music_service import MusicError
from app.services import video_factory
from app.services.video_service import VideoError
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
    download_ytdl_bytes,
    extract_download_urls,
    extract_youtube_urls,
    format_download_result,
    is_youtube_url,
    summarize_youtube,
)
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
_screenshot_lock = threading.Lock()
def _screenshot_settle() -> float:
    import os
    try:
        return max(0.0, float(os.environ.get("SCREENSHOT_SETTLE_SECONDS", "5")))
    except ValueError:
        return 5.0
def _url_is_safe_to_fetch(url: str, allowed_hosts: Optional[list] = None) -> bool:
    """SSRF guard for the screenshot command (reachable by untrusted Pleroma
    users, not just trusted web users).

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
        # coincide with the social poller's card renders (same path). Unbounded
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
    even when the source page serves nothing to a link-preview crawler. The media (if
    any) is passed pre-fetched as a data: URI so the render needs no network and is
    deterministic. Returns PNG bytes; raises if no browser is installed.
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
