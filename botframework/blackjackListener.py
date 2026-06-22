"""#blackjack — Blackjack (21) vs the bot dealer over Nostr, solo OR at a multi-seat table.

START by posting "blackjack" mentioning the bot (solo: you vs the dealer), or "blackjack @friend …"
to seat friends at one table. Everyone is dealt a hand; each plays their OWN hand privately in DMs
(reply "hit"/"stand") against the SAME dealer, independently and in any order. When every seat has
finished, the dealer draws to 17 and the table result — each player win/lose/push vs the dealer — is
posted publicly. No AI search: the dealer follows a fixed rule, O(1) per action. State is a
replaceable kind-30078 doc keyed by the game root id. Every post carries #blackjack.
"""
import os
import re
import sys
import json
import time
import fcntl
import random
import hashlib
import tempfile

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import blackjack_render
import nostr as _nk
from config import NOSTR_NSEC
from app.services.nostr import event as _ev

_KIND_APP = 30078
_START_RE = re.compile(r"\b(blackjack|black\s*jack|start)\b", re.IGNORECASE)
_DM_GAME_RE = re.compile(r"\bg:([0-9a-f]{64})\b", re.IGNORECASE)
_NOSTR_TOKEN_RE = re.compile(
    r"nostr:[a-z0-9]+|\b(?:npub1|nprofile1|nevent1|note1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]+",
    re.IGNORECASE)
_LOOKBACK_DAYS = int(os.getenv("BLACKJACK_LOOKBACK_DAYS", "3"))
_INVITE_MAX = int(os.getenv("BLACKJACK_INVITE_MAX_PER_HOUR", "6"))
_INVITE_WINDOW = 3600
_MAX_SEATS = int(os.getenv("BLACKJACK_MAX_SEATS", "5"))
_invite_times: dict = {}
_OUTCOME_WORD = {"win": "won 🎉", "blackjack": "BLACKJACK 🃏", "lose": "lost", "push": "push"}


# ---- cross-restart dedup --------------------------------------------------
def _suffix():
    return hashlib.sha1((NOSTR_NSEC or "").encode()).hexdigest()[:10] if NOSTR_NSEC else "default"


_IDS_FILE = os.path.join(script_dir, f".processed_blackjack_ids_{_suffix()}")
_DM_IDS_FILE = os.path.join(script_dir, f".processed_blackjack_dms_{_suffix()}")


def _claim_in(ids_file, item_id):
    lock = ids_file + ".lock"
    try:
        with open(lock, "w") as lk:
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
                if len(ids) > 5000:
                    ids = set(sorted(ids)[-5000:])
                fd, tmp = tempfile.mkstemp(dir=script_dir, prefix=".bjids_")
                with os.fdopen(fd, "w") as f:
                    f.write("\n".join(ids))
                os.replace(tmp, ids_file)
                return True
            finally:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"[blackjack] claim failed: {e}", flush=True)
        return False


def _claim(note_id):
    return _claim_in(_IDS_FILE, note_id)


def _claim_dm(rumor_id):
    return _claim_in(_DM_IDS_FILE, rumor_id)


# ---- state store (kind-30078) ---------------------------------------------
def _dtag(gameid):
    return f"pcai:blackjack:{gameid}"


def _save_game(gameid, state):
    ev = _ev.build_event(_nk._SECKEY, _KIND_APP, json.dumps(state, separators=(",", ":")),
                         tags=[["d", _dtag(gameid)]])
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))


def _load_doc(dtag):
    try:
        evs = _nk._run(_nk._svc.relay.query(
            _nk._RELAYS, [{"authors": [_nk._PUBKEY], "kinds": [_KIND_APP], "#d": [dtag], "limit": 1}])) or []
    except Exception:
        return None
    evs.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    try:
        return json.loads(evs[0].get("content") or "{}") if evs else None
    except Exception:
        return None


def _load_game(gameid):
    return _load_doc(_dtag(gameid))


def _player_dtag(pk):
    return f"pcai:blackjack:player:{pk}"


def _get_player_game(pk):
    doc = _load_doc(_player_dtag(pk))
    return doc.get("gameid") if isinstance(doc, dict) else None


def _set_player_game(pk, gameid):
    ev = _ev.build_event(_nk._SECKEY, _KIND_APP, json.dumps({"gameid": gameid}, separators=(",", ":")),
                         tags=[["d", _player_dtag(pk)]])
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))


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
    return ("🃏 Wanna play? Mention me with \"blackjack\" (solo) or \"blackjack @friend\" to seat a "
            "table — I'll DM each player their hand; reply \"hit\" or \"stand\"." + play
            + "\n#blackjack #nostr #gamestr")


