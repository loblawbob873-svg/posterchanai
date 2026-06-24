"""Nostr Stats Bot — optional, posts a cyberpunk activity graph every 6 hours.

Reads the built-in relay's Postgres directly (read-only) for two metrics over the past 7 days,
counting ONLY pubkeys whose latest kind-0 profile carries a non-empty `nip05`:
  - daily kind-1 POSTS by nip05 users
  - daily ACTIVE nip05 users (distinct authors who posted that day)
Renders a neon/cyberpunk chart with Pillow (already a dependency — no new packages) and either
PREVIEWS it to the admin's Telegram or POSTS it as a kind-1 note from the configured stats account
(`stats_bot_nsec`). Gated on `stats_bot_enabled` (default off). Mirrors logs_scheduler's shape:
one shared entry point (`run_stats_bot`) used by the 6h cron, the admin "Preview now" button, and a
command; `preview_only=True` forces the Telegram preview regardless of the configured mode.
"""

import os
import io
import json
import base64
import logging
import asyncio
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

stats_scheduler = None
_DAYS = 30   # collect 30 days; the weekly panel is the last 7 of it (one Postgres pass)

# Cyberpunk palette (matches the web client: --cyan / --neon-magenta on near-black). The 30-day
# panel uses a SECOND pair (green/amber) so all four series read distinctly.
_BG = (10, 7, 18)
_CYAN = (0, 240, 255)        # weekly posts
_MAGENTA = (255, 43, 214)    # weekly active users
_GREEN = (57, 255, 130)      # monthly posts
_AMBER = (255, 210, 90)      # monthly active users
_GRID = (40, 30, 60)
_TEXT = (200, 190, 220)
_MUTED = (120, 110, 150)

_FONT_PATHS = [
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
]


# --- data ----------------------------------------------------------------

def _relay_dsn() -> str:
    from app.services import settings_store
    return (settings_store.get("nostr_relay_pg_dsn", "") or
            os.environ.get("NOSTR_RELAY_PG_DSN",
                           "host=127.0.0.1 port=5432 dbname=posterchan_relay user=posterchan"))


