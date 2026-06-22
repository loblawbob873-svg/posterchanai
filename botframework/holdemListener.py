"""#holdem — multiplayer Texas Hold'em over Nostr, dealt + refereed by the bot.

START by posting "holdem @friend @friend …" mentioning the bot to seat a table (you + up to 5
friends). The bot deals hole cards privately in DMs and posts the community board + action publicly.
Each player acts on THEIR turn via DM or a public reply: `check`, `call`, `raise <amount>`, `fold`,
or `allin`. The bot drives the betting rounds (pre-flop → flop → turn → river), builds side pots for
all-ins, and posts the showdown. Play-money chips; everyone starts with the same stack each hand.

NO AI / LLM and NO firehose: like the other game bots it only touches its own mentions + move DMs
(claim-deduped, bounded queries), and the poker math is O(21) per showdown — negligible CPU. State
is a kind-30078 doc (`pcai:holdem:<gameid>`) the web client also reads. Mirrors blackjackListener.
"""

import os
import re
import sys
import time
import json
import hashlib
import fcntl

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import holdem_render
import holdem_game as _G
import nostr as _nk
from config import NOSTR_NSEC
from app.services.nostr import event as _ev
from app.services.nostr import nip44 as _nip44


def _footer():
    """Promo footer on every public hand-result post: invite people to the app + where it runs."""
    site = (os.getenv("CHESS_SITE_URL", "") or "").strip().rstrip("/")
    play = f"\n🃏 Play Texas Hold'em (and chess, blackjack & more) at {site}" if site else \
           "\n🃏 Play Texas Hold'em on PosterChan"
    return play + "\n#holdem #poker #nostr #gamestr"

_KIND_APP = 30078
_MAX_SEATS = int(os.getenv("HOLDEM_MAX_SEATS", "6"))
_START_RE = re.compile(r"\b(?:hold\s*'?em|holdem|poker)\b", re.IGNORECASE)
_DM_GAME_RE = re.compile(r"\bg:([0-9a-f]{64})\b", re.IGNORECASE)
_NOSTR_TOKEN_RE = re.compile(
    r"nostr:[a-z0-9]+|\b(?:npub1|nprofile1|nevent1|note1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]+",
    re.IGNORECASE)
_LOOKBACK_DAYS = int(os.getenv("HOLDEM_LOOKBACK_DAYS", "3"))
_INVITE_MAX = int(os.getenv("HOLDEM_INVITE_MAX_PER_HOUR", "3"))
_INVITE_WINDOW = 3600
_invite_times: dict = {}


# ---- dedup (same pattern as the other game bots) --------------------------
def _suffix():
    return hashlib.sha1((NOSTR_NSEC or "").encode()).hexdigest()[:10] if NOSTR_NSEC else "default"


def _state_dir():
    """Where the processed-id dedup files live. Prefer a PERSISTENT dir so a redeploy that replaces
    the code (e.g. a Docker image rebuild, which wipes botframework/) doesn't lose the dedup and
    re-process days of old mentions/DMs — which would re-deal games and flood players. Falls back to
    the script dir on bare-metal installs (persistent there anyway)."""
    for d in (os.getenv("PCAI_BOT_STATE_DIR"), "/app/data", script_dir):
        if d and os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    return script_dir


_STATE_DIR = _state_dir()
_IDS_FILE = os.path.join(_STATE_DIR, f".processed_holdem_ids_{_suffix()}")
_DM_IDS_FILE = os.path.join(_STATE_DIR, f".processed_holdem_dms_{_suffix()}")
_CMD_IDS_FILE = os.path.join(_STATE_DIR, f".processed_holdem_cmds_{_suffix()}")
_MAX_IDS = 5000


def _claim_in(ids_file, item_id):
    lock = ids_file + ".lock"
    try:
        with open(lock, "w") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            ids = set()
            try:
                with open(ids_file) as f:
                    ids = set(f.read().split())
            except FileNotFoundError:
                pass
            if item_id in ids:
                return False
            ids.add(item_id)
            if len(ids) > _MAX_IDS:
                ids = set(list(ids)[-_MAX_IDS:])
            with open(ids_file, "w") as f:
                f.write("\n".join(ids))
            return True
    except Exception:
        return False


