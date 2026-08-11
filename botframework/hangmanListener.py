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
# Must name THIS game — a bare "start" used to match, but every game listener shares the one bot
# identity, so "start connect4" fired hangman too. The app always posts the #hangman tag.
_START_RE = re.compile(r"\b(?:hang\s*man|hangman)\b", re.IGNORECASE)
# An app-embedded game pointer inside a DM ("g:<64-hex-root>"); bare human DM replies omit it and
# fall back to the per-player pending-game pointer.
_DM_GAME_RE = re.compile(r"\bg:([0-9a-f]{64})\b", re.IGNORECASE)
_LETTER_RE = re.compile(r"[a-zA-Z]")
_NOSTR_TOKEN_RE = re.compile(
    r"nostr:[a-z0-9]+|\b(?:npub1|nprofile1|nevent1|note1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]+",
    re.IGNORECASE)
_LOOKBACK_DAYS = int(os.getenv("HANGMAN_LOOKBACK_DAYS", "3"))
_INVITE_MAX = int(os.getenv("HANGMAN_INVITE_MAX_PER_HOUR", "3"))
_INVITE_WINDOW = 3600
_invite_times: dict = {}

# Friend-friendly words grouped by category. The category doubles as the built-in HINT for solo /
# random games, so it plays like real hangman with a clue — not a tech-vocab quiz. Common spelling,
# no proper nouns, nothing Nostr/crypto-specific.
_WORD_CATEGORIES = {
    "an animal": ["elephant", "giraffe", "penguin", "dolphin", "kangaroo", "butterfly", "octopus",
                  "squirrel", "hedgehog", "flamingo", "cheetah", "raccoon", "rabbit", "turtle", "otter"],
    "a food": ["pizza", "spaghetti", "chocolate", "pancake", "avocado", "burrito", "popcorn",
               "watermelon", "strawberry", "cinnamon", "pretzel", "noodle", "muffin", "pickle"],
    "a place": ["mountain", "island", "volcano", "desert", "waterfall", "rainforest", "lighthouse",
                "village", "harbor", "meadow", "canyon", "glacier", "castle"],
    "something at home": ["umbrella", "blanket", "pillow", "lantern", "telescope", "backpack",
                          "mirror", "candle", "kettle", "ladder", "drawer", "basket"],
    "a sport or hobby": ["basketball", "swimming", "surfing", "skateboard", "volleyball", "painting",
                         "camping", "fishing", "dancing", "gardening", "bowling"],
    "weather or nature": ["rainbow", "thunder", "snowflake", "sunshine", "blossom", "breeze",
                          "lightning", "river", "forest", "meadow"],
}
_WORDS_FLAT = [(w, cat) for cat, ws in _WORD_CATEGORIES.items() for w in ws]


def _pick_word():
    """Return (word, hint) where the hint is the word's category — the built-in clue for random games."""
    return _WORDS_FLAT[secrets.randbelow(len(_WORDS_FLAT))]


# ---- dedup ----------------------------------------------------------------
def _suffix():
    return hashlib.sha1((NOSTR_NSEC or "").encode()).hexdigest()[:10] if NOSTR_NSEC else "default"


_IDS_FILE = os.path.join(script_dir, f".processed_hangman_ids_{_suffix()}")
_LOCK_FILE = _IDS_FILE + ".lock"
_DM_IDS_FILE = os.path.join(script_dir, f".processed_hangman_dms_{_suffix()}")
_MAX_IDS = 5000


def _claim_in(ids_file, item_id):
    lock_file = ids_file + ".lock"
    try:
        with open(lock_file, "w") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            try:
                ids = set()
                try:
                    with open(ids_file) as f:
                        ids = {ln.strip() for ln in f if ln.strip()}
                except FileNotFoundError:
                    pass
                if item_id in ids:
                    return False
                ids.add(item_id)
                if len(ids) > _MAX_IDS:
                    ids = set(sorted(ids)[-_MAX_IDS:])
                fd, tmp = tempfile.mkstemp(dir=script_dir, prefix=".hmids_")
                with os.fdopen(fd, "w") as f:
                    f.write("\n".join(ids))
                os.replace(tmp, ids_file)
                return True
            finally:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"[hangman] claim failed: {e}", flush=True)
        return False


def _claim_dm(rumor_id):
    return _claim_in(_DM_IDS_FILE, rumor_id)


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
    return _load_doc(_dtag(gameid))


def _load_doc(dtag):
    try:
        evs = _nk._run(_nk._svc.relay.query(
            _nk._RELAYS, [{"authors": [_nk._PUBKEY], "kinds": [_KIND_APP], "#d": [dtag], "limit": 1}])) or []
    except Exception:
        return None
    if not evs:
        return None
    evs.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    try:
        return json.loads(evs[0].get("content") or "{}")
    except Exception:
        return None