def _collect_stats(days: int = _DAYS) -> dict:
    """BLOCKING: query the relay's Postgres → daily posts + DAU for nip05 users (past `days`, UTC).

    Buckets are UTC midnights, oldest→newest, ending today. Returns a dict the renderer consumes.
    """
    import psycopg2
    conn = psycopg2.connect(_relay_dsn(), connect_timeout=10)
    try:
        cur = conn.cursor()
        # nip05 set: the relay keeps only the newest kind-0 per pubkey (replaceable), so one row each.
        cur.execute("SELECT pubkey, content FROM events WHERE kind=0")
        nip05 = set()
        for pk, content in cur.fetchall():
            try:
                m = json.loads(content or "{}")
                if isinstance(m, dict) and str(m.get("nip05", "") or "").strip():
                    nip05.add(pk)
            except Exception:
                pass

        now = datetime.now(timezone.utc)
        midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        starts = [int((midnight - timedelta(days=days - 1 - i)).timestamp()) for i in range(days)]
        since = starts[0]
        labels = [datetime.fromtimestamp(s, timezone.utc).strftime("%a") for s in starts]
        dates = [datetime.fromtimestamp(s, timezone.utc).strftime("%m/%d") for s in starts]

        posts = [0] * days
        actives = [set() for _ in range(days)]
        # Only (created_at, pubkey), and via a SERVER-SIDE (named) cursor so a busy relay's 7-day
        # kind-1 result STREAMS instead of buffering wholesale in the client (bounded memory/CPU).
        # Uses the (kind, created_at) index; we filter to nip05 authors in Python.
        scan = conn.cursor(name="stats_k1_scan")
        scan.itersize = 5000
        scan.execute("SELECT created_at, pubkey FROM events WHERE kind=1 AND created_at >= %s", (since,))
        for ts, pk in scan:
            if pk not in nip05:
                continue
            idx = int((ts - since) // 86400)              # UTC has no DST → each bucket is exactly 86400s
            if idx < 0 or idx >= days:
                continue
            posts[idx] += 1
            actives[idx].add(pk)
        scan.close()

        dau = [len(s) for s in actives]

        def _union(sets):
            u = set()
            for s in sets:
                u |= s
            return len(u)

        # DAU is per-day DISTINCT, so a week/month "active users" total is the UNION of daily sets
        # (not the sum of daily counts). Posts are simple sums.
        return {
            "labels": labels, "dates": dates, "posts": posts, "dau": dau,
            "posts_week": sum(posts[-7:]), "posts_month": sum(posts),
            "active_week": _union(actives[-7:]), "active_month": _union(actives),
            "nip05_total": len(nip05),
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


# --- chart ---------------------------------------------------------------

def _font(sz: int):
    from PIL import ImageFont
    for p in _FONT_PATHS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                pass
    try:
        return ImageFont.load_default(sz)
    except TypeError:
        return ImageFont.load_default()


def _kfmt(v) -> str:
    v = float(v)
    if v >= 1000:
        return f"{v / 1000:.1f}k".replace(".0k", "k")
    return str(int(round(v)))


def _draw_panel(base, reg, xlabels, posts, dau, post_color, dau_color, post_mode, title, fonts):
    """Draw one chart into region `reg`=(x0,y0,x1,y1). post_mode 'bars' or 'lines'. Posts use the
    left scale; active users get their own (~92% height). Glow is composited per-panel — the glow
    layer is black outside this panel's shapes, so screen() only blooms here."""
    from PIL import Image, ImageDraw, ImageFilter, ImageChops
    f_lbl, f_sm, f_hdr = fonts
    x0, y0, x1, y1 = reg
    d = ImageDraw.Draw(base)
    d.text((x0, y0 - 30), title, font=f_hdr, fill=_TEXT, anchor="la")
    # per-panel mini legend (top-right)
    lx = x1 - 220
    if post_mode == "bars":
        d.rectangle([lx, y0 - 26, lx + 16, y0 - 12], fill=post_color)
    else:
        d.line([(lx, y0 - 19), (lx + 16, y0 - 19)], fill=post_color, width=3)
    d.text((lx + 22, y0 - 27), "Posts", font=f_sm, fill=_TEXT, anchor="la")
    d.line([(lx + 95, y0 - 19), (lx + 111, y0 - 19)], fill=dau_color, width=3)
    d.text((lx + 117, y0 - 27), "Active users", font=f_sm, fill=_TEXT, anchor="la")

    L, R, T, B = x0 + 52, x1, y0 + 6, y1 - 40
    pw, ph = R - L, B - T
    n = len(posts)
    pmax = max(max(posts), 1)
    dmax = max(max(dau), 1)
    for g in range(5):                       # grid + posts y-ticks
        y = B - ph * g / 4
        d.line([(L, y), (R, y)], fill=_GRID)
        d.text((L - 10, y - 8), _kfmt(pmax * g / 4), font=f_sm, fill=_MUTED, anchor="ra")
    slot = pw / n
    xs = [L + slot * i + slot / 2 for i in range(n)]
    post_pts = [(xs[i], B - ph * posts[i] / pmax) for i in range(n)]
    dau_pts = [(xs[i], B - ph * (dau[i] / dmax) * 0.92) for i in range(n)]
    bw = slot * 0.5
    bars = [(xs[i] - bw / 2, post_pts[i][1], xs[i] + bw / 2, B) for i in range(n)]

    glow = Image.new("RGB", base.size, (0, 0, 0))      # local glow layer
    gd = ImageDraw.Draw(glow)
    if post_mode == "bars":
        for b in bars:
            gd.rectangle(b, fill=post_color)
    elif n > 1:
        gd.line(post_pts, fill=post_color, width=5)
    if n > 1:
        gd.line(dau_pts, fill=dau_color, width=5)
    for p in dau_pts:
        gd.ellipse([p[0] - 5, p[1] - 5, p[0] + 5, p[1] + 5], fill=dau_color)
    base.paste(ImageChops.screen(base, glow.filter(ImageFilter.GaussianBlur(6))))
    d = ImageDraw.Draw(base)

    if post_mode == "bars":                  # crisp shapes over the glow
        for b in bars:
            d.rectangle(b, fill=post_color)
    elif n > 1:
        d.line(post_pts, fill=post_color, width=3)
        for p in post_pts:
            d.ellipse([p[0] - 3, p[1] - 3, p[0] + 3, p[1] + 3], fill=post_color)
    if n > 1:
        d.line(dau_pts, fill=dau_color, width=3)
    for p in dau_pts:
        d.ellipse([p[0] - 4, p[1] - 4, p[0] + 4, p[1] + 4], fill=dau_color)

    step = 1 if n <= 10 else max(1, n // 6)  # x labels (sparse when many) + value labels (few only)
    for i in range(n):
        if i % step == 0 or i == n - 1:
            d.text((xs[i], B + 8), xlabels[i], font=f_sm, fill=_MUTED, anchor="ma")
    if n <= 10:
        for i in range(n):
            if posts[i]:
                if post_mode == "bars":   # INSIDE the bar top (dark) so it can't collide with the line above
                    d.text((xs[i], post_pts[i][1] + 5), _kfmt(posts[i]), font=f_sm, fill=_BG, anchor="ma")
                else:
                    d.text((xs[i], post_pts[i][1] - 7), _kfmt(posts[i]), font=f_sm, fill=post_color,
                           anchor="mb", stroke_width=2, stroke_fill=_BG)
            d.text((dau_pts[i][0], dau_pts[i][1] - 8), _kfmt(dau[i]), font=f_sm, fill=dau_color,
                   anchor="mb", stroke_width=2, stroke_fill=_BG)


def _render_chart(stats: dict, title: str) -> bytes:
    """BLOCKING: two-panel cyberpunk chart — weekly (cyan bars + magenta line) above monthly
    (green/amber dual line) → PNG bytes."""
    from PIL import Image, ImageDraw

    W, H = 1000, 1020
    base = Image.new("RGB", (W, H), _BG)
    sl = Image.new("RGB", (W, H), _BG)
    sld = ImageDraw.Draw(sl)
    for y in range(0, H, 3):
        sld.line([(0, y), (W, y)], fill=(16, 12, 28))
    base = Image.blend(base, sl, 0.5)

    f_title, f_hdr, f_lbl, f_sm = _font(30), _font(20), _font(17), _font(14)
    fonts = (f_lbl, f_sm, f_hdr)
    d = ImageDraw.Draw(base)
    d.text((60, 22), title, font=f_title, fill=_CYAN, anchor="la")

    dates, labels = stats["dates"], stats["labels"]
    posts, dau = stats["posts"], stats["dau"]
    _draw_panel(base, (70, 150, W - 30, 470), labels[-7:], posts[-7:], dau[-7:],
                _CYAN, _MAGENTA, "bars", "Last 7 days", fonts)
    _draw_panel(base, (70, 620, W - 30, 940), dates, posts, dau,
                _GREEN, _AMBER, "lines", "Last 30 days", fonts)

    d = ImageDraw.Draw(base)
    foot = (f"Posts = kind-1 notes by NIP-05 users · Active = distinct NIP-05 authors/day "
            f"· {stats['nip05_total']} NIP-05 users known")
    d.text((70, H - 38), foot, font=f_sm, fill=_MUTED, anchor="la")

    out = io.BytesIO()
    base.save(out, "PNG")
    return out.getvalue()


# --- output --------------------------------------------------------------

def _instance_name() -> str:
    from app.services import settings_store
    return (settings_store.get("site_name", "") or settings_store.get("nostr_relay_name", "")
            or "this relay").strip() or "this relay"


def _relay_count() -> int:
    """How many relays this relay federates with (its data sources) — the upstream relay list."""
    from app.services import settings_store
    raw = settings_store.get("nostr_relay_upstream_relays", "") or ""
    n = len([x for x in raw.replace(",", "\n").splitlines() if x.strip().lower().startswith("ws")])
    if not n:
        try:
            from app.services.nostr import nostr_service
            n = len(nostr_service.DEFAULT_RELAYS)
        except Exception:
            n = 0
    return n


def _summary_text(stats: dict) -> str:
    name = _instance_name()
    relays = _relay_count()
    src = f"{relays} relays" if relays else "its connected relays"
    return (
        f"📊 Nostr activity on {name}\n\n"
        f"Past 7 days:\n"
        f"• {stats['posts_week']:,} posts · {stats['active_week']:,} active users\n"
        f"Past 30 days:\n"
        f"• {stats['posts_month']:,} posts · {stats['active_month']:,} active users\n\n"
        f"How it's counted: only NIP-05–verified users, aggregated from this relay's view of {src}. "
        f"Posts = kind-1 notes; Active = distinct authors who posted that period.\n\n"
        f"{stats['nip05_total']:,} NIP-05 users known\n\n"
        f"#nostr #grownostr #nostrstats"
    )


async def _preview_telegram(summary: str, png: bytes) -> None:
    """Send the chart to the admin's Telegram (no public post)."""
    from app.database import SessionLocal
    from app.models import User
    from app.services import settings_store
    from app.services.telegram_service import telegram_service
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.id == 1).first()
        if not admin or not admin.telegram_enabled or not admin.telegram_chat_id:
            logger.info("[stats-bot] preview requested but admin has no Telegram linked")
            return
        token = settings_store.get("telegram_bot_token", "")
        if token:
            telegram_service.set_token(token)
        await telegram_service.send_photo(admin.telegram_chat_id, base64.b64encode(png).decode(),
                                          caption=summary, parse_mode="")
    finally:
        try:
            db.close()
        except Exception:
            pass


async def _post_to_nostr(summary: str, png: bytes) -> None:
    """Publish the chart as a kind-1 note from the stats account to the local relay + the network."""
    from app.services import settings_store
    from app.services.nostr import nostr_service
    nsec = settings_store.get("stats_bot_nsec", "")
    if not nsec:
        logger.warning("[stats-bot] mode=post but stats_bot_nsec is unset — skipping public post")
        return
    seckey = nostr_service.decode_seckey(nsec)
    # Bots publish to the LOCAL relay ONLY — it stores the note and its outbox federates it to the
    # upstream relays. (Posting directly to the public relays too would be redundant + slower.)
    relays = ["ws://127.0.0.1:3052"]
    media_cfg = {"service": settings_store.get("stats_bot_media_service", "") or "blossom",
                 "endpoint": settings_store.get("stats_bot_media_endpoint", "") or ""}
    await nostr_service.post_note(seckey, relays, summary,
                                  media_list=[(png, "image/png")], media_cfg=media_cfg)
    logger.info("[stats-bot] posted weekly stats note")


async def run_stats_bot(preview_only: bool = False, force: bool = False):
    """Build the chart and deliver it (preview to Telegram, or public post — per stats_bot_mode).

    The 6h cron and the enabled-check call this plainly. The admin "Preview now" button passes
    preview_only=True (always a Telegram preview, never a public post). `force` bypasses the enabled
    gate (manual runs). Returns the summary text, or None if it didn't run.
    """
    from app.services import settings_store
    if not force and not preview_only and not settings_store.get_bool("stats_bot_enabled", False):
        return None
    try:
        stats = await asyncio.to_thread(_collect_stats, _DAYS)
        png = await asyncio.to_thread(_render_chart, stats, f"Nostr activity · {_instance_name()}")
    except Exception as e:
        logger.warning("[stats-bot] failed to build stats: %s", e)
        return None
    summary = _summary_text(stats)
    mode = (settings_store.get("stats_bot_mode", "preview") or "preview").strip().lower()
    try:
        if preview_only or mode != "post":
            await _preview_telegram(summary, png)
        else:
            await _post_to_nostr(summary, png)
    except Exception as e:
        logger.warning("[stats-bot] delivery failed: %s", e)
    return summary


# --- scheduler -----------------------------------------------------------

async def _tick():
    from app.services import settings_store
    if not settings_store.get_bool("stats_bot_enabled", False):
        return
    await run_stats_bot()


def start_stats_bot_scheduler():
    global stats_scheduler
    if stats_scheduler is not None:
        return
    from app.services import settings_store
    if not settings_store.get_bool("stats_bot_enabled", False):
        logger.info("[stats-bot] disabled")
        return
    stats_scheduler = AsyncIOScheduler()
    stats_scheduler.add_job(_tick, IntervalTrigger(hours=6), id="stats_bot",
                            name="Nostr Stats Bot", replace_existing=True)
    stats_scheduler.start()
    logger.info("[stats-bot] scheduler started (every 6h)")


def stop_stats_bot_scheduler():
    global stats_scheduler
    if stats_scheduler is not None:
        try:
            stats_scheduler.shutdown()
        except Exception:
            pass
        stats_scheduler = None
        logger.info("[stats-bot] scheduler stopped")