def _claim(note_id):
    return _claim_in(_IDS_FILE, note_id)


def _claim_dm(rumor_id):
    return _claim_in(_DM_IDS_FILE, rumor_id)


def _claim_cmd(eid):
    return _claim_in(_CMD_IDS_FILE, eid)


# ---- state store (kind-30078) ---------------------------------------------
def _dtag(gameid):
    return f"pcai:holdem:{gameid}"


def _player_dtag(pk):
    return f"pcai:holdem:player:{pk}"


def _enc_state(state):
    """Public doc copy with SECRETS hidden: hole cards NIP-44'd per-player (only that player + the bot
    can read), and the UNDEALT DECK self-encrypted (bot-only) so nobody can peek at future board
    cards. The web client decrypts ONLY its own hole cards; the board reveals as it's dealt."""
    out = {k: v for k, v in state.items() if k not in ("hole", "deck")}
    he = {}
    for pk, cards in state.get("hole", {}).items():
        try:
            he[pk] = _nip44.encrypt_to(_nk._SECKEY, bytes.fromhex(pk), json.dumps(cards))
        except Exception:
            pass
    out["hole_enc"] = he
    try:
        out["deck_enc"] = _nip44.encrypt_self(_nk._SECKEY, json.dumps(state.get("deck", [])))
    except Exception:
        out["deck_enc"] = ""
    out["bot_pub"] = _nk._PUBKEY     # so the web client knows whose key to decrypt its hole cards with
    return out


def _dec_state(doc):
    """Restore plain `hole`/`deck` from the encrypted doc, for the bot's game logic."""
    if not isinstance(doc, dict):
        return doc
    hole = {}
    for pk, ct in (doc.get("hole_enc") or {}).items():
        try:
            hole[pk] = json.loads(_nip44.decrypt_from(_nk._SECKEY, bytes.fromhex(pk), ct))
        except Exception:
            pass
    doc["hole"] = hole
    try:
        doc["deck"] = json.loads(_nip44.decrypt_self(_nk._SECKEY, doc.get("deck_enc") or "[]"))
    except Exception:
        doc["deck"] = doc.get("deck", [])
    return doc


def _save_game(gameid, state):
    ev = _ev.build_event(_nk._SECKEY, _KIND_APP, json.dumps(_enc_state(state), separators=(",", ":")),
                         tags=[["d", _dtag(gameid)]])
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))


def _load_doc(dtag):
    try:
        evs = _nk._run(_nk._svc.relay.query(_nk._RELAYS, [{"authors": [_nk._PUBKEY], "kinds": [_KIND_APP],
                                                           "#d": [dtag], "limit": 1}]))
        return json.loads(evs[0].get("content") or "{}") if evs else None
    except Exception:
        return None


def _load_game(gameid):
    return _dec_state(_load_doc(_dtag(gameid)))


def _get_player_game(pk):
    doc = _load_doc(_player_dtag(pk))
    return doc.get("gameid") if isinstance(doc, dict) else None


def _set_player_game(pk, gameid):
    ev = _ev.build_event(_nk._SECKEY, _KIND_APP, json.dumps({"gameid": gameid}),
                         tags=[["d", _player_dtag(pk)]])
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))


# ---- note helpers ----------------------------------------------------------
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
    return re.sub(r"@[\w@.]+", "", t).strip()


def _clean_dm_text(text):
    gameid = None
    m = _DM_GAME_RE.search(text or "")
    if m:
        gameid = m.group(1)
        text = text.replace(m.group(0), "")
    return gameid, (text or "").strip()


def _reply_text(note, msg):
    try:
        _nk.send_reply(note, msg)
    except Exception as e:
        print(f"[holdem] reply failed: {e}", flush=True)


# ---- action parsing --------------------------------------------------------
_AMT_RE = re.compile(r"(\d+)")


