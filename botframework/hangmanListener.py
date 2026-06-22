"""#hangman — guess-the-word over Nostr, refereed by the bot. The bot picks a random word; the
guesser replies with single LETTERS (or the whole word). START with "hangman" (you guess) or
"hangman @opponent" (they guess); both watch. The secret word is stored NIP-44 self-encrypted in the
game state so it's NOT visible in the public kind-30078 doc — only the bot can read it to score
guesses. Mirrors the chess/ttt bots (separate files, kind-30078 state, post-text footer)."""
import os
import re
import sys
import json
import time
import fcntl
import hashlib
import secrets
import tempfile

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import hangman_render
import nostr as _nk
from config import NOSTR_NSEC
from app.services.nostr import event as _ev, nip44 as _nip44

_KIND_APP = 30078
_MAX_WRONG = 6
_START_RE = re.compile(r"\bhang\s*man\b", re.IGNORECASE)
_LETTER_RE = re.compile(r"[a-zA-Z]")
_NOSTR_TOKEN_RE = re.compile(
    r"nostr:[a-z0-9]+|\b(?:npub1|nprofile1|nevent1|note1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]+",
    re.IGNORECASE)
_LOOKBACK_DAYS = int(os.getenv("HANGMAN_LOOKBACK_DAYS", "3"))
_INVITE_MAX = int(os.getenv("HANGMAN_INVITE_MAX_PER_HOUR", "3"))
_INVITE_WINDOW = 3600
_invite_times: dict = {}

_WORDS = [
    "nostr", "relay", "satoshi", "lightning", "cyberpunk", "decentralized", "protocol", "keypair",
    "blossom", "zaps", "freedom", "privacy", "npub", "signature", "gossip", "mesh", "encryption",
    "firewall", "neon", "matrix", "android", "terminal", "hacker", "uplink", "datastream", "android",
    "phantom", "synthwave", "override", "mainframe", "darknet", "glitch", "avatar", "hologram",
    "quantum", "neural", "android", "circuit", "protocol", "binary", "vaporwave", "render",
]


def _pick_word():
    return _WORDS[secrets.randbelow(len(_WORDS))].lower()


# ---- dedup ----------------------------------------------------------------
def _suffix():
    return hashlib.sha1((NOSTR_NSEC or "").encode()).hexdigest()[:10] if NOSTR_NSEC else "default"


_IDS_FILE = os.path.join(script_dir, f".processed_hangman_ids_{_suffix()}")
_LOCK_FILE = _IDS_FILE + ".lock"


def _claim(note_id):
    try:
        with open(_LOCK_FILE, "w") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            try:
                ids = set()
                try:
                    with open(_IDS_FILE) as f:
                        ids = {ln.strip() for ln in f if ln.strip()}
                except FileNotFoundError:
                    pass
                if note_id in ids:
                    return False
                ids.add(note_id)
                if len(ids) > 5000:
                    ids = set(sorted(ids)[-5000:])
                fd, tmp = tempfile.mkstemp(dir=script_dir, prefix=".hmids_")
                with os.fdopen(fd, "w") as f:
                    f.write("\n".join(ids))
                os.replace(tmp, _IDS_FILE)
                return True
            finally:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"[hangman] claim failed: {e}", flush=True)
        return False


# ---- state ----------------------------------------------------------------
def _dtag(gameid):
    return f"pcai:hangman:{gameid}"


def _save_game(gameid, state):
    ev = _ev.build_event(_nk._SECKEY, _KIND_APP, json.dumps(state, separators=(",", ":")),
                         tags=[["d", _dtag(gameid)]])
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))


def _load_game(gameid):
    try:
        evs = _nk._run(_nk._svc.relay.query(
            _nk._RELAYS, [{"authors": [_nk._PUBKEY], "kinds": [_KIND_APP], "#d": [_dtag(gameid)], "limit": 1}])) or []
    except Exception:
        return None
    if not evs:
        return None
    evs.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    try:
        return json.loads(evs[0].get("content") or "{}")
    except Exception:
        return None


def _word_of(state):
    """Decrypt the secret word (bot-only)."""
    try:
        return _nip44.decrypt_self(_nk._SECKEY, state["word_enc"])
    except Exception:
        return ""


def _display(word, guessed):
    return " ".join(c.upper() if c in guessed else "_" for c in word)