# Per-player pointer to their current game — lets a bare DM reply (no app marker) route back to the
# player's pending game. Keyed by pubkey.
def _player_dtag(pk):
    return f"pcai:hangman:player:{pk}"


def _get_player_game(pk):
    doc = _load_doc(_player_dtag(pk))
    return doc.get("gameid") if isinstance(doc, dict) else None


def _set_player_game(pk, gameid):
    ev = _ev.build_event(_nk._SECKEY, _KIND_APP, json.dumps({"gameid": gameid}, separators=(",", ":")),
                         tags=[["d", _player_dtag(pk)]])
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))


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


def _clean_dm_text(text):
    """Parse a guess DM → (gameid_or_None, move_text). Strips the app's 'g:<root>' marker, nostr
    tokens and hashtags; the guess is the first non-empty line."""
    m = _DM_GAME_RE.search(text or "")
    gameid = m.group(1).lower() if m else None
    t = _DM_GAME_RE.sub("", text or "")
    t = _NOSTR_TOKEN_RE.sub("", t)
    t = re.sub(r"#\w+", "", t)
    for line in t.splitlines():
        if line.strip():
            return gameid, line.strip()
    return gameid, t.strip()


def _footer():
    site = (os.getenv("CHESS_SITE_URL", "") or "").strip()
    play = f"\nPlay interactively at {site}." if site else ""
    return ("🎯 Wanna play your own? Mention me with \"start\" to play (or \"start @friend\" to make "
            "them guess); I'll pick a word and DM you the word to guess, one letter at a time." + play
            + "\n#hangman #nostr #gamestr")


def _publish(gameid, parent_id, players, body, png, federate=True):
    info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
    url = info.get("url")
    if not url:
        raise RuntimeError("image upload failed")
    # Real mentions, not bare @handles: a p-tag notifies but renders as plain text, so a result
    # post read "@npub1mq3s439… wins" — unrendered AND truncated. See _nk.mentionify.
    content = _nk.mentionify(f"{body}\n{url}\n\n{_footer()}", players, _name)
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


def _dm_current_player(state, gameid):
    """DM the masked word + gallows to the GUESSER (private gameplay). In hangman the guesser is
    essentially always the player to act until the game ends. No-op if the guesser is the bot.
    Sets the per-player pending-game pointer so a bare DM reply (no app marker) routes back here."""
    guesser = state.get("guesser")
    if not guesser or guesser == _nk._PUBKEY:
        return
    word = _word_of(state)
    disp = _display(word, set(state["guessed"])) if word else state.get("display", "")
    wrong = state.get("wrong", 0)
    title = "YOUR GUESS"
    sub = f"Misses {wrong}/{_MAX_WRONG}" + (f" · wrong: {' '.join(state['wrong_letters'])}"
                                            if state.get("wrong_letters") else "")
    try:
        png = hangman_render.render(disp, state.get("wrong_letters", []), wrong, title=title, subtitle=sub)
        info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
        url = info.get("url") or ""
    except Exception as e:
        print(f"[hangman] DM board upload failed: {e}", flush=True)
        url = ""
    _hint = state.get("hint", "")
    body = (f"🎯 Your word to guess: {disp}\n"
            + (f"💡 Clue: {_hint}\n" if _hint else "")
            + f"Misses {wrong}/{_MAX_WRONG}"
            + (f" · wrong: {' '.join(state['wrong_letters'])}" if state.get("wrong_letters") else "") + "\n"
            + (url + "\n\n" if url else "")
            + "Reply to this DM with a letter A-Z (or the whole word). Or play from the Hangman tab in the app.")
    try:
        _nk.send_dm(guesser, body, extra_tags=[["g", gameid]])
        _set_player_game(guesser, gameid)
    except Exception as e:
        print(f"[hangman] send_dm failed: {e}", flush=True)