def _parse_action(text):
    """('fold'|'check'|'call'|'raise'|'allin'|None, amount|None) from free text."""
    t = (text or "").lower().strip()
    if re.search(r"\b(all\s*-?\s*in|allin|shove|jam)\b", t):
        return "allin", None
    if re.search(r"\b(fold|muck|quit|out)\b", t):
        return "fold", None
    if re.search(r"\b(check|x)\b", t):
        return "check", None
    if re.search(r"\b(call|c)\b", t):
        return "call", None
    if re.search(r"\b(raise|bet|r|to)\b", t):
        m = _AMT_RE.search(t)
        return "raise", (int(m.group(1)) if m else None)
    # a bare number = raise-to that amount
    m = _AMT_RE.fullmatch(t)
    if m:
        return "raise", int(m.group(1))
    return None, None


# ---- rendering -------------------------------------------------------------
def _board_png(state, reveal=False):
    return holdem_render.render_table(state, reveal=reveal)


def _do_publish(gameid, parent_id, players, body, png):
    # Build the kind-1 event by hand (e-root/reply + p-tags + game hashtags + imeta) and publish it
    # straight to the relay — same path the working blackjack bot uses. (post_note() takes no tag
    # list, so the old post_note(..., extra_tags=) call raised and nothing ever posted.)
    content = body
    imeta = None
    if png:
        try:
            info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
            url = info.get("url")
            if url:
                content = f"{body}\n{url}"
                imeta = _ev.imeta_tag(url, "image/png", info.get("sha256", ""), info.get("dim", ""))
        except Exception as e:
            print(f"[holdem] board upload failed: {e}", flush=True)
    # A solo game's id is synthetic (no real start note), so e-rooting it would make a phantom reply —
    # pass gameid=None for a clean standalone post.
    tags = [["e", gameid, "", "root"]] if gameid else []
    if parent_id and parent_id != gameid:
        tags.append(["e", parent_id, "", "reply"])
    for pk in (players or []):
        if pk:
            tags.append(["p", pk])
    for _t in ("holdem", "poker", "nostr", "gamestr"):
        tags.append(["t", _t])
    if imeta:
        tags.append(imeta)
    try:
        ev = _ev.build_event(_nk._SECKEY, 1, content, tags=tags)
        _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))
        return ev
    except Exception as e:
        print(f"[holdem] publish failed: {e}", flush=True)
    return {}


def _action_line(state, pk):
    la = _G.legal_actions(state, pk)
    if not la:
        return ""
    opts = []
    if la.get("check"):
        opts.append("`check`")
    if la.get("call") is not None:
        opts.append(f"`call` ({la['call']})")
    if "raise_to_min" in la:
        opts.append(f"`raise <amt>` (min {la['raise_to_min']})")
    if la.get("allin"):
        opts.append("`allin`")
    opts.append("`fold`")
    return "Your move — reply: " + ", ".join(opts)


def _dm_to_act(state, gameid, pk):
    """DM the player whose turn it is: their hole cards + the board + pot + legal actions."""
    if not pk or pk == _nk._PUBKEY:
        return
    png = None
    try:
        png = holdem_render.render_seat(state, pk)
        info = _nk._run(_nk._svc.media.upload(_nk._MEDIA_CFG, _nk._SECKEY, png, "image/png")) or {}
        url = info.get("url") or ""
    except Exception as e:
        print(f"[holdem] seat DM render failed: {e}", flush=True)
        url = ""
    hole = " ".join(_G.card_str(c) for c in state["hole"][pk])
    board = " ".join(_G.card_str(c) for c in state["board"]) or "(pre-flop)"
    pot = sum(state["contrib"].values())
    call = max(0, state["to_call"] - state["street_bet"][pk])
    body = (f"🃏 Your hole cards: {hole}\nBoard: {board}\nPot: {pot} · to call: {call} · "
            f"your stack: {state['stacks'][pk]}\n"
            + (url + "\n\n" if url else "")
            + _action_line(state, pk) + "\nOr play from the Hold'em tab in the app.")
    try:
        _nk.send_dm(pk, body, extra_tags=[["g", gameid]])
        _set_player_game(pk, gameid)
    except Exception as e:
        print(f"[holdem] send_dm failed: {e}", flush=True)


