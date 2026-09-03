"""#blackjack — Blackjack (21) vs the bot dealer over Nostr, with chips, betting and a persistent
table — solo (private) OR a multi-seat table.

Each player plays their OWN hand vs the SAME dealer (hit/stand), wagering chips each round; the dealer
draws to 17 and pays out (blackjack 3:2). The table KEEPS GOING — it auto-deals the next round, carrying
stacks, until you leave or bust out. Mirrors holdemListener: the app drives play through a reliable,
OFF-TIMELINE command channel (dedicated kind-30388 #t=blackjackcmd) — no public timeline spam, no flaky DM
encryption. The dealer's hole card + the undealt deck are self-encrypted (bot-only) in the state doc;
player hands are open. Solo games are private (in-app results; one public wrap-up when the table closes);
multiplayer tables post each round's result publicly with the table image + app promo.
"""
import os
import re
import sys
import json
import time
import fcntl
import hashlib
import tempfile

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import blackjack_render
import blackjack_game as _G
import nostr as _nk
from config import NOSTR_NSEC
from app.services.nostr import event as _ev
from app.services.nostr import nip44 as _nip44

_KIND_APP = 30388
_START_RE = re.compile(r"\b(blackjack|black\s*jack)\b", re.IGNORECASE)
_DM_GAME_RE = re.compile(r"\bg:([0-9a-f]{64})\b", re.IGNORECASE)
_NOSTR_TOKEN_RE = re.compile(
    r"nostr:[a-z0-9]+|\b(?:npub1|nprofile1|nevent1|note1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]+",
    re.IGNORECASE)
_LOOKBACK_DAYS = int(os.getenv("BLACKJACK_LOOKBACK_DAYS", "3"))
_INVITE_MAX = int(os.getenv("BLACKJACK_INVITE_MAX_PER_HOUR", "6"))
_INVITE_WINDOW = 3600
_MAX_SEATS = int(os.getenv("BLACKJACK_MAX_SEATS", "5"))
_invite_times: dict = {}
_OUTCOME = {"win": "won", "blackjack": "BLACKJACK 🃏", "lose": "lost", "push": "push"}


# ---- dedup (persistent across redeploys; seed-on-fresh, see process_blackjack) ----
def _suffix():
    return hashlib.sha1((NOSTR_NSEC or "").encode()).hexdigest()[:10] if NOSTR_NSEC else "default"


def _state_dir():
    for d in (os.getenv("PCAI_BOT_STATE_DIR"), "/app/data", script_dir):
        if d and os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    return script_dir


_STATE_DIR = _state_dir()
_IDS_FILE = os.path.join(_STATE_DIR, f".processed_blackjack_ids_{_suffix()}")
_DM_IDS_FILE = os.path.join(_STATE_DIR, f".processed_blackjack_dms_{_suffix()}")
_CMD_IDS_FILE = os.path.join(_STATE_DIR, f".processed_blackjack_cmds_{_suffix()}")
_MAX_IDS = 5000


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
                if len(ids) > _MAX_IDS:
                    ids = set(sorted(ids)[-_MAX_IDS:])
                fd, tmp = tempfile.mkstemp(dir=_STATE_DIR, prefix=".bjids_")
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


def _claim_cmd(eid):
    return _claim_in(_CMD_IDS_FILE, eid)


# ---- state store (dedicated kind-30388): hide dealer hole card + deck -----------
def _dtag(gameid):
    return f"pcai:blackjack:{gameid}"


def _player_dtag(pk):
    return f"pcai:blackjack:player:{pk}"


def _enc_state(state):
    """Public doc with secrets hidden: the undealt deck is self-encrypted (bot-only), and WHILE THE
    ROUND IS LIVE the dealer's hole card is hidden (only the up card + a face-down count are exposed).
    Player hands are open (blackjack is played face-up). At showdown the full dealer hand is revealed."""
    out = {k: v for k, v in state.items() if k != "deck"}
    try:
        out["deck_enc"] = _nip44.encrypt_self(_nk._SECKEY, json.dumps(state.get("deck", [])))
    except Exception:
        out["deck_enc"] = ""
    if state.get("status") != "over":
        dh = state.get("dhand", [])
        out["dealer_up"] = dh[0] if dh else None
        out["dealer_down"] = max(0, len(dh) - 1)
        try:
            out["dhand_enc"] = _nip44.encrypt_self(_nk._SECKEY, json.dumps(dh))
        except Exception:
            out["dhand_enc"] = ""
        out.pop("dhand", None)
    out["bot_pub"] = _nk._PUBKEY
    return out