# ---- nostr helpers --------------------------------------------------------
def _tags(note):
    return (note.get("_event") or {}).get("tags", [])


def _root_id(note):
    es = [t for t in _tags(note) if len(t) >= 2 and t[0] == "e"]
    for t in es:
        if len(t) >= 4 and t[3] == "root":
            return t[1]
    return es[0][1] if es else None


def _ptags(note):
    return [t[1] for t in _tags(note) if len(t) >= 2 and t[0] == "p" and t[1]]


def _name(pk):
    try:
        return "@" + (_nk.resolve_user(pk).get("username") or _nk._short_npub(pk))
    except Exception:
        return "@" + (pk or "")[:8]


def _clean_text(note):
    t = _NOSTR_TOKEN_RE.sub("", note.get("text") or "")
    t = re.sub(r"@[\w@.]+", "", t)
    t = re.sub(r"#\w+", "", t)
    for line in t.splitlines():
        if line.strip():
            return line.strip()
    return t.strip()


def _footer():
    site = (os.getenv("CHESS_SITE_URL", "") or "").strip()
    play = f"\nPlay interactively at {site}." if site else ""
    return ("🎯 Wanna play your own? Reply \"hangman\" to start a game (I'll pick a word); "
            "then reply with a letter A-Z." + play + "\n#hangman #nostr #gamestr")


def _publish(gameid, parent_id, players, body, png, federate=True):
    info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
    url = info.get("url")
    if not url:
        raise RuntimeError("image upload failed")
    content = f"{body}\n{url}\n\n{_footer()}"
    tags = [["e", gameid, "", "root"]]
    if parent_id and parent_id != gameid:
        tags.append(["e", parent_id, "", "reply"])
    for pk in players:
        if pk:
            tags.append(["p", pk])
    for _t in ("hangman", "nostr", "gamestr"):
        tags.append(["t", _t])
    if not federate:   # mid-game boards stay local-only (only opening + final go public)
        tags.append(["nofederate", "1"])
    tags.append(_ev.imeta_tag(url, "image/png", info.get("sha256", ""), info.get("dim", "")))
    ev = _ev.build_event(_nk._SECKEY, 1, content, tags=tags)
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))
    return ev


def _reply_text(note, text):
    try:
        _nk.send_reply(note, text + "\n\n#hangman #nostr #gamestr")
    except Exception as e:
        print(f"[hangman] reply failed: {e}", flush=True)


def _post(state, gameid, parent_id, word=None, gameover=False, result=""):
    guessed = set(state["guessed"])
    # On game over, reveal the whole word on the board; otherwise show the current masked display.
    disp = _display(word, set(word)) if (word and gameover) else (_display(word, guessed) if word else state.get("display", ""))
    title = "GAME OVER" if gameover else f"{state['guesser_name']} — guess a letter"
    sub = result if gameover else f"Reply with a letter A-Z (or the whole word)."
    png = hangman_render.render(disp, state.get("wrong_letters", []), state.get("wrong", 0),
                                title=title, subtitle=sub)
    if gameover:
        body = f"🏁 {result}"
    else:
        body = (f"🎯 #hangman — {state['guesser_name']} to guess: {disp}\n"
                f"Misses {state.get('wrong', 0)}/{_MAX_WRONG}"
                + (f" · wrong: {' '.join(state['wrong_letters'])}" if state.get("wrong_letters") else "")
                + ". Reply with a letter A-Z.")
    # Opening (parent==gameid) and the final post are public; mid-game guesses stay local-only.
    federate = gameover or (parent_id == gameid)
    ev = _publish(gameid, parent_id, [state["guesser"], state.get("opponent")], body, png, federate=federate)
    state["last_board_event"] = ev.get("id")
    _save_game(gameid, state)