# ---- game flow -------------------------------------------------------------
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
    seats = list(dict.fromkeys([sender] + opponents))
    seats = [s for s in seats if s != own_pk][:_MAX_SEATS]
    solo = len(seats) < 2
    if solo:
        seats.append(own_pk)   # SOLO → the bot takes a seat and plays (heads-up vs the dealer)
    state, _ = _G.start_hand(seats, button=0)
    state["bot"] = own_pk
    state["names"] = {pk: _name(pk) for pk in seats}
    state["root"] = gameid
    state["gameid"] = gameid
    # solo vs the bot = PRIVATE: no public opening/result posts (just a practice game vs the dealer).
    # A multiplayer table is public so the seated friends get notified + the app-promo on results.
    state["private"] = solo
    print(f"[holdem] new table {gameid[:12]} seats={len(seats)} private={solo}", flush=True)
    _save_game(gameid, state)
    if not solo:
        who = ", ".join(state["names"][p] for p in seats)
        body = (f"🃏 #holdem — {who} are at the table!\n"
                f"Blinds {state['sb']}/{state['bb']}. 📩 Check your DMs for your hole cards. "
                f"{state['names'][state['to_act']]} is first to act.")
        _do_publish(gameid, gameid, seats, body, _board_png(state))
    for pk in seats:
        if pk != own_pk:
            _set_player_game(pk, gameid)
    _run_bot_turns(state, gameid, gameid)   # if the bot is first to act it plays; else DM the human


def _start_solo(sender, own_pk):
    """Start a PRIVATE heads-up game vs the bot, triggered by a kind-30078 command (the app's 'New
    game vs bot' button). No public timeline post. Guards against a player spinning up many tables."""
    if not sender or sender == own_pk:
        return
    now = time.time()
    recent = [t for t in _invite_times.get(sender, []) if now - t < _INVITE_WINDOW]
    if _INVITE_MAX and len(recent) >= _INVITE_MAX:
        return
    cur = _get_player_game(sender)                     # already in a live game? don't start another
    if cur:
        ex = _load_game(cur)
        if ex and ex.get("status") == "betting" and sender in ex.get("seats", []) \
                and sender not in ex.get("left", []):
            return
    recent.append(now)
    _invite_times[sender] = recent
    gameid = os.urandom(32).hex()
    seats = [sender, own_pk]
    state, _ = _G.start_hand(seats, button=0)
    state["bot"] = own_pk
    state["names"] = {pk: _name(pk) for pk in seats}
    state["root"] = gameid
    state["gameid"] = gameid
    state["private"] = True
    print(f"[holdem] new SOLO table {gameid[:12]} for {sender[:8]}", flush=True)
    _save_game(gameid, state)
    _set_player_game(sender, gameid)
    _run_bot_turns(state, gameid, gameid)


def _handle_cmd(author, payload, own_pk):
    """A reliable, off-timeline command (kind-30078 #t=holdemcmd): {action, gameid?, amount?}. The app
    uses this for solo start + ALL moves instead of public kind-1 notes / flaky NIP-17 DMs. The reply
    is a no-op — the player sees the result in the app (it reads the game doc)."""
    action = (payload.get("action") or "").lower().strip()
    if not action:
        return
    if action == "start":
        _start_solo(author, own_pk)
        return
    gameid = payload.get("gameid") or _get_player_game(author)
    if not gameid:
        return
    state = _load_game(gameid)
    if not state:
        return
    amt = payload.get("amount")
    text = action + (f" {int(amt)}" if action == "raise" and amt else "")
    _apply_action(author, gameid, state, text, lambda m: None, state.get("root") or gameid)


def _run_bot_turns(state, gameid, parent_id):
    """Drive any bot-occupied seat: while it's the bot's turn, decide + act (looping across players),
    then DM whichever human is up next. Resolves the hand (and persistent re-deal) if the bot's action
    ends it. A safety cap prevents any pathological loop."""
    bot = state.get("bot")
    guard = 0
    while (state.get("status") == "betting" and state.get("to_act") == bot
           and bot in state.get("seats", []) and guard < 200):
        guard += 1
        action, amount = _G.bot_decide(state, bot)
        _, events = _G.act(state, bot, action, amount)
        _save_game(gameid, state)
        print(f"[holdem] bot {action}{(' ' + str(amount)) if amount else ''}", flush=True)
        if "showdown" in events:
            _post_result(state, gameid, parent_id, showdown=True); return
        if "folded_win" in events:
            _post_result(state, gameid, parent_id, showdown=False); return
    if state.get("status") == "betting" and state.get("to_act") and state.get("to_act") != bot:
        _dm_to_act(state, gameid, state["to_act"])