def _dec_state(doc):
    """Restore plain `deck` and (if hidden) `dhand` for the bot's game logic."""
    if not isinstance(doc, dict):
        return doc
    try:
        doc["deck"] = json.loads(_nip44.decrypt_self(_nk._SECKEY, doc.get("deck_enc") or "[]"))
    except Exception:
        doc["deck"] = doc.get("deck", [])
    if "dhand" not in doc and doc.get("dhand_enc"):
        try:
            doc["dhand"] = json.loads(_nip44.decrypt_self(_nk._SECKEY, doc["dhand_enc"]))
        except Exception:
            doc["dhand"] = []
    return doc


def _save_game(gameid, state):
    ev = _ev.build_event(_nk._SECKEY, _KIND_APP, json.dumps(_enc_state(state), separators=(",", ":")),
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
    return _dec_state(_load_doc(_dtag(gameid)))


def _get_player_game(pk):
    doc = _load_doc(_player_dtag(pk))
    return doc.get("gameid") if isinstance(doc, dict) else None


def _set_player_game(pk, gameid):
    ev = _ev.build_event(_nk._SECKEY, _KIND_APP, json.dumps({"gameid": gameid}, separators=(",", ":")),
                         tags=[["d", _player_dtag(pk)]])
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))


# ---- nostr helpers ------------------------------------------------------------
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


_LOG_MAX = 24   # rolling cap on the per-round action log carried in the state doc (keeps the relay doc small)


def _bj_log(state, line, pk=None):
    """Append a human-readable, round-tagged action line ('@bob hit → 18', 'Dealer: ... (= 18)') so the
    turn DM and the web UI can show the table's play-by-play (what everyone hit/stood on, how the dealer
    played). Blackjack is face-up, so this is all public info — it rides safely in the state doc."""
    log = state.setdefault("log", [])
    log.append({"r": state.get("round_no", 0), "t": line, "pk": pk})
    del log[:-_LOG_MAX]


def _bj_recap(state):
    """Action lines for the CURRENT round, oldest→newest (for the turn DM / UI)."""
    r = state.get("round_no", 0)
    return [e.get("t") for e in (state.get("log") or []) if e.get("r") == r and e.get("t")]


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
    site = (os.getenv("CHESS_SITE_URL", "") or "").strip().rstrip("/")
    play = f"\n🃏 Play Blackjack (and chess, hold'em & more) at {site}" if site else \
           "\n🃏 Play Blackjack on PosterChan"
    return play + "\n#blackjack #nostr #gamestr"


def _do_publish(gameid, parent_id, players, body, png):
    """Public kind-1: builds the event with e-root (real games), p-tags, hashtags + the table image.
    Pass gameid=None for a standalone post (solo games have a synthetic id with no real start note)."""
    # Real mentions, not bare @handles: a p-tag notifies but renders as plain text, so a result post
    # read "@npub1mq3s439… wins" — unrendered AND truncated. See _nk.mentionify.
    content = _nk.mentionify(body, players, _name)
    imeta = None
    if png:
        try:
            info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
            url = info.get("url")
            if url:
                # `content`, not `body`: rebuilding from the original string would discard the
                # mention rewrite above, which is exactly how hold'em shipped bare @handles on
                # every post that carried a board image — i.e. on every result.
                content = f"{content}\n{url}"
                imeta = _ev.imeta_tag(url, "image/png", info.get("sha256", ""), info.get("dim", ""))
        except Exception as e:
            print(f"[blackjack] board upload failed: {e}", flush=True)
    tags = [["e", gameid, "", "root"]] if gameid else []
    if parent_id and parent_id != gameid:
        tags.append(["e", parent_id, "", "reply"])
    for pk in (players or []):
        if pk:
            tags.append(["p", pk])
    for _t in ("blackjack", "nostr", "gamestr"):
        tags.append(["t", _t])
    if imeta:
        tags.append(imeta)
    try:
        ev = _ev.build_event(_nk._SECKEY, 1, content, tags=tags)
        _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))
        return ev
    except Exception as e:
        print(f"[blackjack] publish failed: {e}", flush=True)
    return {}


