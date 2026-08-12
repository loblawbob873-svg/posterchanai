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
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

stats_scheduler = None
_DAYS = 30     # collect 30 days; the weekly panel is the last 7 of it (one Postgres pass)
_MONTHS = 6    # also bucket the last 6 calendar months for the month-by-month panel (same pass)

# Cyberpunk palette (matches the web client: --cyan / --neon-magenta on near-black). The 30-day
# panel uses a SECOND pair (green/amber) so all four series read distinctly.
_BG = (10, 7, 18)
_CYAN = (0, 240, 255)        # weekly posts
_MAGENTA = (255, 43, 214)    # weekly active users
_GREEN = (57, 255, 130)      # 30-day posts
_AMBER = (255, 210, 90)      # 30-day active users
_VIOLET = (170, 120, 255)    # month-by-month posts
_ORANGE = (255, 140, 60)     # month-by-month active users
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


def _collect_stats(days: int = _DAYS, months: int = _MONTHS) -> dict:
    """BLOCKING: query the relay's Postgres → daily posts + DAU (past `days`) AND monthly posts + MAU
    (past `months` calendar months) for nip05 users, all UTC, in ONE streaming pass.

    Daily buckets are UTC midnights ending today; month buckets are calendar months ending with the
    current month. Returns a dict the renderer consumes.
    """
    import bisect
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

        # Drop fedi→Nostr bridge puppets. They carry a nip05_name (so they'd otherwise count), but their
        # kind-1s are MIRRORED fediverse posts, not native Nostr activity — counting them inflates the
        # numbers misleadingly ("only here for now"). Same DB as `events` (posterchan_relay); the table
        # may be absent on a node that never bridged, so fall back to counting all nip05 users.
        try:
            cur.execute("SELECT pubkey_hex FROM fedi_puppets")
            nip05 -= {r[0] for r in cur.fetchall() if r[0]}
        except Exception:
            conn.rollback()   # a failed statement poisons the txn for the following named-cursor scan

        now = datetime.now(timezone.utc)
        # THE DAILY WINDOW ENDS YESTERDAY, and every panel is better for it. Including today put a
        # PARTIAL day at the end of every chart — measured mid-morning UTC it was 4.9k posts against a
        # 9k run rate — so the last bar always looked like the network had fallen off a cliff, the
        # "past 7 days" total was really six and a bit, and the trailing point dragged the line down
        # on all three panels. Complete UTC days only; the month panel still shows the current month,
        # where a partial bucket is obvious and expected.
        midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        last_day = midnight - timedelta(days=1)
        starts = [int((last_day - timedelta(days=days - 1 - i)).timestamp()) for i in range(days)]
        day_since = starts[0]
        day_until = int(midnight.timestamp())        # exclusive: today is not a day yet
        labels = [datetime.fromtimestamp(s, timezone.utc).strftime("%a") for s in starts]
        dates = [datetime.fromtimestamp(s, timezone.utc).strftime("%m/%d") for s in starts]

        # Calendar-month buckets: the last `months` months, oldest→newest, ending with this month.
        yms = []
        y, mo = now.year, now.month
        for _ in range(months):
            yms.append((y, mo))
            mo -= 1
            if mo == 0:
                mo, y = 12, y - 1
        yms.reverse()
        month_labels = [datetime(yy, mm, 1, tzinfo=timezone.utc).strftime("%b") for yy, mm in yms]
        # Per-month start timestamps (ascending) — a bisect maps each event to its month bucket without
        # building a datetime per row (the scan can be millions of rows on a busy relay).
        month_starts = [int(datetime(yy, mm, 1, tzinfo=timezone.utc).timestamp()) for yy, mm in yms]
        month_since = month_starts[0]

        posts = [0] * days
        actives = [set() for _ in range(days)]
        m_posts = [0] * months
        m_actives = [set() for _ in range(months)]
        # Scan the WHOLE window (the earlier of the day/month start) ONCE, via a SERVER-SIDE (named)
        # cursor so a busy relay's result STREAMS instead of buffering wholesale (bounded memory/CPU).
        # Uses the (kind, created_at) index; we filter to nip05 authors in Python.
        since = min(day_since, month_since)
        scan = conn.cursor(name="stats_k1_scan")
        scan.itersize = 5000
        scan.execute("SELECT created_at, pubkey FROM events WHERE kind=1 AND created_at >= %s", (since,))
        for ts, pk in scan:
            if pk not in nip05:
                continue
            if day_since <= ts < day_until:
                idx = int((ts - day_since) // 86400)       # UTC has no DST → each bucket is exactly 86400s
                if 0 <= idx < days:
                    posts[idx] += 1
                    actives[idx].add(pk)
            mi = bisect.bisect_right(month_starts, ts) - 1  # which calendar-month bucket
            if 0 <= mi < months:
                m_posts[mi] += 1
                m_actives[mi].add(pk)
        scan.close()

        dau = [len(s) for s in actives]
        mau = [len(s) for s in m_actives]

        def _union(sets):
            u = set()
            for s in sets:
                u |= s
            return len(u)

        # DAU/MAU are per-period DISTINCT, so a week/month "active users" total is the UNION of the
        # finer sets (not the sum of counts). Posts are simple sums.
        # A UNIQUE-USER COUNT OVER 7 DAYS AND ONE OVER 30 ARE NOT COMPARABLE, and printing them on
        # consecutive lines invites exactly that comparison ("active users looks way higher on 30
        # days" — of course it does: a longer window catches anyone who posted once). The average
        # DAY is the figure that means the same thing in both, so it goes out beside them.
        dau_week = [x for x in dau[-7:] if x]
        dau_all = [x for x in dau if x]
        return {
            "labels": labels, "dates": dates, "posts": posts, "dau": dau,
            "month_labels": month_labels, "posts_by_month": m_posts, "mau": mau,
            "posts_week": sum(posts[-7:]), "posts_month": sum(posts),
            "active_week": _union(actives[-7:]), "active_month": _union(actives),
            "dau_avg_week": int(round(sum(dau_week) / len(dau_week))) if dau_week else 0,
            "dau_avg_month": int(round(sum(dau_all) / len(dau_all))) if dau_all else 0,
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


def _nice_max(v: float) -> float:
    """The next round number at or above `v` — 1, 2, 2.5 or 5 × a power of ten.

    Axis ticks have to be numbers a reader can hold ("0 · 2.5k · 5k"), not the data's own maximum
    printed to one decimal. It matters more here than usual because this chart now has TWO scales and
    they share five gridlines: a tick that is not round on both is a tick nobody can read off.
    """
    v = max(float(v), 1.0)
    import math
    p = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * p:
            return m * p
    return 10 * p


def _kfmt(v) -> str:
    v = float(v)
    if v >= 1000:
        return f"{v / 1000:.1f}k".replace(".0k", "k")
    return str(int(round(v)))


def _draw_panel(base, reg, xlabels, posts, dau, post_color, dau_color, post_mode, title, fonts):
    """Draw one chart into region `reg`=(x0,y0,x1,y1). post_mode 'bars' or 'lines'.

    ONE SCALE, ON THE LEFT, FOR BOTH SERIES — so the line sits where its numbers put it, about a sixth
    of the way up, because there are about a sixth as many people as posts.

    It was normalised to its OWN maximum at 92% of the panel height while the only axis drawn was the
    posts one, so ~1,400 people read as ~9k and the same measurement appeared at three different
    heights on three panels. A second axis would fix the reading and invent a different lie: two
    series that share an axis can be compared, two that do not cannot.

    Glow is composited per-panel — the glow layer is black outside this panel's shapes, so screen()
    only blooms here."""
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
    # ONE maximum, covering both series, rounded so the ticks are numbers a reader can hold.
    smax = _nice_max(max(max(posts), max(dau)))
    for g in range(5):
        y = B - ph * g / 4
        d.line([(L, y), (R, y)], fill=_GRID)
        d.text((L - 10, y - 8), _kfmt(smax * g / 4), font=f_sm, fill=_MUTED, anchor="ra")
    slot = pw / n
    xs = [L + slot * i + slot / 2 for i in range(n)]
    post_pts = [(xs[i], B - ph * posts[i] / smax) for i in range(n)]
    dau_pts = [(xs[i], B - ph * dau[i] / smax) for i in range(n)]
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
    # Both series are labelled where there is room. On a shared scale the line sits well below the bar
    # tops, so the labels no longer collide the way they did when it was blown up to its own scale.
    if n <= 10:
        for i in range(n):
            if posts[i]:
                if post_mode == "bars":
                    # INSIDE the bar top (dark) so it cannot collide with the line — unless the bar is
                    # too short to hold it, which on a panel spanning 7k to 323k is most of them: the
                    # label then landed on the axis and read as part of it.
                    if B - post_pts[i][1] >= 22:
                        d.text((xs[i], post_pts[i][1] + 5), _kfmt(posts[i]), font=f_sm, fill=_BG, anchor="ma")
                    else:
                        d.text((xs[i], post_pts[i][1] - 7), _kfmt(posts[i]), font=f_sm, fill=post_color,
                               anchor="mb", stroke_width=2, stroke_fill=_BG)
                else:
                    d.text((xs[i], post_pts[i][1] - 7), _kfmt(posts[i]), font=f_sm, fill=post_color,
                           anchor="mb", stroke_width=2, stroke_fill=_BG)
            d.text((dau_pts[i][0], dau_pts[i][1] - 8), _kfmt(dau[i]), font=f_sm, fill=dau_color,
                   anchor="mb", stroke_width=2, stroke_fill=_BG)


def _render_chart(stats: dict, title: str) -> bytes:
    """BLOCKING: three-panel cyberpunk chart — weekly (cyan bars + magenta line), 30-day (green/amber
    dual line), and month-by-month (violet bars + orange line) → PNG bytes."""
    from PIL import Image, ImageDraw

    W, H = 1000, 1460
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
    m_labels = stats.get("month_labels") or []
    if m_labels:
        _draw_panel(base, (70, 1090, W - 30, 1410), m_labels,
                    stats.get("posts_by_month") or [], stats.get("mau") or [],
                    _VIOLET, _ORANGE, "bars", f"Last {len(m_labels)} months", fonts)

    d = ImageDraw.Draw(base)
    foot = (f"One scale: posts and active users are both read off the left axis · kind-1 notes, "
            f"complete UTC days · NIP-05 profiles only · {stats['nip05_total']:,} known")
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
    # The per-day average is the figure that means the same thing in both windows — a distinct-user
    # count over 30 days is bigger than one over 7 for no reason but the length of the window.
    return (
        f"📊 Nostr activity on {name}\n\n"
        f"Past 7 days: {stats['posts_week']:,} posts · {stats['active_week']:,} people "
        f"(~{stats.get('dau_avg_week', 0):,}/day)\n"
        f"Past 30 days: {stats['posts_month']:,} posts · {stats['active_month']:,} people "
        f"(~{stats.get('dau_avg_month', 0):,}/day)\n\n"
        f"Complete UTC days · kind-1 notes · profiles with a NIP-05 · this relay's view of {src}\n"
        f"{stats['nip05_total']:,} NIP-05 users known\n\n"
        f"#nostr #grownostr #nostrstats"
    )


async def build_stats():
    """Collect + render (blocking parts off-thread). Returns (summary_text, png_bytes). Used by the
    Nostr-only Preview (display the chart, no posting) and by post_stats."""
    stats = await asyncio.to_thread(_collect_stats, _DAYS)
    png = await asyncio.to_thread(_render_chart, stats, f"Nostr activity · {_instance_name()}")
    return _summary_text(stats), png


async def post_stats(nsec: str):
    """Build the chart and POST it as a kind-1 note from the given bot account to the LOCAL relay
    only (its outbox federates upstream). Returns the summary; raises ValueError on a bad nsec. The
    note keeps the #nostr #grownostr #nostrstats tags + the 'how it's counted' line (see _summary_text)."""
    from app.services.nostr import nostr_service
    seckey = nostr_service.decode_seckey(nsec)        # raises on a malformed key
    summary, png = await build_stats()
    await nostr_service.post_note(seckey, ["ws://127.0.0.1:3052"], summary,
                                  media_list=[(png, "image/png")],
                                  media_cfg={"service": "blossom", "endpoint": ""})
    logger.info("[stats-bot] posted stats note")
    return summary


def _stats_bots() -> list:
    """Enabled Nostr bots on THIS host whose config has stats_enabled. Returns [(name, nsec), …].
    The stats feature is a CONFIG flag (not a main.py mode — argparse would reject an unknown flag),
    so the botframework subprocess never sees it; the app posts on the bot's behalf with its nsec."""
    import socket
    from app.database import SessionLocal
    from app.models import Bot
    host = socket.gethostname().split(".")[0]
    out = []
    db = SessionLocal()
    try:
        for b in db.query(Bot).filter(Bot.enabled == True, Bot.platform == "nostr").all():  # noqa: E712
            if b.host and b.host.split(".")[0] != host:     # empty host = runs on any node
                continue
            try:
                cfg = json.loads(b.config or "{}") or {}
            except Exception:
                cfg = {}
            if cfg.get("stats_enabled") and cfg.get("nostr_nsec"):
                out.append((b.name, cfg["nostr_nsec"]))
    finally:
        try:
            db.close()
        except Exception:
            pass
    return out


# --- scheduler -----------------------------------------------------------

async def _tick():
    """Every 6 hours: post for each enabled Nostr bot that has the stats feature on (gated by the bots
    master switch, like every other bot). A node with no stats bot is a cheap no-op."""
    from app.services import settings_store
    if not settings_store.get_bool("bots_manager_enabled", False):
        return
    for name, nsec in _stats_bots():
        try:
            await post_stats(nsec)
            logger.info("[stats-bot] posted for bot %s", name)
        except Exception as e:
            logger.warning("[stats-bot] post failed for bot %s: %s", name, e)


def start_stats_bot_scheduler():
    # Register a job that posts EVERY 6 HOURS on fixed UTC wall-clock hours, anchored on
    # `stats_bot_post_hour` (UTC 0–23, default 16): the four slots {h, h+6, h+12, h+18} mod 24, so
    # the default posts at 04:00/10:00/16:00/22:00 UTC. It self-gates on the bots master switch +
    # which bots have the stats feature (read live each tick), so enabling it on a bot needs no
    # restart of this scheduler. A wall-clock CRON (not an interval) means a restart does NOT fire an
    # immediate/extra post — cron computes the next matching H:00, it never fires on registration.
    # UTC matches the chart's UTC-midnight buckets. coalesce + misfire grace let a worker that was
    # briefly down near a slot still post that slot, just once.
    global stats_scheduler
    if stats_scheduler is not None:
        return
    from app.services import settings_store
    hour = settings_store.get_int("stats_bot_post_hour", 16)
    if not (0 <= hour <= 23):
        hour = 16
    hours = ",".join(str((hour + 6 * k) % 24) for k in range(4))
    stats_scheduler = AsyncIOScheduler()
    stats_scheduler.add_job(_tick, CronTrigger(hour=hours, minute=0, timezone="UTC"),
                            id="stats_bot", name="Nostr Stats Bot", replace_existing=True,
                            coalesce=True, misfire_grace_time=3600)
    stats_scheduler.start()
    logger.info("[stats-bot] scheduler started (every 6h at UTC hours %s)", hours)


def stop_stats_bot_scheduler():
    global stats_scheduler
    if stats_scheduler is not None:
        try:
            stats_scheduler.shutdown()
        except Exception:
            pass
        stats_scheduler = None
        logger.info("[stats-bot] scheduler stopped")
