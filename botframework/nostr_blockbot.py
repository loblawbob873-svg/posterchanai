#!/usr/bin/env python3
"""The block bot for a NOSTR bot (a Pleroma bot runs blockbot.py, against Pleroma's database).

The facts come from this node itself
(app/services/community_stats.py, via community_api): a fediverse account blocking one of ours
arrives at the ActivityPub server as a Block, and on Nostr a block is a public mute list. The bot
posts as its own Nostr account, which the node's fediverse server delivers to its fediverse
followers -- so the announcements reach both networks.

    blocks()   every minute: announce blocks that are new since the last look
    scalps()   daily: the leaderboard of our most-blocked accounts
    fba()      daily: the most-defederated instances (fba.ryona.agency)
"""
import datetime
import json
import logging
import os
import re
import sys
import threading
import time

import pytz
import requests

import community_api
import nostr_engagement as engagement
from ai import generate_reply
from config import AUTO_NARRATE, BLOCK_IMAGE, BLOCK_LIMIT, BLOCK_PROMPT, OPENAI_ENDPOINT
from tts import generate_narration_video, generate_speech_with_retries

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _post(text, image_bytes=None, audio_bytes=None, video_bytes=None):
    from nostr import post_image_to_fediverse
    post_image_to_fediverse(text, image_bytes, audio_bytes=audio_bytes, video_bytes=video_bytes)


def _ai_on() -> bool:
    return bool(OPENAI_ENDPOINT and OPENAI_ENDPOINT.startswith(("http://", "https://")))


# --- who blocked whom ---------------------------------------------------------------------------

def _state_path() -> str:
    """One memory per bot (bots share this directory): which blocks it has already announced."""
    who = (os.getenv("NOSTR_NSEC") or "default")
    import hashlib
    tag = hashlib.sha256(who.encode()).hexdigest()[:12]
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), f".last_blockbot_{tag}.json")


def _load_seen():
    try:
        with open(_state_path()) as f:
            d = json.load(f)
        return set(d.get("seen") or []), True
    except (OSError, ValueError):
        return set(), False


def _save_seen(seen: set) -> None:
    tmp = _state_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"seen": sorted(seen), "at": int(time.time())}, f)
    os.replace(tmp, _state_path())


def _key(b: dict) -> str:
    return f"{b.get('via')}|{b.get('blocker')}|{b.get('blocked')}"


def new_blocks(current: list, seen: set, had_memory: bool) -> tuple:
    """(to announce, what to remember). The FIRST look only remembers: without a memory every block
    that already exists would be announced at once. A block that was lifted is forgotten, so one
    put back later is news again."""
    keys = {_key(b): b for b in current}
    fresh = [b for k, b in keys.items() if k not in seen] if had_memory else []
    return fresh, set(keys)


def validate_block_message(ai_msg: str, handles: list) -> bool:
    """The model may reword the post, never the names: every handle must survive verbatim, and no
    other script may creep in (a known failure of the smaller models)."""
    if not ai_msg:
        return False
    low = ai_msg.lower()
    if any(h.lower() not in low for h in handles):
        logging.warning("AI block message dropped or changed a name; using the plain message")
        return False
    if re.search(r"[一-鿿぀-ゟ゠-ヿ가-힯]", ai_msg):
        return False
    return True


def blocks(print_only=False):
    try:
        current = community_api.get("blocks")["blocks"]
    except community_api.Unavailable as e:
        logging.warning(f"[BLOCKBOT] could not read blocks, trying again next minute: {e}")
        return                                     # never mistake "could not ask" for "no blocks"
    seen, had_memory = _load_seen()
    fresh, remember = new_blocks(current, seen, had_memory)
    if not print_only:
        _save_seen(remember)
    if not fresh:
        return
    lines = [f"BLOCKER: {b['blocker_handle']} blocked {b['blocked_handle']}"
             + (" (on the fediverse)" if b.get("via") == "fediverse" else " (on Nostr)") for b in fresh]
    msg = "\n".join(lines)
    handles = [h for b in fresh for h in (b["blocker_handle"], b["blocked_handle"])]
    if _ai_on():
        try:
            ai_msg = (generate_reply(BLOCK_PROMPT.format(block_details=msg) + " /no_think") or "").replace("/no_think", "").strip()
            if ai_msg and "None" not in ai_msg and validate_block_message(ai_msg, handles):
                msg = re.sub(r"\bBLOCKEE:\s*", "", ai_msg)
        except Exception as e:
            logging.warning(f"[BLOCKBOT] AI wording failed, using the plain message: {e}")
    print(msg)
    if print_only:
        return
    audio_bytes = video_bytes = None
    if AUTO_NARRATE:
        video_bytes = generate_narration_video(msg, os.getenv("NOSTR_PROFILE_PICTURE") or None)
        if not video_bytes:
            audio_bytes = generate_speech_with_retries(msg)
    image_bytes = None
    if BLOCK_IMAGE and os.path.exists(BLOCK_IMAGE):
        with open(BLOCK_IMAGE, "rb") as f:
            image_bytes = f.read()
    try:
        _post(msg, image_bytes, audio_bytes=audio_bytes, video_bytes=video_bytes)
    except Exception as e:
        logging.error(f"[BLOCKBOT] post failed: {e}")


