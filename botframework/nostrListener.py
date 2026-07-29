"""Nostr mention listener — replies to kind-1 notes that p-tag the bot.

Structurally mirrors pleromaListener (recent-mention window + persistent dedup +
shared command dispatch), but Nostr-shaped: a note that tags the bot's pubkey IS a
mention (no @handle parsing needed), media is referenced by URL, posts are public.
DMs (NIP-04/17) are intentionally out of scope for v1.
"""

import os
import re
import sys
import time
import fcntl
import hashlib
import tempfile
from datetime import datetime, timedelta, timezone

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

from config import NOSTR_NSEC, BOT_BLACKLIST, BOT_NOSTR_PUBKEYS
from bot_commands import MEDIA_COMMANDS, NO_CAPTION_COMMANDS, BOT_HELP_TEXT
from rate_limit import SlidingWindowLimiter
from ai import generate_reply, is_ai_configured
import nostr as _nk
from searxng import smart_search, summarize_search_results, search_and_download_images
from news import fetch_news_from_source
from core.utils import contains_bad_words
from posterchanai_api import process_media, capture_screenshot, fetch_ytdl_media, parse_ytdl_postaction

get_mentions = _nk.get_mentions
get_note = _nk.get_note
get_own_account = _nk.get_own_account
send_reply = _nk.send_reply
get_thread_history = _nk.get_thread_history
has_own_reply = _nk.has_own_reply
download_image_from_url = _nk.download_image_from_url

# nostr: tokens in content (nostr:npub1…, nostr:nprofile…, nostr:note…) — stripped from prompts.
# Strip mention/entity tokens from the prompt: `nostr:<…>` URIs AND bare bech32 entities
# (npub/nprofile/nevent/note/naddr) so a bare-npub mention doesn't push the command word off
# the front (e.g. "npub1bot… cum" must reduce to "cum", or the command isn't recognized).
_NOSTR_TOKEN_RE = re.compile(
    r"nostr:[a-z0-9]+|\b(?:npub1|nprofile1|nevent1|note1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]+",
    re.IGNORECASE,
)
_YTDL_COOLDOWN_SECONDS = 30
_ytdl_last_request: dict = {}

# Abuse guard: Nostr is permissionless (anyone on any relay can mention the bot), so cap
# requests per sender pubkey and globally. Tunable per-bot (Admin → Bots) via the
# NOSTR_RATE_* env the manager injects. 0 disables a dimension. Exempt list = npub/hex
# pubkeys never limited (e.g. the operator).
_RATE_PER_USER = int(os.getenv("NOSTR_RATE_PER_USER", "5"))      # per window per pubkey
_RATE_GLOBAL = int(os.getenv("NOSTR_RATE_GLOBAL", "30"))         # per window, all senders
_RATE_WINDOW = int(os.getenv("NOSTR_RATE_WINDOW", "300"))        # seconds


def _rate_exempt() -> set:
    out = set()
    for tok in (os.getenv("NOSTR_RATE_EXEMPT", "") or "").replace(",", "\n").split():
        try:
            hexpk = _nk._svc.to_pubkey_hex(tok.strip())
            if hexpk:
                out.add(hexpk)
        except Exception:
            pass
    return out


_rl = SlidingWindowLimiter(_RATE_PER_USER, _RATE_GLOBAL, _RATE_WINDOW, _rate_exempt())

