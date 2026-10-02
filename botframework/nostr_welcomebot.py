#!/usr/bin/env python3
"""The welcome bot for a NOSTR bot (a Pleroma bot runs welcomebot.py, against Pleroma's database).

A member of a Nostr instance is somebody this node granted a NIP-05 name (`name@poster.place`). The
node hands over that roll (community_stats.member_list, via community_api); a name that was not on
it at the last look is a newcomer, and gets a welcome post that TAGS them (`nostr:npub…`, which
Nostr clients render as their name and notify them about, and the fediverse side turns into a real
@mention).

Three rules, each because the alternative posts something wrong:
  * the FIRST look only remembers -- otherwise every existing member is welcomed at once;
  * "could not ask", and an EMPTY roll, change nothing -- a registry that reads empty for a moment
    must not make every member a newcomer on the next good read;
  * a jump of more than BULK names at once is an import or a restore, not a crowd of strangers
    arriving in one minute: it is remembered and announced as nothing.
Other bots (which mint themselves a name here) are never welcomed.
"""
import datetime
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time

import pytz

import community_api
from ai import generate_reply
from config import AUTO_NARRATE, BOT_BLACKLIST, OPENAI_ENDPOINT, WELCOME_IMAGE, WELCOME_MESSAGE, WELCOME_PROMPT
from core.utils import is_listed_bot
from tts import generate_narration_video, generate_speech_with_retries

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BULK = 10
_CJK = re.compile(r"[一-鿿぀-ゟ゠-ヿ가-힯]")


def _post(text, image_bytes=None, audio_bytes=None, video_bytes=None):
    from nostr import post_image_to_fediverse
    post_image_to_fediverse(text, image_bytes, audio_bytes=audio_bytes, video_bytes=video_bytes)


def _ai_on() -> bool:
    return bool(OPENAI_ENDPOINT and OPENAI_ENDPOINT.startswith(("http://", "https://")))


def _state_path() -> str:
    tag = hashlib.sha256((os.getenv("NOSTR_NSEC") or "default").encode()).hexdigest()[:12]
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), f".welcomed_nostr_{tag}.json")


def _load_seen():
    try:
        with open(_state_path()) as f:
            return set(json.load(f).get("seen") or []), True
    except (OSError, ValueError, AttributeError, TypeError):
        return set(), False


def _save_seen(seen: set) -> None:
    tmp = _state_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"seen": sorted(seen), "at": int(time.time())}, f)
    os.replace(tmp, _state_path())


def newcomers(roll: list, seen: set, had_memory: bool) -> tuple:
    """(to welcome, what to remember)."""
    keys = {m["pubkey"]: m for m in roll if m.get("pubkey")}
    remember = set(seen) | set(keys)
    if not had_memory:
        return [], remember
    fresh = [m for pk, m in keys.items() if pk not in seen]
    if len(fresh) > BULK:
        logging.info(f"[WELCOMEBOT] {len(fresh)} names appeared at once -- an import, not newcomers; welcoming none")
        return [], remember
    fresh = [m for m in fresh if not m.get("bot") and not is_listed_bot(m.get("handle") or "", BOT_BLACKLIST)]
    return fresh, remember


# WHAT THE MODEL MAY SAY ABOUT THE PLACE. Told only "welcome X to poster.place", the model guessed what
# the site is from its NAME and wrote a confident brochure for a gallery of Polish poster art ("All
# originals, guaranteed authentic") -- every word invented, posted publicly under the instance's own
# bot. So it is told what it IS, and that it knows nothing else; and a long reply (where invented
# detail lives) falls back to the plain welcome rather than going out.
FACTS = ("Facts, and the ONLY things you know about {instance_name}: it is a self-hosted Nostr community "
         "(a PosterChan server). The new member's address there is @{username}, which works as their "
         "Nostr name (NIP-05). Nothing else about {instance_name} -- its topic, content, features, "
         "history or users -- is known to you: do not describe the site, do not list features, do not "
         "invent anything about it. Write the welcome itself, under {max_words} words.")
MAX_WORDS = 70


def message(m: dict, instance: str = "") -> str:
    """The welcome, tagging the newcomer. The model writes it with their readable address, which is
    then swapped for the tagging reference; a reply that loses the address gets it put in front.
    The instance is the domain of the address this node granted them -- i.e. this node's own."""
    handle, ref = (m.get("handle") or "").strip(), (m.get("ref") or m.get("handle") or "").strip()
    instance = instance or (handle.rsplit("@", 1)[-1] if handle.count("@") >= 2 else "our instance")
    plain = f"{ref} {WELCOME_MESSAGE.format(instance_name=instance)}"
    if not _ai_on():
        return plain
    try:
        fill = dict(username=handle.lstrip("@"), instance_name=instance, max_words=MAX_WORDS)
        ai = (generate_reply(WELCOME_PROMPT.format(**fill) + "\n\n" + FACTS.format(**fill)
                             + " /no_think") or "").replace("/no_think", "").strip()
    except Exception as e:
        logging.warning(f"[WELCOMEBOT] AI wording failed, using the plain welcome: {e}")
        return plain
    if not ai or ai == "None" or len(ai) < 10 or _CJK.search(ai):
        return plain
    if len(ai.split()) > MAX_WORDS + 20:
        logging.info(f"[WELCOMEBOT] AI welcome ran to {len(ai.split())} words, using the plain welcome")
        return plain
    bare = handle.lstrip("@")
    for spelling in (handle, "@" + bare.split("@")[0], bare):
        if spelling and spelling in ai:
            return ai.replace(spelling, ref, 1).replace(spelling, "")
    return f"{ref} {ai}"


def welcome(print_only=False):
    try:
        roll = community_api.get("members")["members"]
    except community_api.Unavailable as e:
        logging.warning(f"[WELCOMEBOT] could not read the member roll, trying again next minute: {e}")
        return
    if not roll:
        logging.info("[WELCOMEBOT] the member roll read empty -- changing nothing")
        return
    seen, had_memory = _load_seen()
    fresh, remember = newcomers(roll, seen, had_memory)
    if not print_only:
        _save_seen(remember)
    for m in fresh:
        msg = message(m)
        print(msg)
        if print_only:
            continue
        audio_bytes = video_bytes = None
        if AUTO_NARRATE:
            video_bytes = generate_narration_video(msg, os.getenv("NOSTR_PROFILE_PICTURE") or None)
            if not video_bytes:
                audio_bytes = generate_speech_with_retries(msg)
        image_bytes = None
        if WELCOME_IMAGE and os.path.exists(WELCOME_IMAGE):
            with open(WELCOME_IMAGE, "rb") as f:
                image_bytes = f.read()
        try:
            _post(msg, image_bytes, audio_bytes=audio_bytes, video_bytes=video_bytes)
        except Exception as e:
            logging.error(f"[WELCOMEBOT] post failed: {e}")


def waitToStart():
    while True:
        if datetime.datetime.now(pytz.timezone("Atlantic/Reykjavik")).second == 0:
            break
        time.sleep(0.5)


def background():
    print("Running the welcome bot (Nostr, this instance's new members)")
    while True:
        t = threading.Thread(target=welcome)
        t.start()
        t.join(timeout=55)
        time.sleep(60)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"daemon": lambda: (waitToStart(), background()),
     "welcome": lambda: welcome(len(sys.argv) > 2 and sys.argv[2] == "print")}.get(
        cmd, lambda: print("Usage: nostr_welcomebot.py daemon | welcome [print]"))()