def _table_png(state, reveal):
    return blackjack_render.render_table(state, reveal=reveal)


# ---- start --------------------------------------------------------------------
def _new_state(seats, own_pk, gameid, private, bets=None):
    state = _G.start_round(seats, bets=bets, button=0)
    state["bot"] = own_pk
    state["names"] = {pk: _name(pk) for pk in seats}
    state["root"] = gameid
    state["gameid"] = gameid
    state["private"] = private
    state["round_no"] = 1
    state["bet_pref"] = dict(state.get("bet", {}))   # remembered wager for the next round
    return state


def _start_solo(sender, own_pk, bet=None):
    """Private heads-up game vs the dealer from a kind-30388 command (the app's 'New hand'). No public
    post. Guards against a player spinning up many tables."""
    if not sender or sender == own_pk:
        return
    now = time.time()
    recent = [t for t in _invite_times.get(sender, []) if now - t < _INVITE_WINDOW]
    if _INVITE_MAX and len(recent) >= _INVITE_MAX:
        return
    cur = _get_player_game(sender)
    if cur:
        ex = _load_game(cur)
        if ex and ex.get("status") in ("playing", "betting", "over") and sender in ex.get("seats", []) \
                and sender not in ex.get("left", []):
            return                                   # already at a live table → resume it, don't dupe
    recent.append(now)
    _invite_times[sender] = recent
    gameid = os.urandom(32).hex()
    state = _G.new_table([sender])                   # status 'betting' — no cards yet; bet IN the game
    state["bot"] = own_pk
    state["names"] = {sender: _name(sender)}
    state["root"] = gameid
    state["gameid"] = gameid
    state["private"] = True
    if bet:
        try:
            state["bet_pref"][sender] = max(_G.MIN_BET, int(bet))
        except Exception:
            pass
    print(f"[blackjack] new SOLO table {gameid[:12]} for {sender[:8]} (betting)", flush=True)
    _save_game(gameid, state)
    _set_player_game(sender, gameid)
    try:
        _nk.send_dm(sender, "🃏 New blackjack table! Place your bet to deal — reply 'bet 50' then "
                            "'deal', or tap the chips in the Blackjack tab.", extra_tags=[["g", gameid]])
    except Exception:
        pass


def _start_game(note, own_pk):
    """Public multi-seat table from a 'blackjack @friend' mention."""
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
    seats = list(dict.fromkeys([sender] + opponents))
    seats = [s for s in seats if s != own_pk][:_MAX_SEATS]
    if not seats:
        return
    solo = len(seats) == 1
    state = _new_state(seats, own_pk, gameid, solo)
    print(f"[blackjack] new table {gameid[:12]} seats={len(seats)} private={solo}", flush=True)
    _save_game(gameid, state)
    if not solo:
        who = ", ".join(state["names"][p] for p in seats)
        body = (f"🃏 #blackjack — {who} {'is' if solo else 'are'} at the table vs the dealer!\n"
                f"📩 Check your DMs for your hand — reply 'hit' or 'stand'.")
        _do_publish(gameid, gameid, seats, body, _table_png(state, reveal=False))
    _deal_followup(state, gameid)


def _deal_followup(state, gameid):
    """After a deal: DM each seat their hand; if everyone already finished (naturals), settle now."""
    for pk in state["seats"]:
        if pk != state.get("bot"):
            _set_player_game(pk, gameid)
            if not state["done"].get(pk):
                _dm_seat(state, gameid, pk)
    if _G.all_done(state):
        _finish(state, gameid, state.get("root") or gameid)