# ---- random-reply: occasionally strike up a thread with a NIP-05-verified stranger on the firehose
# timeline. OFF unless NOSTR_RANDOM_REPLY is enabled (per-bot, Admin → Bots). Built to be CHEAP: a
# random gate drops almost every note BEFORE any profile fetch / NIP-05 verify / LLM call, and a
# global "starts per hour" cap bounds LLM work to a few replies an hour no matter how busy the feed.
_RR_ENABLED = (os.getenv("NOSTR_RANDOM_REPLY", "") or "").strip().lower() in ("1", "true", "yes", "on")
_RR_PER_HOUR = int(os.getenv("NOSTR_RANDOM_REPLY_PER_HOUR", "3"))      # max NEW threads started / hour
_RR_PROB = float(os.getenv("NOSTR_RANDOM_REPLY_PROB", "0.03"))         # chance per eligible note (low)
_RR_MAX_THREAD = int(os.getenv("NOSTR_RANDOM_REPLY_MAX_THREAD", "2"))  # max bot replies per started thread
_RR_QUIET = (os.getenv("NOSTR_RANDOM_REPLY_QUIET", "") or "").strip()  # "HH-HH" 24h local; blank = never
_RR_WINDOW = 3600
_rr_starts = SlidingWindowLimiter(0, _RR_PER_HOUR, _RR_WINDOW)   # global-only: N new threads/hour
_rr_threads: dict = {}    # thread-root id -> bot reply count (random-reply-initiated threads only)
_rr_seen: set = set()     # note ids already considered (bounded)
# Restart guard — see the cutoff in the mention loop. BOT_STARTUP_CATCHUP_SECS controls how much
# pre-start history a freshly spawned listener will still answer (default 2 min: enough to cover the
# restart gap itself, not enough to re-answer what the previous process handled).
_PROCESS_START = datetime.now(timezone.utc)
try:
    _STARTUP_CATCHUP = max(0, int(os.getenv("BOT_STARTUP_CATCHUP_SECS", "120")))
except ValueError:
    _STARTUP_CATCHUP = 120
_rr_cursor = [0]          # newest created_at seen, so each poll only scans new notes
_rr_next_scan = [0.0]     # throttle: replies are ≤N/hour, so scan the timeline every ~45s, not every poll


def _rr_in_quiet() -> bool:
    """True if now is inside the NOSTR_RANDOM_REPLY_QUIET 'HH-HH' window (handles past-midnight)."""
    if not _RR_QUIET:
        return False
    try:
        a, b = (int(x) % 24 for x in _RR_QUIET.split("-", 1))
    except (ValueError, TypeError):
        return False
    if a == b:
        return False
    h = time.localtime().tm_hour
    return a <= h < b if a < b else (h >= a or h < b)


def _thread_root(ev: dict) -> str:
    """Conversation root id: the NIP-10 'root'-marked e-tag, else the parent, else the note's own id
    (a top-level note IS its own root). Used to enforce the per-thread random-reply cap."""
    for t in ev.get("tags", []):
        if len(t) >= 4 and t[0] == "e" and t[3] == "root":
            return t[1]
    return _nk._reply_parent_id(ev) or ev.get("id", "")


def _state_suffix() -> str:
    key = (NOSTR_NSEC or "").strip()
    return hashlib.sha1(key.encode()).hexdigest()[:10] if key else "default"


_PROCESSED_IDS_FILE = os.path.join(script_dir, f".processed_nostr_ids_{_state_suffix()}")
_LOCK_FILE = _PROCESSED_IDS_FILE + ".lock"
_MAX_TRACKED_IDS = 5000
_replied_ids: set = set()


def _load_ids():
    global _replied_ids
    try:
        with open(_PROCESSED_IDS_FILE, "r") as f:
            _replied_ids = {ln.strip() for ln in f if ln.strip()}
    except FileNotFoundError:
        _replied_ids = set()


def _save_ids():
    global _replied_ids
    if len(_replied_ids) > _MAX_TRACKED_IDS:
        _replied_ids = set(sorted(_replied_ids)[-_MAX_TRACKED_IDS:])
    try:
        fd, tmp = tempfile.mkstemp(dir=script_dir, prefix=".nids_tmp_")
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(_replied_ids))
        os.replace(tmp, _PROCESSED_IDS_FILE)
    except Exception as e:
        print(f"[nostr] failed to save ids: {e}", flush=True)


def _claim(note_id) -> bool:
    """Atomically claim a note id across processes (file lock), like the pleroma listener."""
    try:
        with open(_LOCK_FILE, "w") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                _load_ids()
                if note_id in _replied_ids:
                    return False
                _replied_ids.add(note_id)
                _save_ids()
                return True
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"[nostr] claim failed: {e}", flush=True)
        return False


_load_ids()