def _apply_action(sender, gameid, state, text, reply, parent_id):
    if sender not in state.get("seats", []):
        return
    low = (text or "").lower().strip()
    if re.search(r"\b(leave|sit\s*out|stand\s*up|quit\s*table|cash\s*out)\b", low):
        state, events = _G.leave(state, sender)
        _save_game(gameid, state)
        reply("👋 You've left the table. The hand continues with the rest.")
        if "folded_win" in events or state.get("status") == "done":
            _post_result(state, gameid, parent_id, showdown=False)
        else:
            _run_bot_turns(state, gameid, parent_id)
        return
    if state.get("status") != "betting":
        reply("🏁 This hand is over — the next one is being dealt.")
        return
    if state.get("to_act") != sender:
        reply("⏳ Not your turn yet — waiting on " + state["names"].get(state.get("to_act"), "another player") + ".")
        return
    action, amount = _parse_action(text)
    if not action:
        reply("🃏 Reply `check`, `call`, `raise <amt>`, `fold`, `allin`, or `leave`.")
        return
    state, events = _G.act(state, sender, action, amount)
    _save_game(gameid, state)
    reply(f"✅ {action}" + (f" {amount}" if action == 'raise' and amount else ""))
    # NOTE: street changes (flop/turn/river) are NOT posted publicly — that would flood the timeline.
    # The web client + the to-act player's DM both show the board from the (updated) state doc. Only
    # the HAND RESULT goes to the public timeline (with the table image + app promo).
    if "showdown" in events:
        _post_result(state, gameid, parent_id, showdown=True)
        return
    if "folded_win" in events:
        _post_result(state, gameid, parent_id, showdown=False)
        return
    _run_bot_turns(state, gameid, parent_id)   # bot plays if it's now its turn, else DMs the human


def _post_result(state, gameid, parent_id, showdown):
    winners = state.get("winners", {})
    parts = []
    for pk, amt in winners.items():
        nm = state["names"].get(pk, _name(pk))
        rank = (state.get("ranks") or {}).get(pk)
        parts.append(f"{nm} wins {amt}" + (f" with {rank}" if showdown and rank else ""))
    summary = " · ".join(parts) if parts else "no winner"
    state["result"] = summary
    state["status"] = "done"
    _save_game(gameid, state)
    private = bool(state.get("private"))
    # PUBLIC result post for EVERY hand (winner + table image + app promo). Solo games post standalone
    # (their id is synthetic → no phantom e-root); multiplayer threads under the table root.
    head = "🏁 #holdem showdown!" if showdown else "🏁 #holdem hand over —"
    pot_won = sum(state.get("winners", {}).values())
    body = f"{head} {summary}.  ({pot_won} chips){_footer()}"
    _do_publish(None if private else gameid, None if private else parent_id, state["seats"],
                body, _board_png(state, reveal=showdown))
    # PERSISTENT table: deal the next hand (rotate button, carry stacks, drop leavers/busted).
    nxt, _ = _G.next_hand(state)
    if nxt is None:
        if private:
            # SOLO wrap-up: per-hand results stayed in-app; on table close post ONE public result
            # (final chip outcome + table image + app promo), and DM the player too.
            bot = state.get("bot")
            humans = [p for p in state["seats"] if p != bot]
            lines = []
            for h in humans:
                nm = state["names"].get(h, _name(h))
                fs = state["stacks"].get(h, 0)
                if fs <= 0:
                    lines.append(f"{nm} busted out vs the dealer")
                elif state["stacks"].get(bot, 0) <= 0:
                    lines.append(f"{nm} broke the dealer and took it all 🏆")
                else:
                    lines.append(f"{nm} cashed out with {fs} chips")
            outcome = "; ".join(lines) if lines else "table closed"
            body = f"🏁 #holdem — {outcome}.{_footer()}"
            try:
                _do_publish(None, None, state["seats"], body, _board_png(state, reveal=True))
            except Exception as e:
                print(f"[holdem] solo wrap-up post failed: {e}", flush=True)
            for h in humans:
                try:
                    _nk.send_dm(h, f"🏁 {outcome}. gg! Start a new game from the Hold'em tab.")
                except Exception:
                    pass
        else:
            _do_publish(gameid, parent_id, state["seats"],
                        "🃏 Table closed — not enough players to continue. gg!" + _footer(), None)
        return
    nxt["private"] = private
    # carry the just-finished result into the next hand's doc so the web UI can show "you won X"
    # (the previous hand's done-doc is overwritten immediately by this re-deal — same d-tag).
    nxt["last_result"] = {"summary": summary, "winners": {p: a for p, a in winners.items()},
                          "showdown": bool(showdown)}
    _save_game(gameid, nxt)
    for pk in nxt["seats"]:
        if pk != nxt.get("bot"):
            _set_player_game(pk, gameid)
    _run_bot_turns(nxt, gameid, parent_id)   # bot plays the new hand if it's first to act, else DM