# ---- play ---------------------------------------------------------------------
def _set_bet(author, gameid, state, amount):
    try:
        amt = int(amount)
    except Exception:
        return
    state.setdefault("bet_pref", {})[author] = max(_G.MIN_BET, amt)
    _save_game(gameid, state)


def _apply_action(sender, gameid, state, text, reply, parent_id):
    if sender not in state.get("seats", []):
        return
    low = (text or "").lower().strip()
    if re.search(r"\b(leave|sit\s*out|stand\s*up|quit\s*table|cash\s*out)\b", low):
        _G.leave(state, sender)
        _save_game(gameid, state)
        reply("👋 You've left the table.")
        # Leaving must NEVER post publicly (solo OR group) — settle/close silently.
        if _G.all_done(state) and state.get("status") == "playing":
            _finish(state, gameid, parent_id, announce=False)
        elif state.get("status") == "over":      # left between rounds → close if nobody remains
            nxt, _ = _G.next_round(state, bets=state.get("bet_pref"))
            if nxt is None:
                _close_table(state, gameid, parent_id, announce=False)
        return
    bm = re.match(r"\bbet\s+(\d+)", low)               # DM betting: "bet 50"
    if bm:
        _set_bet(sender, gameid, state, bm.group(1))
        reply(f"🪙 Bet set to {state.get('bet_pref', {}).get(sender)}. Reply 'deal' to play the hand.")
        return
    if low in ("deal", "next", "rebet", "redeal", "play") and state.get("status") in ("over", "betting"):
        _deal_next(state, gameid, parent_id)
        return
    if state.get("status") != "playing":
        reply("🃏 Place a bet then deal — reply 'bet 50' then 'deal', or use the Blackjack tab.")
        return
    if state["done"].get(sender):
        reply("✋ You've finished your hand — waiting on the rest of the table.")
        return
    nm = (state.get("names") or {}).get(sender) or _name(sender)
    if low in ("hit", "h", "draw", "card", "twist"):
        _G.hit(state, sender)
        pv = _G.hand_value(state["hands"][sender])
        if state["done"].get(sender):
            _bj_log(state, f"{nm} hit → bust at {pv}", sender)
            reply(f"💥 Bust at {pv}!")
            _after_action(state, gameid, parent_id, sender)
        else:
            _bj_log(state, f"{nm} hit → {pv}", sender)
            _save_game(gameid, state)
            _dm_seat(state, gameid, sender)
    elif low in ("stand", "s", "stay", "hold", "stick"):
        _G.stand(state, sender)
        sv = _G.hand_value(state['hands'][sender])
        _bj_log(state, f"{nm} stands on {sv}", sender)
        reply(f"✋ You stand on {sv}.")
        _after_action(state, gameid, parent_id, sender)
    else:
        reply("🃏 Reply 'hit' to draw, or 'stand' to hold.")


def _after_action(state, gameid, parent_id, sender):
    if _G.all_done(state):
        _finish(state, gameid, parent_id)
    else:
        _save_game(gameid, state)


def _finish(state, gameid, parent_id, announce=True):
    # announce=False → settle SILENTLY, no public post / no result DM. Used when a player LEAVES (the
    # act of leaving must never spam the timeline — solo OR group).
    _G.settle(state)
    private = bool(state.get("private"))
    summary = state.get("result", "")
    # record the dealer's final hand in the action log so the result view shows how the dealer played.
    dh = state.get("dhand", [])
    if dh:
        _bj_log(state, f"Dealer: {' '.join(dh)} (= {_G.hand_value(dh)})")
    # carry the result into the doc so the app shows a "last round" banner on the next deal
    state["last_result"] = {"summary": summary,
                            "payouts": {p: a for p, a in state.get("payouts", {}).items()}}
    # PUBLIC result post for EVERY round (result + table image + app promo). Solo posts standalone
    # (synthetic id → no phantom e-root); multiplayer threads under the table root.
    if announce:
        _do_publish(None if private else gameid, None if private else parent_id, state["seats"],
                    f"🏁 #blackjack — {summary}.{_footer()}", _table_png(state, reveal=True))
    _save_game(gameid, state)
    if private and announce:
        _dm_result(state, gameid)                # also DM the solo player their result image
    # NOTE: do NOT auto-deal. The table stays in "over" so the player PLACES A BET and deals the
    # next hand (authentic blackjack). The persistent table just means it never ends on its own —
    # it waits for a 'deal' command (or a 'leave'). If everyone's broke, the next deal closes it.