def imageposter():
    """One-shot: generate an image from IMAGE_POSTER_PROMPT and post it to Nostr.
    Entry point for `main.py --image` (manager schedule + admin Test-Post). Uses the
    retrying backend call so a transient image-server 524 doesn't fail the post."""
    import random
    from config import IMAGE_POSTER_PROMPT, IMAGE_POSTER_TEXT, IMAGE_POSTER_RANDOM_SCENES
    from image_backend import generate_image_bytes_with_retries
    prompt = IMAGE_POSTER_PROMPT or ""
    if IMAGE_POSTER_RANDOM_SCENES:
        try:
            from random_scenes import RANDOM_SCENE_ELEMENTS
            prompt = f"{prompt}, {random.choice(RANDOM_SCENE_ELEMENTS)}".strip(", ")
        except Exception:
            pass
    print(f"[nostr] image poster generating: {prompt[:80]}", flush=True)
    image_bytes = generate_image_bytes_with_retries(prompt, max_retries=10, retry_delay=30)
    if image_bytes:
        _nk.post_image_to_fediverse(IMAGE_POSTER_TEXT, image_bytes=image_bytes)
        print("[nostr] image poster posted", flush=True)
    else:
        print("[nostr] image poster: generation returned None after retries", flush=True)


def _gather_media(note):
    """Download files linked on the note (or its nearest ancestor with media)."""
    files = list(note.get("files") or [])
    cur = note
    hops = 0
    while not files and cur.get("replyId") and hops < 5:
        cur = get_note(cur["replyId"])
        if not cur:
            break
        files = list(cur.get("files") or [])
        hops += 1
    media = []
    for f in files:
        data = download_image_from_url(f["url"], timeout=300)
        if data:
            # Extensionless/imeta-less links arrive with no type — sniff it from the bytes
            # so the effect/media command gets a correct image/video content-type.
            ct = f.get("type") or _nk.sniff_mime(data)
            media.append((f.get("name") or "file", data, ct or ""))
    # `had_files` lets the caller tell "no media on the post" apart from "media was there but the
    # download failed" (transient) so it can ask the user to retry instead of "attach a file".
    return media, bool(files)


def _sender_brand(note):
    # Resolves the sender's kind-0 profile (network) — only reached when an effect runs.
    return _nk.get_brand(note)


def _handle_media_command(note, command, arg):
    brand_handle, brand_avatar = _sender_brand(note)
    media, had_files = _gather_media(note)
    if not media:
        if command == "glow" and arg.strip():
            _gsum, _gout = process_media("glow", arg, [], brand_handle, brand_avatar)
            imgs = [(f["data"], f["content_type"]) for f in _gout if f["content_type"].startswith("image/")]
            send_reply(note, "" if imgs else (_gsum or "Couldn't make that glow post."), image_bytes=imgs or None)
            return
        if had_files:
            # The post DID carry media; we just couldn't download it (transient CDN/network blip).
            # Don't tell the user to attach a file — tell them to retry.
            send_reply(note, "⚠️ I found the media on that post but couldn't download it just now — try again in a moment.")
            return
        send_reply(note, "📎 Attach or link a file, then add `compress`, `clip 0:10 0:30`, `convert`, `meme <text>` or `glow <text>`.")
        return
    print(f"[nostr] forwarding {len(media)} file(s) for '{command}'")
    summary, out_files = process_media(command, arg, media, brand_handle, brand_avatar)
    image_outs = [(f["data"], f["content_type"]) for f in out_files if f["content_type"].startswith("image/")]
    video_outs = [f["data"] for f in out_files if f["content_type"].startswith("video/")]
    text = "" if command in NO_CAPTION_COMMANDS else (summary or "Done.")
    send_reply(note, text, image_bytes=image_outs or None, video_bytes=(video_outs[0] if video_outs else None))
    for v in video_outs[1:]:
        send_reply(note, "", video_bytes=v)


