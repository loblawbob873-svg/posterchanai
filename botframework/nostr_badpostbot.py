#!/usr/bin/env python3
"""Bad Post of the Day -- a NOSTR bot mode (`--badpost`, one-shot preview `--badpost-print`).

People vote from a post's ⋯ menu in the web client ("Vote Bad Post of the Day"): a NIP-32 label,
kind 1985 in the `pcai.badpost` namespace, published to this node's relay. The day is cut into
WINDOWS of `BADPOST_INTERVAL_HOURS` (default 4, Admin → Bots), aligned to local midnight (00-04,
04-08, ... node-local time). When a window closes, the bot counts the votes CAST IN THAT WINDOW (one
per voter per post), picks the post with the most, and announces it: a screenshot of the post (the
app's own card renderer, via /api/community/badpost-card), PosterChan facepalming
(static/mascot/badpost-facepalm.png), the author tagged, and a roast of the post by this node's model.

The rules, each of which is a way this goes wrong silently:
  * a vote counts only in the window it was cast in, and a window with no votes posts NOTHING.
  * "could not ask the relay" is never "no votes": the window is NOT marked done, so the next minute
    asks again -- a window is retried until it can be read (or until it is a day old: an outage
    must not end in a burst of stale announcements).
  * a post is announced at most ONCE, ever. It is written down BEFORE the publish: a crash between
    the two loses one announcement, where the other order would repeat it.
  * the HEADLINE (who won, how many votes) and the author tag are written by code; the model writes
    only the roast under it, and a roast that names anybody, uses a slur, touches a protected trait
    or threatens anyone is thrown away for a canned one (`roast_ok`). Mean about the POST, never
    about who wrote it.
  * it publishes to this node's LOCAL relay only (botframework/nostr.py `_RELAYS`).
  * nothing a user wrote is logged -- ids and sizes only.
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

import community_api
from ai import generate_reply
from block_wording import NAMEISH
from config import OPENAI_ENDPOINT

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DEFAULT_INTERVAL_HOURS = 4
MAX_CATCH_UP = 86400           # a window that closed more than a day ago is never announced late
FACEPALM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "static", "mascot", "badpost-facepalm.png")


def interval_hours() -> int:
    """`BADPOST_INTERVAL_HOURS` (the bot's Admin → Bots setting), 1-24, default 4."""
    try:
        h = int(float(os.getenv("BADPOST_INTERVAL_HOURS") or DEFAULT_INTERVAL_HOURS))
    except ValueError:
        h = DEFAULT_INTERVAL_HOURS
    return min(24, max(1, h))


ROAST_PROMPT = (
    "You host a comedy roast. This community voted the post below 'Bad Post of the Day'. Write a "
    "short, mean, funny roast of THE POST ITSELF -- its take, its writing, its logic, its vibe -- in at "
    "most three sentences, in the voice of a disappointed announcer handing out a booby prize. Rules: "
    "roast the post, never the person; no slurs or insults about race, ethnicity, religion, gender, "
    "sexuality, disability, nationality, age or appearance; no threats, violence or self-harm jokes; "
    "do not write any usernames, @handles, npubs, nostr: links, URLs or domains; do not repeat the "
    "post. Respond with the roast only.\n\nThe post:\n\"\"\"{post}\"\"\" /no_think")

# Used when the model is off, says nothing, or says something the guard refuses.
FALLBACK_ROASTS = (
    "The community has spoken, and what it said was 'why'. 🤦",
    "Somewhere a keyboard is filing a complaint about what it was made to type.",
    "This post had one job, and it called in sick.",
    "Scientists are still trying to work out what this post was for. The early results are not kind.",
    "Not every thought needs to leave the building. This one should have been escorted back in.",
    "A bold take, in the sense that it boldly went nowhere.",
)

# ---- the guard -----------------------------------------------------------------------------------
# A roast that matches any of these is not posted. Deliberately broad: a false refusal costs a
# canned roast, a false pass costs the community a slur or a threat with this node's name on it.
_SLURS = re.compile(
    r"n[i1!|][gq]{2}|f[a@4]gg?[o0e]t|\bf[a@4]g\b|r[e3]t[a@4]rd|tr[a@4]nn(?:y|ie)|\bk[i1]ke|\bsp[i1]c\b|"
    r"\bch[i1]nk|wetback|towel\s*head|\bdyke|\bc[u*]nt|\bwh[o0]re|\bsl[u*]t|\btr[a@4]p\b|\bgypp?ed\b", re.I)
_TRAITS = re.compile(
    r"\b(?:race|racial|racis\w*|ethnic\w*|religio\w*|jew\w*|muslim\w*|islam\w*|christian\w*|hindu\w*|"
    r"buddhis\w*|atheis\w*|black|white|brown|asian\w*|african\w*|latin[oax]\w*|hispanic\w*|mexican\w*|"
    r"arab\w*|gay|lesbian\w*|homo\w*|trans|transgender\w*|queer\w*|bisexual\w*|sexuality|gender\w*|"
    r"women|woman|girl\w*|disab\w*|crippl\w*|autis\w*|downs?\s+syndrome|wheelchair\w*|blind|deaf|"
    r"immigra\w*|foreign\w*|nationality|ugly|fat|obese|old\s+(?:man|woman|lady|geezer)|boomer\w*)\b", re.I)
_THREATS = re.compile(
    r"\b(?:kill\w*|murder\w*|shoot\w*|stab\w*|die|dies|died|dead|death|kys|hang\s+yourself|"
    r"hurt\s+you|beat\s+you|rape\w*|suicid\w*|end\s+(?:it|yourself)|bomb\w*|doxx?\w*|"
    r"find\s+where\s+you\s+live)\b", re.I)
_CJK = re.compile(r"[一-鿿぀-ゟ゠-ヿ가-힯]")
MAX_ROAST = 500


def roast_ok(text: str) -> bool:
    """True if the model's roast may be posted under the headline."""
    t = (text or "").strip()
    if not t or len(t) > MAX_ROAST:
        return False
    return not (NAMEISH.search(t) or _SLURS.search(t) or _TRAITS.search(t) or _THREATS.search(t)
                or _CJK.search(t))


def _clean(text: str) -> str:
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    t = t.replace("/no_think", "").strip().strip('"').strip()
    return re.sub(r"\n{3,}", "\n\n", t)


def _ai_on() -> bool:
    return bool(OPENAI_ENDPOINT and OPENAI_ENDPOINT.startswith(("http://", "https://")))


def fallback_roast(post_id: str) -> str:
    return FALLBACK_ROASTS[int(hashlib.sha256((post_id or "").encode()).hexdigest(), 16) % len(FALLBACK_ROASTS)]


def roast(post: dict) -> str:
    """The model's roast of the post if it passes the guard, else a canned one."""
    if _ai_on():
        try:
            got = _clean(generate_reply(ROAST_PROMPT.format(post=(post.get("text") or "")[:1200])) or "")
            if roast_ok(got):
                return got
            logging.warning(f"[BADPOST] the model's roast ({len(got)} chars) failed the guard; using a canned one")
        except Exception as e:
            logging.warning(f"[BADPOST] AI roast failed ({type(e).__name__}); using a canned one")
    return fallback_roast(post.get("id") or "")


def compose(post: dict, votes: int, roast_text: str) -> str:
    """The announcement. The author is TAGGED (`nostr:npub…`, and post_note adds the matching `p`
    tag), so they are notified of their prize; the post itself is linked by its `nostr:note…`."""
    n = f"{votes} vote" + ("" if votes == 1 else "s")
    return (f"🤦 BAD POST OF THE DAY 🤦\n\n"
            f"Congratulations {post['ref']}, you've won Bad Post of the Day with {n}! 🏆\n\n"
            f"{roast_text}\n\n{post['note_ref']}")


# ---- the schedule and the count ------------------------------------------------------------------

def window_of(t: datetime.datetime, hours: int) -> tuple:
    """(start, end) of the window holding local time `t`: windows of `hours` from local midnight; the
    day's last one ends at midnight when `hours` does not divide 24."""
    midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
    k = int((t - midnight).total_seconds() // (hours * 3600))
    start = midnight + datetime.timedelta(hours=k * hours)
    return start, min(start + datetime.timedelta(hours=hours), midnight + datetime.timedelta(days=1))


def due_windows(now: datetime.datetime, done_until: float | None, hours: int) -> list:
    """The CLOSED windows not yet announced, oldest first, as (start, end) local datetimes. With no
    memory, only the window that just closed (switching the bot on does not announce the past day)."""
    out = []
    end = window_of(now, hours)[0]                       # the current window starts where the last ended
    oldest = now - datetime.timedelta(seconds=MAX_CATCH_UP)
    while end > oldest:
        if done_until is not None and end.timestamp() <= done_until:
            break
        start = window_of(end - datetime.timedelta(seconds=1), hours)[0]
        out.append((start, end))
        if done_until is None:
            break
        end = start
    return out[::-1]


def tally(votes: list, start: float, end: float, announced=()) -> list:
    """[{post, author, votes, first}] ranked worst first, from the votes CAST IN [start, end): one per
    voter per post, a vote for your own post is not a vote, and a post already announced is never a
    candidate again. Ties go to the post that was voted on first."""
    seen, counts = set(), {}
    for v in votes or []:
        post, voter, at = v.get("post") or "", v.get("voter") or "", int(v.get("at") or 0)
        if not post or not voter or post in announced or voter == v.get("author"):
            continue
        if not (start <= at < end) or (voter, post) in seen:
            continue
        seen.add((voter, post))
        c = counts.setdefault(post, {"post": post, "author": v.get("author") or "", "votes": 0, "first": at})
        c["votes"] += 1
        c["first"] = min(c["first"], at)
    return sorted(counts.values(), key=lambda c: (-c["votes"], c["first"], c["post"]))


# ---- state (a file beside the bot, one per bot key) ----------------------------------------------

def _state_path() -> str:
    tag = hashlib.sha256((os.getenv("NOSTR_NSEC") or "default").encode()).hexdigest()[:12]
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), f".last_badpost_{tag}.json")