def _post(state, gameid, parent_id, word=None, gameover=False, result=""):
    # MID-GAME guess: NO public post — persist + DM the guesser the updated word/gallows privately.
    if not gameover and parent_id != gameid:
        _save_game(gameid, state)
        _dm_current_player(state, gameid)
        return
    guessed = set(state["guessed"])
    # On game over, reveal the whole word on the board; otherwise show the current masked display.
    disp = _display(word, set(word)) if (word and gameover) else (_display(word, guessed) if word else state.get("display", ""))
    title = "GAME OVER" if gameover else f"{state['guesser_name']} — guess a letter"
    sub = result if gameover else f"Check your DMs — I've sent you the word to guess."
    png = hangman_render.render(disp, state.get("wrong_letters", []), state.get("wrong", 0),
                                title=title, subtitle=sub)
    if gameover:
        # Record outcome on the state so the web client can render a clear WIN/LOSS banner.
        # WON → the guesser beat the word; LOST/resigned → the word/bot won (no winner pk).
        won = state.get("status") == "won"
        state["result"] = result
        state["winner_pk"] = state.get("guesser") if won else None
        state["winner_name"] = state.get("guesser_name") if won else None
        body = f"🏁 {result}"
    else:
        # OPENING invitation — public; the game then plays out privately in DMs.
        guesser_is_sender = not state.get("opponent")
        _hint = state.get("hint", "")
        _clue = f"💡 Clue: {_hint}\n" if _hint else ""
        if guesser_is_sender:
            body = (f"🎯 #hangman — {state['guesser_name']} is guessing a {state.get('wordlen','?')}-letter word!\n"
                    + _clue +
                    f"📩 Check your DMs — I've sent you the word to guess (a letter at a time). "
                    f"The result gets posted here. Cheer them on! 🙌")
        else:
            whose = (state.get("setter_name") + "'s") if state.get("setter") else "the bot's"
            body = (f"🎯 #hangman — {state['guesser_name']} has been challenged to guess {whose} word "
                    f"({state.get('wordlen','?')} letters)!\n"
                    + _clue +
                    f"📩 {state['guesser_name']}, check your DMs to start guessing. Follow along here — "
                    f"react and cheer! I'll post the result when it's over.")
    # The opening invitation and the final result are public; mid-game guesses are DM-only.
    ev = _publish(gameid, parent_id, [state["guesser"], state.get("opponent")], body, png, federate=True)
    state["last_board_event"] = ev.get("id")
    _save_game(gameid, state)
    if not gameover:
        _dm_current_player(state, gameid)


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
    if opponents:
        # CHALLENGE: the SETTER (sender) picks the secret word their friend (the guesser) must guess.
        # We DM the setter privately for the word; the game stays "awaiting_word" until they reply.
        guesser = opponents[0]
        state = {
            "v": 1, "guesser": guesser, "guesser_name": _name(guesser),
            "opponent": sender, "setter": sender, "setter_name": _name(sender),
            "guessed": [], "wrong_letters": [], "wrong": 0,
            "status": "awaiting_word", "root": gameid, "started": int(time.time()), "last_board_event": None,
        }
        print(f"[hangman] new game {gameid[:12]} {state['setter_name']} sets word for {state['guesser_name']}", flush=True)
        _save_game(gameid, state)
        _set_player_game(sender, gameid)   # so the setter's bare DM reply routes back to this game
        try:
            _nk.send_dm(sender, f"🎯 You challenged {state['guesser_name']} to #hangman!\n"
                        "Reply to THIS DM with the secret WORD you want them to guess (letters only) — "
                        "or reply 'random' and I'll pick one.\n"
                        "💡 Give them a clue too? Send it as `word | your hint` "
                        "(e.g. `pancake | breakfast favorite`).", extra_tags=[["g", gameid]])
        except Exception as e:
            print(f"[hangman] setter prompt DM failed: {e}", flush=True)
        return
    # SOLO: no opponent → you guess the bot's randomly-picked word. The category is your clue.
    guesser = sender
    word, hint = _pick_word()
    state = {
        "v": 1, "guesser": guesser, "guesser_name": _name(guesser),
        "opponent": None,
        "word_enc": _nip44.encrypt_self(_nk._SECKEY, word),
        "wordlen": len(word), "guessed": [], "wrong_letters": [], "wrong": 0,
        "hint": hint,
        "display": " ".join("_" for _ in word), "status": "active",
        "root": gameid, "started": int(time.time()), "last_board_event": None,
    }
    print(f"[hangman] new game {gameid[:12]} guesser={state['guesser_name']} word={word}", flush=True)
    _post(state, gameid, gameid, word=word)


def _set_secret_word(setter, gameid, state, text, reply):
    """The challenger replied with the word their friend must guess. Store it, activate the game,
    post the public opening + DM the guesser the puzzle."""
    raw = (text or "").strip()
    hint = ""
    if "|" in raw:                       # "word | a clue for your friend" — optional hint
        raw, hint = raw.split("|", 1)
    raw = raw.strip().lower()
    hint = hint.strip()[:80]
    if raw in ("random", "rand", "you pick", "bot", "surprise"):
        word, cat = _pick_word()
        if not hint:
            hint = cat                   # fall back to the random word's category as the clue
    else:
        word = re.sub(r"[^a-z]", "", raw)
        if len(word) < 3 or len(word) > 24:
            reply("🎯 Send ONE word (letters only, 3–24 chars) for your friend to guess — or reply "
                  "'random'. Want to give them a clue? Send it as `word | your hint`.")
            return
    state["word_enc"] = _nip44.encrypt_self(_nk._SECKEY, word)
    state["wordlen"] = len(word)
    state["display"] = " ".join("_" for _ in word)
    state["hint"] = hint
    state["status"] = "active"
    reply(f"✅ Secret word set ({len(word)} letters" + (f", clue “{hint}”" if hint else "")
          + f"). I've DM'd {state['guesser_name']} the puzzle — they're guessing now!")
    _post(state, gameid, gameid, word=word)   # public opening + DM the guesser