def _publish(gameid, parent_id, players, body, png, federate=True):
    info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
    url = info.get("url")
    if not url:
        raise RuntimeError("board image upload failed")
    content = f"{body}\n{url}\n\n{_footer()}"
    tags = [["e", gameid, "", "root"]]
    if parent_id and parent_id != gameid:
        tags.append(["e", parent_id, "", "reply"])
    for pk in (players or []):
        if pk:
            tags.append(["p", pk])
    for _t in ("blackjack", "nostr", "gamestr"):
        tags.append(["t", _t])
    if not federate:
        tags.append(["nofederate", "1"])
    tags.append(_ev.imeta_tag(url, "image/png", info.get("sha256", ""), info.get("dim", "")))
    ev = _ev.build_event(_nk._SECKEY, 1, content, tags=tags)
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))
    return ev


def _reply_text(note, text):
    try:
        _nk.send_reply(note, text + "\n\n#blackjack #nostr #gamestr")
    except Exception as e:
        print(f"[blackjack] reply failed: {e}", flush=True)


# ---- card logic (no search — O(1) per action) -----------------------------
_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K"]
_SUITS = ["S", "H", "D", "C"]


def _new_deck():
    deck = [r + s for s in _SUITS for r in _RANKS]
    random.shuffle(deck)
    return deck


def _card_val(rank):
    if rank == "A":
        return 11
    if rank in ("T", "J", "Q", "K"):
        return 10
    return int(rank)