# ---- start + guess --------------------------------------------------------
def _start_game(note, own_pk):
    sender = (note.get("user") or {}).get("pubkey")
    opponents = [p for p in _ptags(note) if p and p != own_pk and p != sender]
    gameid = note["id"]
    if _load_game(gameid):
        return
    now = time.time()
    recent = [t for t in _invite_times.get(sender, []) if now - t < _INVITE_WINDOW]
    if _INVITE_MAX and len(recent) >= _INVITE_MAX:
        _reply_text(note, f"⏳ You've started {_INVITE_MAX} games in the last hour — that's the limit.")
        return
    recent.append(now)
    _invite_times[sender] = recent
    guesser = opponents[0] if opponents else sender   # invited player guesses; else the sender
    word = _pick_word()
    state = {
        "v": 1, "guesser": guesser, "guesser_name": _name(guesser),
        "opponent": (sender if opponents else None),
        "word_enc": _nip44.encrypt_self(_nk._SECKEY, word),
        "wordlen": len(word), "guessed": [], "wrong_letters": [], "wrong": 0,
        "display": " ".join("_" for _ in word), "status": "active",
        "root": gameid, "started": int(time.time()), "last_board_event": None,
    }
    print(f"[hangman] new game {gameid[:12]} guesser={state['guesser_name']} word={word}", flush=True)
    _post(state, gameid, gameid, word=word)


def _handle_guess(note, gameid, state):
    sender = (note.get("user") or {}).get("pubkey")
    if sender != state["guesser"]:
        return  # only the guesser plays
    if state.get("status") != "active":
        _reply_text(note, "🏁 This game is over. Start a new one with \"hangman\".")
        return
    text = _clean_text(note)
    low = text.lower().strip()
    if low in ("resign", "quit", "give up", "abandon"):
        word = _word_of(state)
        state["status"] = "lost"
        _post(state, gameid, note["id"], word=word, gameover=True, result=f"Gave up. The word was: {word.upper()}")
        return
    word = _word_of(state)
    if not word:
        _reply_text(note, "⚠️ Couldn't read this game's word — it may be corrupt. Start a new one.")
        return
    # whole-word guess
    if len(low) > 1 and low.isalpha():
        if low == word:
            for c in word:
                if c not in state["guessed"]:
                    state["guessed"].append(c)
            state["status"] = "won"
            _post(state, gameid, note["id"], word=word, gameover=True,
                  result=f"🎉 {state['guesser_name']} guessed it: {word.upper()}!")
        else:
            state["wrong"] += 1   # a wrong whole-word guess costs a miss (don't pollute the letters list)
            _finish_or_continue(note, gameid, state, word)
        return
    m = _LETTER_RE.search(low)
    if not m:
        _reply_text(note, "🤔 Reply with a single letter A-Z (or the whole word).")
        return
    letter = m.group(0).lower()
    if letter in state["guessed"] or letter in state["wrong_letters"]:
        _reply_text(note, f"↩️ You already tried '{letter.upper()}'. Try another.")
        return
    if letter in word:
        state["guessed"].append(letter)
    else:
        state["wrong"] += 1
        state["wrong_letters"].append(letter)
    _finish_or_continue(note, gameid, state, word)


def _finish_or_continue(note, gameid, state, word):
    state["display"] = _display(word, set(state["guessed"]))
    if all(c in state["guessed"] for c in word):
        state["status"] = "won"
        _post(state, gameid, note["id"], word=word, gameover=True,
              result=f"🎉 {state['guesser_name']} solved it: {word.upper()}!")
    elif state["wrong"] >= _MAX_WRONG:
        state["status"] = "lost"
        _post(state, gameid, note["id"], word=word, gameover=True,
              result=f"💀 Out of guesses! The word was: {word.upper()}")
    else:
        _post(state, gameid, note["id"], word=word)


def process_hangman():
    own = _nk.get_own_account()
    if not own:
        print("[hangman] no account (NOSTR_NSEC missing) — idle", flush=True)
        return
    own_pk = own.get("pubkey")
    cutoff = int(time.time()) - _LOOKBACK_DAYS * 86400
    for note in _nk.get_mentions(limit=40):
        nid = note.get("id")
        if not nid or (note.get("user") or {}).get("pubkey") == own_pk:
            continue
        if (note.get("_event") or {}).get("created_at", 0) < cutoff:
            continue
        root = _root_id(note)
        state = _load_game(root) if root else None
        is_start = bool(_START_RE.search(note.get("text") or "")) and not state
        if not state and not is_start:
            continue
        if not _claim(nid):
            continue
        try:
            if state:
                _handle_guess(note, root, state)
            else:
                _start_game(note, own_pk)
        except Exception as e:
            print(f"[hangman] processing {nid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