def _handle_move(note, gameid, state):
    sender = (note.get("user") or {}).get("pubkey")
    _apply_action(sender, gameid, state, _clean_text(note), lambda m: _reply_text(note, m), note["id"])


def _handle_dm(sender, gameid, state, move_text):
    _apply_action(sender, gameid, state, move_text, lambda m: _nk.send_dm(sender, m),
                  state.get("root") or gameid)


# ---- poll loop (mirror process_blackjack — efficient, claim-deduped) -------
def process_holdem():
    own = _nk.get_own_account()
    if not own:
        print("[holdem] no account (NOSTR_NSEC missing) — idle", flush=True)
        return
    own_pk = own.get("pubkey")
    cutoff = int(time.time()) - _LOOKBACK_DAYS * 86400
    # First run with a fresh dedup (new node, or a deploy that lost the files): SEED the processed-id
    # store from current history WITHOUT acting on it, then only handle genuinely-new items. Otherwise
    # the bot would re-deal every game and replay every move in the lookback window — a notif flood.
    seed_mentions = not os.path.exists(_IDS_FILE)
    seed_dms = not os.path.exists(_DM_IDS_FILE)
    seed_cmds = not os.path.exists(_CMD_IDS_FILE)
    # PRIMARY channel: reliable, off-timeline kind-30078 commands (#t=holdemcmd) from the app — solo
    # start + every move. content = {"action","gameid"?,"amount"?}; one replaceable doc per player.
    try:
        cmds = _nk._run(_nk._svc.relay.query(
            _nk._RELAYS, [{"kinds": [_KIND_APP], "#t": ["holdemcmd"], "limit": 100}])) or []
    except Exception as e:
        print(f"[holdem] cmd query failed: {e}", flush=True)
        cmds = []
    for ev in cmds:
        eid = ev.get("id")
        author = ev.get("pubkey")
        if not eid or not author or author == own_pk:
            continue
        if ev.get("created_at", 0) < cutoff:
            continue
        if seed_cmds:
            _claim_cmd(eid)        # seed only — don't replay commands on a fresh deploy
            continue
        if not _claim_cmd(eid):
            continue
        try:
            _handle_cmd(author, json.loads(ev.get("content") or "{}"), own_pk)
        except Exception as e:
            print(f"[holdem] cmd {eid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
    for note in _nk.get_mentions(limit=40):
        nid = note.get("id")
        if not nid or (note.get("user") or {}).get("pubkey") == own_pk:
            continue
        if (note.get("_event") or {}).get("created_at", 0) < cutoff:
            continue
        if seed_mentions:
            _claim(nid)        # seed only — do not act on history
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
            print(f"[holdem] processing {nid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
    try:
        dms = _nk.read_dms(limit=100)
    except Exception as e:
        print(f"[holdem] read_dms failed: {e}", flush=True)
        dms = []
    for dm in dms:
        rid = dm.get("rumor_id")
        sender = dm.get("sender")
        if not rid or not sender or sender == own_pk:
            continue
        if dm.get("created_at", 0) < cutoff:
            continue
        if seed_dms:
            _claim_dm(rid)     # seed only — do not replay old moves
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
            print(f"[holdem] DM move {rid[:12]} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