def _dispatch(note, prompt_text, own, thread_history):
    lower = prompt_text.lower()
    # Media / effect commands on an attached or linked file.
    media_cmd = next((c for c in MEDIA_COMMANDS if lower == c or lower.startswith(c + " ")), None)
    if media_cmd:
        arg = prompt_text[len(media_cmd):].strip()
        if media_cmd == "meme" and contains_bad_words(arg.lower()):
            send_reply(note, "I cannot add that text to an image.")
        else:
            _handle_media_command(note, media_cmd, arg)
        return

    if lower in ("screenshot", "shot", "ss") or lower.startswith(("screenshot ", "shot ", "ss ")):
        parts = prompt_text.split(None, 1)
        url = parts[1].strip() if len(parts) > 1 else ""
        if not url:
            send_reply(note, "Usage: screenshot <url>")
            return
        png, err = capture_screenshot(url)
        send_reply(note, f"📸 {url}" if png else (err or "❌ Screenshot failed."), image_bytes=[png] if png else None)
        return

    if lower == "ytdl" or lower.startswith("ytdl "):
        arg = prompt_text[4:].strip()
        as_video = False
        if arg.lower().startswith("video"):
            as_video, arg = True, arg[5:].strip()
        elif arg.lower().startswith("mp3"):
            arg = arg[3:].strip()
        url, clip, compress = parse_ytdl_postaction(arg)
        if clip or compress:
            as_video = True
        sender = (note.get("user") or {}).get("pubkey", "")
        elapsed = time.monotonic() - _ytdl_last_request.get(sender, 0.0)
        if not url:
            send_reply(note, "Usage: ytdl <url> (audio), ytdl video <url>, or ytdl video <url> clip 0:10 0:30 compress")
        elif elapsed < _YTDL_COOLDOWN_SECONDS:
            send_reply(note, f"⏳ Please wait {int(_YTDL_COOLDOWN_SECONDS - elapsed)}s before another download.")
        else:
            _ytdl_last_request[sender] = time.monotonic()
            data, mime, err = fetch_ytdl_media(url, video=as_video, clip=clip, compress=compress)
            if data and (as_video or (mime or "").startswith("video/")):
                send_reply(note, f"🎬 {url}", video_bytes=data)
            elif data:
                send_reply(note, f"🎵 {url}", audio_bytes=data)
            else:
                send_reply(note, f"❌ Download failed: {err or 'unknown error'}")
        return

    if lower in ("help", "/help", "commands", "?"):
        send_reply(note, BOT_HELP_TEXT)
        return

    if lower.startswith("search "):
        query = prompt_text[7:].strip()
        results, categories = smart_search(query)
        send_reply(note, summarize_search_results(results, query, categories) if results else f'No results found for "{query}".')
        return

    if lower.startswith("images "):
        query = prompt_text[7:].strip()
        if contains_bad_words(query.lower()):
            send_reply(note, "I cannot search for images with that content.")
            return
        reply_text, image_list = search_and_download_images(query, max_images=4)
        send_reply(note, reply_text, image_bytes=image_list or None)
        return

    if lower.startswith("news "):
        source = prompt_text[5:].strip()
        try:
            send_reply(note, fetch_news_from_source(source, max_headlines=10))
        except Exception as e:
            send_reply(note, f"Sorry, there was an error fetching news: {e}")
        return

    if "geni" in lower:
        if contains_bad_words(lower):
            send_reply(note, "I cannot generate images for that content.")
            return
        from image_backend import generate_image_bytes_with_retries
        image_bytes = generate_image_bytes_with_retries(prompt_text, max_retries=10, retry_delay=30)
        if image_bytes:
            send_reply(note, "Here is your image. Hope you like it.", image_bytes=image_bytes)
        else:
            print("[nostr] image generation returned None after retries")
        return

    if "/narrate" in lower:
        if not is_ai_configured():
            return
        narrate_prompt = re.sub(r"/narrate\s*", "", prompt_text, flags=re.IGNORECASE).strip()
        if not narrate_prompt:
            send_reply(note, "Usage: /narrate <your message>")
            return
        reply_text = generate_reply(narrate_prompt, thread_history=thread_history, ping=False, narrate_mode=True)
        if reply_text:
            from tts import generate_narration_video, generate_speech_with_retries
            avatar_url = own.get("avatarUrl") if own else None
            video = generate_narration_video(reply_text, avatar_url) if avatar_url else None
            if video:
                send_reply(note, "", video_bytes=video)
            else:
                send_reply(note, reply_text, audio_bytes=generate_speech_with_retries(reply_text))
        return

    # Plain reply.
    reply_text = generate_reply(prompt_text, thread_history=thread_history, ping=False)
    if reply_text:
        send_reply(note, reply_text)