def _apply_move(sender, gameid, state, text, reply, parent_id):
    """Apply one guess from `sender`. `reply(msg)` sends a nudge/error on the same channel (public
    reply or DM). Game-over posts go to `parent_id` (public). Mid-game = no public post; the guesser
    is DM'd by _post → _dm_current_player."""
    if state.get("status") == "awaiting_word":
        if sender != state.get("setter"):
            return   # only the challenger sets the word
        _set_secret_word(sender, gameid, state, text, reply)
        return
    if sender != state["guesser"]:
        return  # only the guesser plays
    if state.get("status") != "active":
        reply("🏁 This game is over. Start a new one with \"start\".")
        return
    low = text.lower().strip()
    if low in ("resign", "quit", "give up", "abandon"):
        word = _word_of(state)
        state["status"] = "lost"
        _post(state, gameid, parent_id, word=word, gameover=True, result=f"Gave up. The word was: {word.upper()}")
        return
    word = _word_of(state)
    if not word:
        reply("⚠️ Couldn't read this game's word — it may be corrupt. Start a new one.")
        return
    # whole-word guess
    if len(low) > 1 and low.isalpha():
        if low == word:
            for c in word:
                if c not in state["guessed"]:
                    state["guessed"].append(c)
            state["status"] = "won"
            _post(state, gameid, parent_id, word=word, gameover=True,
                  result=f"🎉 {state['guesser_name']} guessed it: {word.upper()}!")
        else:
            state["wrong"] += 1   # a wrong whole-word guess costs a miss (don't pollute the letters list)
            _finish_or_continue(gameid, state, word, parent_id)
        return
    m = _LETTER_RE.search(low)
    if not m:
        reply("🤔 Reply with a single letter A-Z (or the whole word).")
        return
    letter = m.group(0).lower()
    if letter in state["guessed"] or letter in state["wrong_letters"]:
        reply(f"↩️ You already tried '{letter.upper()}'. Try another.")
        return
    if letter in word:
        state["guessed"].append(letter)
    else:
        state["wrong"] += 1
        state["wrong_letters"].append(letter)
    _finish_or_continue(gameid, state, word, parent_id)


def _finish_or_continue(gameid, state, word, parent_id):
    state["display"] = _display(word, set(state["guessed"]))
    if all(c in state["guessed"] for c in word):
        state["status"] = "won"
        _post(state, gameid, parent_id, word=word, gameover=True,
              result=f"🎉 {state['guesser_name']} solved it: {word.upper()}!")
    elif state["wrong"] >= _MAX_WRONG:
        state["status"] = "lost"
        _post(state, gameid, parent_id, word=word, gameover=True,
              result=f"💀 Out of guesses! The word was: {word.upper()}")
    else:
        _post(state, gameid, parent_id, word=word)


def _handle_move(note, gameid, state):
    """Public-reply guess path (cross-client public play still works)."""
    sender = (note.get("user") or {}).get("pubkey")
    _apply_move(sender, gameid, state, _clean_text(note), lambda m: _reply_text(note, m), note["id"])


def _handle_dm(sender, gameid, state, move_text):
    """Private-DM guess path — nudges/errors go back as DMs."""
    _apply_move(sender, gameid, state, move_text,
                lambda m: _nk.send_dm(sender, m), state.get("last_board_event") or gameid)


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
                _handle_move(note, root, state)
            else:
                _start_game(note, own_pk)
        except Exception as e:
            print(f"[hangman] processing {nid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
    # ---- private gameplay: read guess DMs (NIP-17) ---------------------------
    try:
        dms = _nk.read_dms(limit=100)
    except Exception as e:
        print(f"[hangman] read_dms failed: {e}", flush=True)
        dms = []
    for dm in dms:
        rid = dm.get("rumor_id")
        sender = dm.get("sender")
        if not rid or not sender or sender == own_pk:
            continue
        if dm.get("created_at", 0) < cutoff:
            continue
        gameid, move_text = _clean_dm_text(dm.get("text") or "")
        if not move_text:
            continue
        if not gameid:
            gameid = _get_player_game(sender)   # bare DM reply → the player's pending game
        if not gameid:
            continue
        if not _claim_dm(rid):
            continue
        state = _load_game(gameid)
        if not state:
            continue
        try:
            _handle_dm(sender, gameid, state, move_text)
        except Exception as e:
            print(f"[hangman] DM guess {rid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