def _hand_value(hand):
    total = sum(_card_val(c[:-1]) for c in hand)
    aces = sum(1 for c in hand if c[0] == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def _is_bj(hand):
    return len(hand) == 2 and _hand_value(hand) == 21


def _dealer_play(state):
    while _hand_value(state["dhand"]) < 17 and state["deck"]:
        state["dhand"].append(state["deck"].pop())


def _table_png(state, hide, title, subtitle):
    seats = [(state["names"][pk], state["hands"][pk], _hand_value(state["hands"][pk]),
              state.get("results", {}).get(pk)) for pk in state["seats"]]
    return blackjack_render.render_table(state["dhand"], _hand_value(state["dhand"]), seats,
                                         hide_hole=hide, title=title, subtitle=subtitle)


# ---- start + play ---------------------------------------------------------
def _start_game(note, own_pk):
    sender = (note.get("user") or {}).get("pubkey")
    gameid = note["id"]
    if not sender or _load_game(gameid):
        return
    now = time.time()
    recent = [t for t in _invite_times.get(sender, []) if now - t < _INVITE_WINDOW]
    if _INVITE_MAX and len(recent) >= _INVITE_MAX:
        _reply_text(note, f"⏳ You've started {_INVITE_MAX} tables in the last hour — that's the limit.")
        return
    recent.append(now)
    _invite_times[sender] = recent
    opponents = [p for p in _ptags(note) if p and p != own_pk and p != sender]
    seats = list(dict.fromkeys([sender] + opponents))     # sender first, dedup, preserve order
    seats = [s for s in seats if s != own_pk][:_MAX_SEATS]  # never seat the dealer; cap table size
    if not seats:
        return
    deck = _new_deck()
    hands = {pk: [deck.pop(), deck.pop()] for pk in seats}
    dhand = [deck.pop(), deck.pop()]
    done = {pk: _is_bj(hands[pk]) for pk in seats}         # a natural blackjack auto-stands
    state = {
        "v": 2, "bot": own_pk, "seats": seats, "names": {pk: _name(pk) for pk in seats},
        "deck": deck, "hands": hands, "done": done, "dhand": dhand,
        "status": "playing", "results": {}, "result": "", "folded": [],
        "root": gameid, "started": int(time.time()), "last_board_event": None,
    }
    print(f"[blackjack] new table {gameid[:12]} seats={len(seats)}", flush=True)
    if _is_bj(dhand) or all(done.values()):     # dealer natural or everyone stood on blackjack
        _save_game(gameid, state)
        _finish(state, gameid, gameid)
        return
    _post_opening(state, gameid)


def _post_opening(state, gameid):
    seats = state["seats"]
    who = ", ".join(state["names"][p] for p in seats)
    solo = len(seats) == 1
    body = (f"🃏 #blackjack — {who} {'sat down at' if solo else 'are at'} the table vs the dealer!\n"
            f"📩 Check your DMs for your hand — reply 'hit' or 'stand'. I'll post the result here.")
    png = _table_png(state, hide=True, title="BLACKJACK",
                     subtitle=(who if solo else f"{len(seats)} seats vs the dealer"))
    ev = _publish(gameid, gameid, seats, body, png, federate=True)
    state["last_board_event"] = ev.get("id")
    _save_game(gameid, state)
    for pk in seats:
        _set_player_game(pk, gameid)
        if not state["done"][pk]:
            _dm_seat(state, gameid, pk)


def _apply_move(sender, gameid, state, text, reply, parent_id):
    if sender not in state.get("seats", []):
        return
    if state.get("status") != "playing":
        reply("🏁 This table is finished. Start a new one with \"blackjack\".")
        return
    if state["done"].get(sender):
        reply("✋ You've already finished your hand — waiting on the rest of the table.")
        return
    low = text.lower().strip()
    if low in ("hit", "h", "draw", "card", "twist"):
        if state["deck"]:
            state["hands"][sender].append(state["deck"].pop())
        pv = _hand_value(state["hands"][sender])
        if pv > 21:
            state["done"][sender] = True
            reply(f"💥 Bust at {pv}!")
            _after_action(state, gameid, parent_id, sender)
        else:
            _save_game(gameid, state)
            _dm_seat(state, gameid, sender)     # updated hand, still your turn
    elif low in ("stand", "s", "stay", "hold", "stick"):
        state["done"][sender] = True
        reply(f"✋ You stand on {_hand_value(state['hands'][sender])}.")
        _after_action(state, gameid, parent_id, sender)
    elif low in ("resign", "quit", "fold", "abandon", "surrender"):
        state["done"][sender] = True
        if sender not in state["folded"]:
            state["folded"].append(sender)
        reply("🏳️ You folded.")
        _after_action(state, gameid, parent_id, sender)
    else:
        reply("🃏 Reply 'hit' to draw a card, or 'stand' to hold.")


def _after_action(state, gameid, parent_id, sender):
    if all(state["done"].get(p) for p in state["seats"]):
        _finish(state, gameid, parent_id)          # last seat done → dealer plays + resolve
    else:
        _save_game(gameid, state)
        try:
            _nk.send_dm(sender, "⏳ Locked in. Waiting for the rest of the table, then the dealer plays.",
                        extra_tags=[["g", gameid]])
        except Exception:
            pass


def _finish(state, gameid, parent_id):
    _dealer_play(state)
    dv = _hand_value(state["dhand"])
    dbj = _is_bj(state["dhand"])
    folded = set(state.get("folded", []))
    res = {}
    for pk in state["seats"]:
        hand = state["hands"][pk]
        pv = _hand_value(hand)
        if pk in folded or pv > 21:
            res[pk] = "lose"
        elif _is_bj(hand) and not dbj:
            res[pk] = "blackjack"
        elif dbj and not _is_bj(hand):
            res[pk] = "lose"
        elif dv > 21 or pv > dv:
            res[pk] = "win"
        elif pv < dv:
            res[pk] = "lose"
        else:
            res[pk] = "push"
    state["results"] = res
    state["status"] = "over"
    if len(state["seats"]) == 1:                  # keep the single-seat winner field for the web banner
        only = state["seats"][0]
        state["winner_pk"] = only if res[only] in ("win", "blackjack") else None
        state["winner_name"] = state["names"][only] if state.get("winner_pk") else None
    summary = (f"Dealer {dv}{' (BJ)' if dbj else (' bust' if dv > 21 else '')} — "
               + ", ".join(f"{state['names'][pk]} {_OUTCOME_WORD[res[pk]]}" for pk in state["seats"]))
    state["result"] = summary
    png = _table_png(state, hide=False, title="GAME OVER", subtitle=summary)
    _publish(gameid, parent_id, state["seats"], f"🏁 {summary}  gg!", png, federate=True)
    _save_game(gameid, state)


def _dm_seat(state, gameid, pk):
    if not pk or pk == _nk._PUBKEY:
        return
    hand = state["hands"][pk]
    pv = _hand_value(hand)
    up = state["dhand"][0] if state.get("dhand") else "?"
    png = blackjack_render.render(state["dhand"], hand, None, pv, hide_hole=True,
                                  title="YOUR HAND", subtitle=f"You have {pv} · dealer shows {up}")
    try:
        info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
        url = info.get("url") or ""
    except Exception as e:
        print(f"[blackjack] DM board upload failed: {e}", flush=True)
        url = ""
    body = (f"🃏 Your hand: {' '.join(hand)}  (= {pv})\nDealer shows {up}.\n"
            + (url + "\n\n" if url else "")
            + "Reply 'hit' to draw, or 'stand' to hold. Or play from the Blackjack tab in the app.")
    try:
        _nk.send_dm(pk, body, extra_tags=[["g", gameid]])
        _set_player_game(pk, gameid)
    except Exception as e:
        print(f"[blackjack] send_dm failed: {e}", flush=True)


def _handle_move(note, gameid, state):
    sender = (note.get("user") or {}).get("pubkey")
    _apply_move(sender, gameid, state, _clean_text(note), lambda m: _reply_text(note, m), note["id"])


def _handle_dm(sender, gameid, state, move_text):
    _apply_move(sender, gameid, state, move_text,
                lambda m: _nk.send_dm(sender, m), state.get("last_board_event") or gameid)


def process_blackjack():
    own = _nk.get_own_account()
    if not own:
        print("[blackjack] no account (NOSTR_NSEC missing) — idle", flush=True)
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
            print(f"[blackjack] processing {nid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
    # ---- private play: read move DMs (NIP-17) --------------------------------
    try:
        dms = _nk.read_dms(limit=100)
    except Exception as e:
        print(f"[blackjack] read_dms failed: {e}", flush=True)
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
            gameid = _get_player_game(sender)
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
            print(f"[blackjack] DM move {rid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