def process_random_replies():
    """Occasionally start a friendly reply to a NIP-05-verified stranger on the firehose timeline.
    Order is deliberately cheap→expensive: random gate → (only then) profile + NIP-05 verify → start
    budget → LLM. Bounded by _RR_PER_HOUR new threads/hour; obeys quiet hours; each started thread is
    capped at _RR_MAX_THREAD bot replies (enforced in process_mentions). No-op unless enabled."""
    if not _RR_ENABLED or _rr_in_quiet():
        return
    now_ts = time.time()
    if now_ts < _rr_next_scan[0]:
        return                       # throttled — the per-note cursor still catches everything next scan
    _rr_next_scan[0] = now_ts + 45
    own = _nk._PUBKEY
    if not own:
        return
    import random as _random
    since = (_rr_cursor[0] - 5) if _rr_cursor[0] else int(time.time()) - 600
    notes = _nk.get_timeline(limit=80, since=since)
    if not notes:
        return
    newest = _rr_cursor[0]
    for note in notes:
        ev = note.get("_event") or {}
        nid = note.get("id")
        pk = (note.get("user") or {}).get("pubkey")
        ts = ev.get("created_at", 0)
        if ts > newest:
            newest = ts
        if not nid or not pk or pk == own or pk.lower() in BOT_NOSTR_PUBKEYS or nid in _rr_seen:
            continue   # never random-reply to ANOTHER of our bots (anti-loop — this path reads the
                       # timeline, not get_mentions, so it needs its own bot-pubkey guard)
        if note.get("replyId"):
            continue   # only START on top-level posts, never barge into an existing thread
        _rr_seen.add(nid)
        if _random.random() > _RR_PROB:
            continue                       # RANDOM GATE — almost everything stops here (no work done)
        meta = _nk.resolve_user(pk) or {}  # network (cached); only for the rare gated note
        nip05 = meta.get("nip05") or ""
        if not nip05 or not _nk.verify_nip05(pk, nip05):
            continue                       # NIP-05 only
        text = (note.get("text") or "").strip()
        if not text or _NOSTR_TOKEN_RE.sub("", text).strip() == "":
            continue
        if not _rr_starts.allow("global"):
            break                          # per-hour start budget spent → stop scanning this poll
        try:
            reply = generate_reply(
                "Reply briefly, warmly and on-topic (1-2 sentences, no hashtags) to this stranger's "
                f"post: {text[:1500]}", thread_history=None, ping=False)
            if reply:
                send_reply(note, reply)
                _rr_threads[nid] = 1       # this top-level note is now a tracked thread root
                print(f"[nostr] random-reply → {nid[:12]} (@{meta.get('username','?')} {nip05})", flush=True)
        except Exception as e:
            print(f"[nostr] random-reply failed for {nid[:12]}: {e}", flush=True)
    _rr_cursor[0] = newest
    if len(_rr_seen) > 8000:
        _rr_seen.clear()
    if len(_rr_threads) > 2000:
        for k in list(_rr_threads)[:1000]:
            _rr_threads.pop(k, None)