def _deal_next(state, gameid, parent_id):
    """Deal the next round (persistent table) using each player's remembered wager."""
    nxt, _ = _G.next_round(state, bets=state.get("bet_pref"))
    if nxt is None:
        _close_table(state, gameid, parent_id)
        return
    nxt["names"] = state.get("names", {})
    nxt["bot"] = state.get("bot")
    nxt["root"] = state.get("root")
    nxt["gameid"] = state.get("gameid")
    nxt["private"] = state.get("private")
    nxt["bet_pref"] = {p: state.get("bet_pref", {}).get(p, nxt["bet"].get(p)) for p in nxt["seats"]}
    nxt["last_result"] = state.get("last_result")
    _save_game(gameid, nxt)
    if not nxt.get("private"):
        _do_publish(gameid, parent_id, nxt["seats"], "🃏 #blackjack — next hand dealt! Check your DMs.",
                    _table_png(nxt, reveal=False))
    _deal_followup(nxt, gameid)


def _close_table(state, gameid, parent_id, announce=True):
    # announce=False → close SILENTLY (no public wrap-up / DM). Used when the table empties via a LEAVE.
    if not announce:
        return
    bot = state.get("bot")
    humans = [p for p in state["seats"] if p != bot]
    lines = []
    for h in humans:
        nm = state["names"].get(h, _name(h))
        fs = state["stacks"].get(h, 0)
        lines.append(f"{nm} busted out vs the dealer" if fs <= 0 else f"{nm} cashed out with {fs} chips")
    outcome = "; ".join(lines) if lines else "table closed"
    if state.get("private"):
        try:
            _do_publish(None, None, state["seats"], f"🏁 #blackjack — {outcome}.{_footer()}",
                        _table_png(state, reveal=True))
        except Exception as e:
            print(f"[blackjack] wrap-up failed: {e}", flush=True)
        for h in humans:
            try:
                _nk.send_dm(h, f"🏁 {outcome}. gg! Start a new game from the Blackjack tab.")
            except Exception:
                pass
    else:
        _do_publish(gameid, parent_id, state["seats"], f"🏁 #blackjack — {outcome}. gg!" + _footer(), None)


def _dm_seat(state, gameid, pk):
    if not pk or pk == _nk._PUBKEY:
        return
    hand = state["hands"][pk]
    pv = _G.hand_value(hand)
    up = state["dhand"][0] if state.get("dhand") else "?"
    png = None
    try:
        png = blackjack_render.render_seat(state, pk)
        info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
        url = info.get("url") or ""
    except Exception as e:
        print(f"[blackjack] seat render failed: {e}", flush=True)
        url = ""
    recap = _bj_recap(state)
    recap_line = ("🔄 This round: " + " · ".join(recap[-8:]) + "\n") if recap else ""
    body = (f"🃏 Your hand: {' '.join(hand)} (= {pv}) · bet {state['bet'].get(pk, 0)}\n"
            f"Dealer shows {up}. Your stack: {state['stacks'].get(pk, 0)}.\n"
            + recap_line
            + (url + "\n\n" if url else "")
            + "Reply 'hit' to draw, or 'stand' to hold. Or play from the Blackjack tab in the app.")
    try:
        _nk.send_dm(pk, body, extra_tags=[["g", gameid]])
        _set_player_game(pk, gameid)
    except Exception as e:
        print(f"[blackjack] send_dm failed: {e}", flush=True)