def _load() -> dict:
    try:
        with open(_state_path()) as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    tmp = _state_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, _state_path())


# ---- publishing ----------------------------------------------------------------------------------

def _facepalm():
    try:
        with open(FACEPALM, "rb") as f:
            return (f.read(), "image/png")
    except OSError:
        logging.warning("[BADPOST] the facepalm graphic is missing; posting without it")
        return None


def _post(text: str, images: list) -> dict:
    """Sign and publish to the LOCAL relay (nostr._RELAYS). Raises on failure."""
    import nostr
    if not nostr._SECKEY:
        raise RuntimeError("NOSTR_NSEC not configured")
    return nostr._run(nostr._svc.post_note(nostr._SECKEY, nostr._RELAYS, text, media_list=images,
                                           media_cfg=nostr._MEDIA_CFG))


def announce(start: float, end: float, print_only: bool = False) -> str | None:
    """Announce the worst post of the window [start, end). Returns its id, or None when nobody voted.
    Raises community_api.Unavailable when the relay could not be asked -- the caller then leaves the
    window open to be retried."""
    state = _load()
    announced = dict(state.get("announced") or {})
    try:
        votes = community_api.get("badpost-votes", since=int(start), until=int(end))["votes"]
    except (KeyError, TypeError) as e:
        raise community_api.Unavailable(f"badpost-votes: unexpected answer ({type(e).__name__})")
    ranked = tally(votes, start, end, announced)
    logging.info(f"[BADPOST] window {int(start)}-{int(end)}: {len(votes)} vote(s), {len(ranked)} candidate(s)")
    winner = card = None
    for cand in ranked[:5]:
        got = community_api.get("badpost-card", id=cand["post"])      # Unavailable propagates
        if got.get("post"):
            winner, card = cand, got
            break
        announced[cand["post"]] = int(time.time())    # gone from the relay: never a candidate again
    if not winner:
        if not print_only and len(announced) != len(state.get("announced") or {}):
            state["announced"] = announced
            _save(state)
        return None
    post = card["post"]
    msg = compose(post, winner["votes"], roast(post))
    if print_only:
        print(msg)
        return post["id"]
    announced[post["id"]] = int(time.time())
    state["announced"] = announced
    _save(state)                                    # BEFORE the publish: once, never twice
    images = []
    if card.get("card"):
        import base64
        images.append((base64.b64decode(card["card"]), card.get("content_type") or "image/png"))
    fp = _facepalm()
    if fp:
        images.append(fp)
    try:
        ev = _post(msg, images)
        logging.info(f"[BADPOST] announced post {post['id'][:12]} ({winner['votes']} votes) as "
                     f"{(ev or {}).get('id', '?')[:12]}, {len(images)} image(s)")
    except Exception as e:
        logging.error(f"[BADPOST] publishing the announcement failed: {type(e).__name__}: {e}")
    return post["id"]