# --- the leaderboard ----------------------------------------------------------------------------

def validate_scalps_message(ai_msg: str, handles: list) -> bool:
    if not ai_msg:
        return False
    low = ai_msg.lower()
    return all(h.lower() in low for h in handles)


def scalps(print_only=False):
    try:
        rows = community_api.get("block-leaderboard", min=BLOCK_LIMIT)["leaderboard"]
    except community_api.Unavailable as e:
        logging.warning(f"[BLOCKBOT] could not read the leaderboard: {e}")
        return
    if not rows:
        logging.info("[BLOCKBOT] nobody here is blocked -- no leaderboard today")
        return
    final = "Block Leaderboard:\n\n" + "\n\n".join(f"{r['handle']}: {r['count']}" for r in rows)
    if _ai_on():
        try:
            prompt = ("Format the following blocklist data as a social media post. Use EXACTLY the names "
                      "provided below - do NOT invent or replace any. Keep every name and block count exactly "
                      "as shown. Add a brief introductory sentence and congratulate the MOST-BLOCKED accounts. "
                      "IMPORTANT: these accounts are the ones being blocked the most by the community -- they "
                      "are NOT doing the blocking. " + final)
            ai_msg = generate_reply(prompt)
            if validate_scalps_message(ai_msg, [r["handle"] for r in rows]):
                final = ai_msg
        except Exception as e:
            logging.warning(f"[BLOCKBOT] AI wording failed, using the plain leaderboard: {e}")
    print(final)
    if not print_only:
        _post(final)


# --- the most-defederated instances -------------------------------------------------------------

def fba(print_only=False):
    try:
        r = requests.get("https://fba.ryona.agency/top?blocked=20", timeout=30)
        data = r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError) as e:
        logging.error(f"[BLOCKBOT] FBA fetch failed: {e}")
        return
    if not isinstance(data, list):
        logging.error("[BLOCKBOT] unexpected FBA response")
        return
    msg = "Top Most Defederated Instances: \n\n" + "".join(
        f"{i.get('domain', '')} : {i.get('highscore', '')}\n\n" for i in data if isinstance(i, dict))
    print(msg)
    if not print_only:
        _post(msg)


def waitToStart():
    while True:
        now = datetime.datetime.now(pytz.timezone("Atlantic/Reykjavik"))
        if now.second == 0:
            print(f"Clock in Sync: {now}")
            break
        time.sleep(0.5)


def background():
    print("Running the block bot (Nostr + fediverse)")
    while True:
        now = datetime.datetime.now(pytz.timezone("Atlantic/Reykjavik"))
        if now.hour == 1 and now.minute == 15:
            fba()
            scalps()
        if now.hour == 1 and now.minute == 0:
            engagement.daily_top_posts()
            time.sleep(5)
            engagement.post_active_user_stats()
        t = threading.Thread(target=blocks)
        t.start()
        t.join(timeout=55)
        time.sleep(60)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    pr = len(sys.argv) > 2 and sys.argv[2] == "print"
    {"daemon": lambda: (waitToStart(), background()), "blocks": lambda: blocks(pr),
     "scalps": lambda: scalps(pr), "fba": lambda: fba(pr)}.get(cmd, lambda: print(
        "Usage: blockbot.py daemon | blocks [print] | scalps [print] | fba [print]"))()