def _dm_result(state, gameid):
    png = None
    try:
        png = _table_png(state, reveal=True)
        info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
        url = info.get("url") or ""
    except Exception:
        url = ""
    for pk in state["seats"]:
        if pk == state.get("bot"):
            continue
        out = state.get("results", {}).get(pk, "lose")
        net = state.get("payouts", {}).get(pk, 0)
        tag = "🏆 You won" if net > 0 else ("🤝 Push" if out == "push" else "💀 You lost")
        body = (f"{tag} {abs(net)} chips — {state.get('result','')}\nStack: {state['stacks'].get(pk,0)}.\n"
                + (url + "\n\n" if url else "")
                + "Place your bet and deal the next hand, or leave — from the Blackjack tab.")
        try:
            _nk.send_dm(pk, body, extra_tags=[["g", gameid]])
        except Exception:
            pass


# ---- command channel + legacy mention/DM --------------------------------------
def _handle_cmd(author, payload, own_pk):
    action = (payload.get("action") or "").lower().strip()
    if not action:
        return
    if action == "start":
        _start_solo(author, own_pk, payload.get("bet"))
        return
    gameid = payload.get("gameid") or _get_player_game(author)
    if not gameid:
        return
    state = _load_game(gameid)
    if not state:
        return
    if action == "bet":
        _set_bet(author, gameid, state, payload.get("amount"))
        return
    if action == "deal":
        # place this player's wager, then deal a hand (from the 'betting' pre-hand state or 'over')
        if payload.get("bet"):
            try:
                state.setdefault("bet_pref", {})[author] = max(_G.MIN_BET, int(payload.get("bet")))
            except Exception:
                pass
        if state.get("status") in ("betting", "over"):
            _deal_next(state, gameid, state.get("root") or gameid)
        return
    _apply_action(author, gameid, state, action, lambda m: None, state.get("root") or gameid)


def _handle_move(note, gameid, state):
    sender = (note.get("user") or {}).get("pubkey")
    _apply_action(sender, gameid, state, _clean_text(note), lambda m: _reply_text(note, m), note["id"])


def _handle_dm(sender, gameid, state, move_text):
    _apply_action(sender, gameid, state, move_text, lambda m: _nk.send_dm(sender, m),
                  state.get("root") or gameid)


def _reply_text(note, text):
    try:
        _nk.send_reply(note, text + "\n\n#blackjack #nostr #gamestr")
    except Exception as e:
        print(f"[blackjack] reply failed: {e}", flush=True)


# ---- poll loop ----------------------------------------------------------------
def process_blackjack():
    own = _nk.get_own_account()
    if not own:
        print("[blackjack] no account (NOSTR_NSEC missing) — idle", flush=True)
        return
    own_pk = own.get("pubkey")
    cutoff = int(time.time()) - _LOOKBACK_DAYS * 86400
    seed_cmds = not os.path.exists(_CMD_IDS_FILE)
    seed_mentions = not os.path.exists(_IDS_FILE)
    seed_dms = not os.path.exists(_DM_IDS_FILE)
    # PRIMARY: reliable off-timeline commands (#t=blackjackcmd) — start/bet/hit/stand/deal/leave.
    try:
        cmds = _nk._run(_nk._svc.relay.query(
            _nk._RELAYS, [{"kinds": [_KIND_APP], "#t": ["blackjackcmd"], "limit": 100}])) or []
    except Exception as e:
        print(f"[blackjack] cmd query failed: {e}", flush=True)
        cmds = []
    for ev in cmds:
        eid = ev.get("id")
        author = ev.get("pubkey")
        if not eid or not author or author == own_pk:
            continue
        if ev.get("created_at", 0) < cutoff:
            continue
        if seed_cmds:
            _claim_cmd(eid)
            continue
        if not _claim_cmd(eid):
            continue
        try:
            _handle_cmd(author, json.loads(ev.get("content") or "{}"), own_pk)
        except Exception as e:
            print(f"[blackjack] cmd {eid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
    # multiplayer start mentions + public-reply moves
    for note in _nk.get_mentions(limit=40):
        nid = note.get("id")
        if not nid or (note.get("user") or {}).get("pubkey") == own_pk:
            continue
        if (note.get("_event") or {}).get("created_at", 0) < cutoff:
            continue
        if seed_mentions:
            _claim(nid)
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
    # legacy move DMs
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
        if seed_dms:
            _claim_dm(rid)
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