_busy = threading.Lock()


def tick(now: datetime.datetime | None = None) -> None:
    """Announce every closed window not yet done, oldest first. A window whose votes could not be
    read is NOT marked done, so the next minute asks again. One pass at a time: a slow pass (a card
    render, the model) still running when the next minute ticks must not start a second one."""
    if not _busy.acquire(blocking=False):
        return
    try:
        now = now or datetime.datetime.now()
        for start, end in due_windows(now, _load().get("done_until"), interval_hours()):
            try:
                announce(start.timestamp(), end.timestamp())
            except community_api.Unavailable as e:
                logging.warning(f"[BADPOST] could not read the votes, retrying this window shortly: {e}")
                return                              # never mistake "could not ask" for "no votes"
            state = _load()
            state["done_until"] = end.timestamp()
            _save(state)
    finally:
        _busy.release()


def preview() -> None:
    """`main.py --badpost-print`: what the CURRENT window would announce if it closed now; posts nothing."""
    now = datetime.datetime.now()
    start = window_of(now, interval_hours())[0]
    try:
        if not announce(start.timestamp(), now.timestamp() + 1, print_only=True):
            print("No Bad Post votes in this window yet -- nothing would be posted.")
    except community_api.Unavailable as e:
        print(f"could not read the Bad Post votes: {e}")


def waitToStart():
    while datetime.datetime.now().second != 0:
        time.sleep(0.5)


def background():
    print(f"Running the Bad Post of the Day bot (announces every {interval_hours()}h, local time)")
    while True:
        t = threading.Thread(target=tick)
        t.start()
        t.join(timeout=600)
        time.sleep(60)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"daemon": lambda: (waitToStart(), background()),
     "preview": preview}.get(cmd, lambda: print("Usage: nostr_badpostbot.py daemon | preview"))()