def process_mentions():
    own = get_own_account()
    if not own:
        print("[nostr] no account configured (NOSTR_NSEC missing)", flush=True)
        return
    own_pubkey = own.get("pubkey")
    blacklist = [b.lower() for b in BOT_BLACKLIST]

    mentions = get_mentions(limit=40)
    print(f"[nostr] fetched {len(mentions)} p-tagged notes", flush=True)
    # A RESTARTED process must not reach back into mentions the previous one already answered.
    # The claim-file + relay guard should cover that, but both have failed in practice (two confirmed
    # same-bot double replies, each a fresh pid a few minutes after the first), and in development
    # this service restarts constantly — so bound how far back a young process is willing to look.
    # Once it's been up longer than the window this is a no-op and behaviour is unchanged.
    cutoff = max(datetime.now(timezone.utc) - timedelta(minutes=10),
                 _PROCESS_START - timedelta(seconds=_STARTUP_CATCHUP))

    for note in mentions:
        nid = note.get("id")
        if not nid:
            continue
        try:
            dt = datetime.fromisoformat((note.get("createdAt") or "").replace("Z", "+00:00"))
            if dt < cutoff:
                continue
        except Exception:
            continue
        user = note.get("user") or {}
        if user.get("pubkey") == own_pubkey:
            continue  # never reply to self
        if (user.get("pubkey") or "").lower() in BOT_NOSTR_PUBKEYS:
            continue  # never reply to ANOTHER of our nostr bots (anti-loop, by pubkey)
        if any(b in (user.get("username") or "").lower() for b in blacklist):
            continue
        # Only respond when actually ADDRESSED (first mention / reply to the bot) — not when
        # the bot is just a NIP-10 p-tag carried forward through a thread it's in. Otherwise
        # it would reply to every subsequent reply between other users in that thread.
        if not _nk.is_addressed(note, own_pubkey):
            continue
        prompt_text = _NOSTR_TOKEN_RE.sub("", note.get("text") or "").strip()
        prompt_text = re.sub(r"@[\w@.]+", "", prompt_text).strip()[:4000]
        if not prompt_text:
            # A quote-only mention (just a nevent/note ref) strips to nothing — pull in the
            # quoted post's text so the bot can actually respond to it instead of skipping.
            quoted = _nk.get_quoted_note(note)
            qtext = (quoted or {}).get("text", "").strip()
            if qtext:
                prompt_text = f"Respond to this quoted post: {qtext}"[:4000]
            else:
                continue
        # Rate limit only ACTUAL requests (after we know there's real work), and BEFORE claim:
        # a token is consumed only on success, a throttled mention is left unclaimed so it
        # re-checks next poll (no token spent on rejection) and is served once the window
        # frees — rather than burning a reply, draining the budget on empties, or being
        # permanently dropped. Silent on rejection (replying would just amplify spam).
        if not _rl.allow(user.get("pubkey") or ""):
            print(f"[nostr] rate-limited {user.get('username')} ({(user.get('pubkey') or '')[:10]}…) — skipping", flush=True)
            continue
        if not _claim(nid):
            continue
        # Belt-and-suspenders across restarts: the local processed-ids file can be lost (or not yet
        # written) when the listener is killed mid-render of a slow effect and restarted — the
        # relay is the durable record of what we already answered, so skip anything we've already
        # replied to. This closes the "fired twice" window a restart-during-render opened.
        if has_own_reply(nid):
            print(f"[nostr] already replied to {nid[:12]} (per relay) — skipping", flush=True)
            continue
        # Random-reply thread cap: if THIS conversation was started by a random reply, the bot answers
        # at most _RR_MAX_THREAD times total (the opening + the follow-up), then bows out so it never
        # gets stuck in an endless back-and-forth with one stranger.
        _root = _thread_root(note.get("_event") or {})
        if _root in _rr_threads and _rr_threads[_root] >= _RR_MAX_THREAD:
            print(f"[nostr] random-reply thread {_root[:12]} hit {_RR_MAX_THREAD}-reply cap — bowing out", flush=True)
            continue
        print(f"[nostr] processing {nid[:12]} from {user.get('username')}: {prompt_text[:80]}", flush=True)
        try:
            thread_history = get_thread_history(nid)
            _dispatch(note, prompt_text, own, thread_history)
            if _root in _rr_threads:
                _rr_threads[_root] += 1   # count the bot's follow-up toward the per-thread cap
        except Exception as e:
            print(f"[nostr] dispatch failed for {nid[:12]}: {e}", flush=True)
            import traceback
            traceback.print_exc()
