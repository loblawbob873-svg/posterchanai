#!/usr/bin/env python3
"""The unfollow bot for a NOSTR bot (a Pleroma bot runs unfollowbot.py, against Pleroma's database).

Who follows this instance's members, from both sides, read from the node (/api/community/follows):
  * on Nostr, a follow is the follower's kind-3 contact list p-tagging a member; an UNFOLLOW is that author's
    NEWER list no longer naming them;
  * on the fediverse, an unfollow is an ActivityPub Undo(Follow), which the AP server records as a tombstone
    with the time it arrived.

Every rule below exists because the alternative announces somebody unfollowing who did not:
  * the FIRST look only remembers -- otherwise every existing follow is "new" next time;
  * "could not ask" changes nothing, and the server refuses an answer the relay cut short;
  * an unfollow needs PROOF: the author's own newer list, without the member. A list that is simply gone from
    the relay (pruned, purged, never reached us) is "no evidence", never "unfollowed";
  * a list that SHRANK to under half (from 10+) is a contact-list wipe -- a client bug Nostr is full of, the
    person never chose it -- and an author dropping more than PER_AUTHOR members at once is a clean-up: both
    are remembered and announced as nothing; so is a pass with more than BULK unfollows (an import, a restore);
  * bots are never announced, on either side.
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
from config import AUTO_NARRATE, OPENAI_ENDPOINT, UNFOLLOW_IMAGE, UNFOLLOW_SILENT_MODE
from tts import generate_narration_video, generate_speech_with_retries

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PER_AUTHOR = 5
BULK = 20
WIPE_FROM = 10


def _state_path() -> str:
    tag = hashlib.sha256((os.getenv("NOSTR_NSEC") or "default").encode()).hexdigest()[:12]
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), f".unfollow_nostr_{tag}.json")


def _load():
    try:
        with open(_state_path()) as f:
            st = json.load(f)
        return {"nostr": dict(st.get("nostr") or {}), "fedi_at": int(st.get("fedi_at") or 0)}, True
    except (OSError, ValueError, AttributeError, TypeError):
        return {"nostr": {}, "fedi_at": 0}, False


def _save(state: dict) -> None:
    tmp = _state_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(dict(state, at=int(time.time())), f)
    os.replace(tmp, _state_path())


def diff(old: dict, picture: list, confirmed: list, bots: set, fedi: list, fedi_at: int, had_memory: bool):
    """(unfollows [(follower, member, via)], new state). Pure: the whole decision, testable without a relay.

    old      -- {author: {at, size, follows}} from the last look
    picture  -- the current lists naming a member
    confirmed-- the current lists of remembered authors absent from `picture`
    """
    new_lists = {r["author"]: {"at": r["at"], "size": r["size"], "follows": list(r["follows"])} for r in picture}
    current = dict(new_lists)
    for r in confirmed:
        current.setdefault(r["author"], {"at": r["at"], "size": r["size"], "follows": list(r["follows"])})
    state_nostr = {a: v for a, v in new_lists.items() if v["follows"]}
    found = []
    for author, o in old.items():
        n = current.get(author)
        if n is None:
            state_nostr.setdefault(author, o)        # no evidence either way: keep what we knew
            continue
        if n["at"] <= o["at"]:
            continue
        dropped = sorted(set(o.get("follows") or []) - set(n["follows"]))
        if not dropped or author in bots:
            continue
        if o.get("size", 0) >= WIPE_FROM and n["size"] < o["size"] / 2:
            logging.info("[UNFOLLOWBOT] a contact list shrank %d -> %d: a wipe, not %d unfollows"
                         % (o["size"], n["size"], len(dropped)))
            continue
        if len(dropped) > PER_AUTHOR:
            logging.info("[UNFOLLOWBOT] one author dropped %d members at once: a clean-up, announcing none" % len(dropped))
            continue
        found += [(author, m, "nostr") for m in dropped if m not in bots]
    new_fedi_at = max([fedi_at] + [f["at"] for f in fedi])
    if had_memory:
        found += [(f["ref"], f["member"], "fediverse") for f in fedi
                  if f["gone"] and f["at"] > fedi_at and f["member"] not in bots]
    state = {"nostr": state_nostr, "fedi_at": new_fedi_at}
    if not had_memory:
        return [], state
    if len(found) > BULK:
        logging.info("[UNFOLLOWBOT] %d unfollows in one look -- an import or a restore, announcing none" % len(found))
        return [], state
    return found, state


def _ai_on() -> bool:
    return bool(OPENAI_ENDPOINT and OPENAI_ENDPOINT.startswith(("http://", "https://")))


def message(found: list, names: dict, refs: dict) -> str:
    """The post. Lines tag both people (`nostr:npub…` renders as their name and notifies them); the model may
    dramatise it, written with readable names that are then swapped for the tagging references -- and a reply
    that lost a name falls back to the plain lines."""
    def ref(x):
        return refs.get(x) or x                      # a fediverse unfollower arrives as its own reference
    plain = "\n".join(f"{ref(f)} unfollowed {ref(m)}." for f, m, _ in found)
    if not _ai_on():
        return plain
    readable = "\n".join(f"{names.get(f) or ref(f)} unfollowed {names.get(m) or ref(m)}." for f, m, _ in found)
    coward = names.get(found[-1][0]) or ref(found[-1][0])
    try:
        ai = (generate_reply(f"Generate a dramatic version of this unfollow notification. Respond only with the "
                             f"post: {readable} Mock {coward} for being a coward who unfollowed someone. Keep every "
                             f"name exactly as written, without quotes. /no_think") or "").replace("/no_think", "").strip()
    except Exception as e:
        logging.warning(f"[UNFOLLOWBOT] AI wording failed, using the plain lines: {e}")
        return plain
    if not ai or ai == "None" or len(ai) > 1200:
        return plain
    for f, m, _ in found:
        for who in (f, m):
            name = names.get(who)
            if name and name != ref(who):
                if name not in ai:
                    return plain
                ai = ai.replace(name, ref(who))
    return ai


def _post(text, image_bytes=None, audio_bytes=None, video_bytes=None):
    from nostr import post_image_to_fediverse
    post_image_to_fediverse(text, image_bytes, audio_bytes=audio_bytes, video_bytes=video_bytes)


def unfollows(print_only=False):
    try:
        data = community_api.get("follows")
        old, had_memory = _load()
        missing = [a for a in old["nostr"] if a not in {r["author"] for r in data["nostr"]}]
        confirmed = []
        for i in range(0, len(missing), 50):
            confirmed += community_api.get("contact-lists", authors=",".join(missing[i:i + 50]))["lists"]
    except community_api.Unavailable as e:
        logging.warning(f"[UNFOLLOWBOT] could not read follows, trying again next round: {e}")
        return
    found, state = diff(old["nostr"], data["nostr"], confirmed, set(data.get("bots") or []),
                        data.get("fedi") or [], old["fedi_at"], had_memory)
    if not print_only:
        _save(state)
    if not had_memory:
        logging.info(f"[UNFOLLOWBOT] first look: remembering {len(state['nostr'])} follower lists, announcing nothing")
        return
    if not found:
        return
    msg = message(found, data.get("names") or {}, data.get("refs") or {})
    print(msg)
    if print_only or UNFOLLOW_SILENT_MODE:
        return
    audio_bytes = video_bytes = None
    if AUTO_NARRATE:
        video_bytes = generate_narration_video(msg, os.getenv("NOSTR_PROFILE_PICTURE") or None)
        if not video_bytes:
            audio_bytes = generate_speech_with_retries(msg)
    image_bytes = None
    if UNFOLLOW_IMAGE and os.path.exists(UNFOLLOW_IMAGE):
        with open(UNFOLLOW_IMAGE, "rb") as f:
            image_bytes = f.read()
    try:
        _post(msg, image_bytes, audio_bytes=audio_bytes, video_bytes=video_bytes)
    except Exception as e:
        logging.error(f"[UNFOLLOWBOT] post failed: {e}")


def waitToStart():
    while True:
        if datetime.datetime.now(pytz.timezone("Atlantic/Reykjavik")).second == 0:
            break
        time.sleep(0.5)


def background():
    print("Running the unfollow bot (Nostr + fediverse followers of this instance's members)")
    while True:
        t = threading.Thread(target=unfollows)
        t.start()
        t.join(timeout=290)
        time.sleep(300)                              # every 5 minutes, as the Pleroma unfollow bot did


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"daemon": lambda: (waitToStart(), background()),
     "unfollows": lambda: unfollows(len(sys.argv) > 2 and sys.argv[2] == "print")}.get(
        cmd, lambda: print("Usage: nostr_unfollowbot.py daemon | unfollows [print]"))()
