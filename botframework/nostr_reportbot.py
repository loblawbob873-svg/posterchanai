#!/usr/bin/env python3
"""The report bot for a NOSTR bot (a Pleroma bot runs reportbot.py, against Pleroma's admin API).

A Nostr report is a public NIP-56 event (kind 1984). The node reads them off its own relay and
hands over only the ones that involve THIS instance's members -- filed by one of ours, or about one
of ours (app/services/community_stats.reports, via community_api). Every minute the bot announces
what is new, posting as its own Nostr account; the node's fediverse server carries it to the
fediverse too.

The headline (who reported whom, for what) is written HERE, never by the model -- the same rule as
the block bot, for the same reason: it is the only way the names are the right people. The model may
add commentary under it, and that commentary is dropped if it names anybody.
"""
import datetime
import hashlib
import json
import logging
import os
import sys
import threading
import time

import pytz

import community_api
from ai import generate_reply
from block_wording import COMMENTARY_ONLY, commentary
from config import AUTO_NARRATE, BOT_BLACKLIST, OPENAI_ENDPOINT, REPORT_IMAGE, REPORT_PROMPT
from core.utils import is_listed_bot
from tts import generate_narration_video, generate_speech_with_retries

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

LOOKBACK = 7 * 86400          # what is asked for each minute
FORGET_AFTER = 14 * 86400     # > LOOKBACK, so nothing still being returned is ever forgotten
MAX_LINES = 5


def _post(text, image_bytes=None, audio_bytes=None, video_bytes=None):
    from nostr import post_image_to_fediverse
    post_image_to_fediverse(text, image_bytes, audio_bytes=audio_bytes, video_bytes=video_bytes)


def _ai_on() -> bool:
    return bool(OPENAI_ENDPOINT and OPENAI_ENDPOINT.startswith(("http://", "https://")))


def _state_path() -> str:
    tag = hashlib.sha256((os.getenv("NOSTR_NSEC") or "default").encode()).hexdigest()[:12]
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), f".last_reportbot_{tag}.json")


def _load_seen():
    try:
        with open(_state_path()) as f:
            return dict(json.load(f).get("seen") or {}), True
    except (OSError, ValueError, AttributeError):
        return {}, False


def _save_seen(seen: dict) -> None:
    tmp = _state_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"seen": seen, "at": int(time.time())}, f)
    os.replace(tmp, _state_path())


def new_reports(current: list, seen: dict, had_memory: bool, now: int | None = None) -> tuple:
    """(to announce, what to remember). The FIRST look only remembers -- otherwise every report of the
    last week is announced at once the moment the bot is switched on."""
    now = int(time.time()) if now is None else now
    fresh = [r for r in current if r.get("id") not in seen] if had_memory else []
    remember = {k: t for k, t in seen.items() if now - int(t or 0) < FORGET_AFTER}
    remember.update({r["id"]: now for r in current if r.get("id")})
    return fresh, remember


def _involves_a_bot(r: dict) -> bool:
    return any(is_listed_bot(r.get(k) or "", BOT_BLACKLIST) for k in ("reporter_handle", "reported_handle"))


def _line(r: dict) -> str:
    who = lambda side: (r.get(f"{side}_ref") or r.get(f"{side}_handle") or "someone").strip()  # noqa: E731
    out = f"🚨 {who('reporter')} reported {who('reported')} for {r.get('type') or 'other'}"
    if r.get("reason"):
        out += f': "{r["reason"]}"'
    if r.get("note_ref"):
        out += f"\n{r['note_ref']}"
    return out


def compose(fresh: list) -> str:
    shown = fresh[:MAX_LINES]
    msg = "\n\n".join(_line(r) for r in shown)
    if len(fresh) > MAX_LINES:
        msg += f"\n\n…and {len(fresh) - MAX_LINES} more"
    if _ai_on():
        try:
            details = "; ".join(f"Reporter: {r.get('reporter_handle')}, Reported: {r.get('reported_handle')}, "
                                f"Type: {r.get('type')}, Reason: {r.get('reason') or 'none given'}" for r in shown)
            ai_msg = (generate_reply(REPORT_PROMPT.format(report_details=details) + COMMENTARY_ONLY + " /no_think")
                      or "").replace("/no_think", "").strip()
            extra = commentary(ai_msg, names=[r.get(k) for r in shown for k in ("reporter_handle", "reported_handle")])
            if extra:
                msg += "\n\n" + extra
        except Exception as e:
            logging.warning(f"[REPORTBOT] AI wording failed, using the plain message: {e}")
    return msg


def reports(print_only=False):
    try:
        current = community_api.get("reports", since=int(time.time()) - LOOKBACK)["reports"]
    except community_api.Unavailable as e:
        logging.warning(f"[REPORTBOT] could not read reports, trying again next minute: {e}")
        return                                     # never mistake "could not ask" for "no reports"
    seen, had_memory = _load_seen()
    fresh, remember = new_reports(current, seen, had_memory)
    fresh = [r for r in fresh if not _involves_a_bot(r)]
    if not print_only:
        _save_seen(remember)
    if not fresh:
        return
    msg = compose(fresh)
    print(msg)
    if print_only:
        return
    audio_bytes = video_bytes = None
    if AUTO_NARRATE:
        video_bytes = generate_narration_video(msg, os.getenv("NOSTR_PROFILE_PICTURE") or None)
        if not video_bytes:
            audio_bytes = generate_speech_with_retries(msg)
    image_bytes = None
    if REPORT_IMAGE and os.path.exists(REPORT_IMAGE):
        with open(REPORT_IMAGE, "rb") as f:
            image_bytes = f.read()
    try:
        _post(msg, image_bytes, audio_bytes=audio_bytes, video_bytes=video_bytes)
    except Exception as e:
        logging.error(f"[REPORTBOT] post failed: {e}")


def waitToStart():
    while True:
        if datetime.datetime.now(pytz.timezone("Atlantic/Reykjavik")).second == 0:
            break
        time.sleep(0.5)


def background():
    print("Running the report bot (Nostr, this instance's members only)")
    while True:
        t = threading.Thread(target=reports)
        t.start()
        t.join(timeout=55)
        time.sleep(60)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"daemon": lambda: (waitToStart(), background()),
     "reports": lambda: reports(len(sys.argv) > 2 and sys.argv[2] == "print")}.get(
        cmd, lambda: print("Usage: nostr_reportbot.py daemon | reports [print]"))()
