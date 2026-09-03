"""End-to-end game tester: act as a real local-key Nostr player against a game bot, over the live
relay. Verifies the whole DM-gameplay loop (which an extension wallet can't show easily): start
(public kind-1 mention) → bot DMs the board (NIP-17) → we read+decrypt it → we send a move DM → bot
applies + replies → ... until the game ends.

Usage:  ./venv-unified/bin/python scripts/game_e2e_test.py [chess|ttt|connect4|hangman|all]
It self-registers its throwaway pubkey into the relay's live WoT (control dir) and cleans up after.
"""
import os, sys, json, time, urllib.request

sys.path.insert(0, os.getcwd())
from app.services.nostr import nip17, bip340, event as _ev, relay as R, bech32 as _b32

RELAYS = ["ws://127.0.0.1:3052/relay"]
CONTROL_DIR = os.path.join(os.getcwd(), "data", "nostr_relay.control.d")
KIND_APP = 30388

GAMES = {
    "chess":    {"flag": "chess_bot_npub",    "dtag": "pcai:chesstr:",  "tag": "chess"},
    "ttt":      {"flag": "ttt_bot_npub",      "dtag": "pcai:ttt:",      "tag": "tictactoe"},
    "connect4": {"flag": "connect4_bot_npub", "dtag": "pcai:connect4:", "tag": "connect4"},
    "hangman":  {"flag": "hangman_bot_npub",  "dtag": "pcai:hangman:",  "tag": "hangman"},
}


def run(coro):
    import asyncio
    return asyncio.run(coro)


def cfg_npubs():
    with urllib.request.urlopen("http://localhost:3051/client/config", timeout=10) as r:
        return json.load(r)


def npub_to_hex(npub):
    return _b32.decode("npub", npub).hex()


def wot_add(pk_hex):
    os.makedirs(CONTROL_DIR, exist_ok=True)
    with open(os.path.join(CONTROL_DIR, f"cmd_test_{pk_hex[:8]}.json"), "w") as f:
        json.dump({"cmd": "wot-add", "pubkeys": [pk_hex]}, f)
    time.sleep(10)


def cleanup(pk_hex):
    os.makedirs(CONTROL_DIR, exist_ok=True)
    with open(os.path.join(CONTROL_DIR, f"cmd_del_{pk_hex[:8]}.json"), "w") as f:
        json.dump({"cmd": "delete-author", "pubkeys": [pk_hex]}, f)


def load_state(bot_hex, dtag):
    evs = run(R.query(RELAYS, [{"authors": [bot_hex], "kinds": [KIND_APP], "#d": [dtag], "limit": 1}])) or []
    evs.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    for e in evs:
        try:
            return json.loads(e.get("content") or "{}")
        except Exception:
            pass
    return None


def count_bot_dms(bot_hex, player_sk, player_hex, gameid):
    evs = run(R.query(RELAYS, [{"kinds": [1059], "#p": [player_hex], "limit": 100}])) or []
    n = 0
    for w in evs:
        try:
            sender, text, rumor = nip17.unwrap(player_sk, w)
        except Exception:
            continue
        if sender != bot_hex:
            continue
        if any(len(t) >= 2 and t[0] == "g" and t[1] == gameid for t in rumor.get("tags", [])):
            n += 1
    return n


# ---- per-game move logic (reads dedicated kind-30388 state, returns a move string or None if not our turn) --
def move_chess(state, me):
    import chess
    board = chess.Board(state["fen"])
    my_turn = (board.turn == chess.WHITE) == (state["white"] == me)
    if not my_turn:
        return None
    mv = next(iter(board.legal_moves), None)
    return mv.uci() if mv else None


def move_ttt(state, me):
    cells = state["cells"]
    stm = "X" if sum(1 for c in cells if c) % 2 == 0 else "O"
    my_mark = "X" if state["x"] == me else "O"
    if stm != my_mark:
        return None
    for i, c in enumerate(cells):
        if not c:
            return str(i + 1)
    return None


def move_connect4(state, me):
    cells = state["cells"]; COLS = 7
    stm = "1" if sum(1 for c in cells if c) % 2 == 0 else "2"
    my_mark = "1" if state["p1"] == me else "2"
    if stm != my_mark:
        return None
    for col in range(COLS):
        if not cells[col]:            # top row of the column empty → playable
            return str(col + 1)
    return None


def move_hangman(state, me, _tried):
    if state.get("guesser") != me:
        return None
    for ch in "etaoinshrdlucmfwypvbgkjqxz":   # always our turn while active; walk the alphabet
        if ch not in _tried:
            _tried.add(ch)
            return ch
    return None


MOVERS = {"chess": move_chess, "ttt": move_ttt, "connect4": move_connect4, "hangman": move_hangman}


def play(game, bot_hex):
    import secrets
    g = GAMES[game]
    player_sk = secrets.token_bytes(32)
    player_hex = bip340.pubkey_from_seckey(player_sk).hex()
    print(f"\n===== {game.upper()} =====  bot={bot_hex[:10]} player={player_hex[:10]}")
    wot_add(player_hex)

    start = _ev.build_event(player_sk, 1, f"start #{g['tag']}",
                            tags=[["p", bot_hex], ["t", g["tag"]], ["nofederate", "1"]])
    gameid = start["id"]
    n = 0
    for _ in range(6):
        n = run(R.publish(RELAYS, start))
        if n:
            break
        time.sleep(3)
    if not n:
        print("  FAIL: relay rejected the start post"); cleanup(player_hex); return False
    dtag = g["dtag"] + gameid

    st, deadline = None, time.time() + 90
    while time.time() < deadline:
        st = load_state(bot_hex, dtag)
        if st:
            break
        time.sleep(3)
    if not st:
        print("  FAIL: bot never created the game state"); cleanup(player_hex); return False
    print(f"  game created, status={st.get('status')}")

    tried = set()
    steps = 0
    while st and st.get("status") in ("active", "awaiting_word") and steps < 30:
        if st.get("status") == "awaiting_word":
            print("  (awaiting_word — challenger flow; skipping in vs-bot test)"); break
        mv = MOVERS[game](st, player_hex, tried) if game == "hangman" else MOVERS[game](st, player_hex)
        if mv is None:
            time.sleep(3); st = load_state(bot_hex, dtag); continue
        dm = nip17.wrap(player_sk, bot_hex, f"{mv}\n\ng:{gameid}")
        run(R.publish(RELAYS, dm))
        steps += 1
        snap = json.dumps(st)
        t2 = time.time() + 50
        while time.time() < t2:
            time.sleep(3); st = load_state(bot_hex, dtag)
            if not st or json.dumps(st) != snap:
                break
    dms = count_bot_dms(bot_hex, player_sk, player_hex, gameid)
    status = st.get("status") if st else "?"
    ok = (dms >= 1) and (status != "active" or steps >= 1)
    print(f"  RESULT status={status} moves_sent={steps} bot_DMs_received={dms} "
          f"result={st.get('result') if st else None!r}")
    print("  " + ("OK ✅" if ok else "CHECK ⚠️"))
    cleanup(player_hex)
    return ok


def _bj_value(hand):
    t = a = 0
    for c in hand or []:
        r = c[:-1]
        if r == "A":
            t += 11; a += 1
        elif r in ("T", "J", "Q", "K"):
            t += 10
        else:
            t += int(r)
    while t > 21 and a:
        t -= 10; a -= 1
    return t


def play_blackjack_table(bot_hex):
    """Two local-key players seat ONE blackjack table; each plays its own hand vs the shared dealer
    (basic strategy: hit < 17, else stand). Verifies the multi-seat resolve + per-seat DMs."""
    import secrets
    print(f"\n===== BLACKJACK TABLE =====  bot={bot_hex[:10]}")
    A = {"sk": secrets.token_bytes(32)}; A["pk"] = bip340.pubkey_from_seckey(A["sk"]).hex()
    B = {"sk": secrets.token_bytes(32)}; B["pk"] = bip340.pubkey_from_seckey(B["sk"]).hex()
    os.makedirs(CONTROL_DIR, exist_ok=True)
    with open(os.path.join(CONTROL_DIR, "cmd_test_bjtable.json"), "w") as f:
        json.dump({"cmd": "wot-add", "pubkeys": [A["pk"], B["pk"]]}, f)   # seat both into the WoT
    time.sleep(10)
    # A starts the table, seating B (p-tag)
    start = _ev.build_event(A["sk"], 1, "blackjack #blackjack",
                            tags=[["p", bot_hex], ["p", B["pk"]], ["t", "blackjack"], ["nofederate", "1"]])
    gameid = start["id"]
    n = 0
    for _ in range(5):
        n = run(R.publish(RELAYS, start))
        if n:
            break
        time.sleep(3)
    if not n:
        print("  FAIL: relay rejected the start post"); cleanup(A["pk"]); cleanup(B["pk"]); return False
    dtag = "pcai:blackjack:" + gameid
    st, deadline = None, time.time() + 90
    while time.time() < deadline:
        st = load_state(bot_hex, dtag)
        if st:
            break
        time.sleep(3)
    if not st:
        print("  FAIL: bot never created the table"); cleanup(A["pk"]); cleanup(B["pk"]); return False
    seats = st.get("seats", [])
    print(f"  table created: {len(seats)} seats, status={st.get('status')}")
    for label, P in (("A", A), ("B", B)):
        for _ in range(8):
            st = load_state(bot_hex, dtag)
            if not st or st.get("status") != "playing" or st.get("done", {}).get(P["pk"]):
                break
            hand = st.get("hands", {}).get(P["pk"], [])
            v = _bj_value(hand)
            action = "stand" if v >= 17 else "hit"
            run(R.publish(RELAYS, nip17.wrap(P["sk"], bot_hex, f"{action}\n\ng:{gameid}")))
            print(f"  {label} {action} (had {v})")
            snap = json.dumps([st.get("done"), len(hand), st.get("status")])
            t2 = time.time() + 45
            while time.time() < t2:
                time.sleep(3); st = load_state(bot_hex, dtag)
                if not st or json.dumps([st.get("done"), len(st.get("hands", {}).get(P["pk"], [])), st.get("status")]) != snap:
                    break
            if action == "stand":
                break
    t3 = time.time() + 45
    while time.time() < t3:
        st = load_state(bot_hex, dtag)
        if st and st.get("status") == "over":
            break
        time.sleep(3)
    dmsA = count_bot_dms(bot_hex, A["sk"], A["pk"], gameid)
    dmsB = count_bot_dms(bot_hex, B["sk"], B["pk"], gameid)
    results = (st or {}).get("results", {})
    ok = bool(st) and st.get("status") == "over" and len(results) == 2 and dmsA >= 1 and dmsB >= 1
    print(f"  RESULT status={st.get('status') if st else '?'} results={ {('A' if k==A['pk'] else 'B' if k==B['pk'] else k[:6]): v for k, v in results.items()} } DMs A={dmsA} B={dmsB}")
    print(f"  result text: {st.get('result') if st else None}")
    print("  " + ("OK ✅ multi-seat works end-to-end" if ok else "CHECK ⚠️"))
    cleanup(A["pk"]); cleanup(B["pk"])
    return ok


def play_holdem_table(bot_hex):
    """Two local-key players seat a Texas Hold'em table; the to-act player folds. Verifies the SECURE
    deal (hole cards + deck encrypted in the public doc, no plaintext leak), that a betting action is
    processed, and that the PERSISTENT table auto-deals the next hand with chips conserved."""
    import secrets
    print(f"\n===== HOLD'EM TABLE =====  bot={bot_hex[:10]}")
    A = {"sk": secrets.token_bytes(32)}; A["pk"] = bip340.pubkey_from_seckey(A["sk"]).hex()
    B = {"sk": secrets.token_bytes(32)}; B["pk"] = bip340.pubkey_from_seckey(B["sk"]).hex()
    sks = {A["pk"]: A["sk"], B["pk"]: B["sk"]}
    os.makedirs(CONTROL_DIR, exist_ok=True)
    with open(os.path.join(CONTROL_DIR, "cmd_test_holdem.json"), "w") as f:
        json.dump({"cmd": "wot-add", "pubkeys": [A["pk"], B["pk"]]}, f)   # seat both into the WoT
    time.sleep(10)
    start = _ev.build_event(A["sk"], 1, "holdem #holdem",
                            tags=[["p", bot_hex], ["p", B["pk"]], ["t", "holdem"], ["nofederate", "1"]])
    gameid = start["id"]
    n = 0
    for _ in range(5):
        n = run(R.publish(RELAYS, start))
        if n:
            break
        time.sleep(3)
    if not n:
        print("  FAIL: relay rejected the start post"); cleanup(A["pk"]); cleanup(B["pk"]); return False
    dtag = "pcai:holdem:" + gameid
    st, deadline = None, time.time() + 90
    while time.time() < deadline:
        st = load_state(bot_hex, dtag)
        if st:
            break
        time.sleep(3)
    if not st:
        print("  FAIL: bot never dealt the table"); cleanup(A["pk"]); cleanup(B["pk"]); return False
    leaked = ("hole" in st) or ("deck" in st)
    secure = bool(st.get("hole_enc")) and bool(st.get("deck_enc")) and not leaked
    print(f"  dealt: {len(st.get('seats', []))} seats, street={st.get('street')}, "
          f"pot={sum((st.get('contrib') or {}).values())}, secure(no-leak)={secure}")
    actor = st.get("to_act")
    fold = _ev.build_event(sks[actor], 1, "fold", tags=[["e", gameid, "", "root"], ["p", bot_hex]])
    run(R.publish(RELAYS, fold))
    print(f"  {'A' if actor == A['pk'] else 'B'} folds")
    # wait specifically for the RE-DEALT hand (hand_no>1, betting). Don't break on the transient
    # done-doc: there the pot is already in stacks but contrib still records it, so stacks+contrib
    # double-counts the blinds (the old "2015" false alarm). A fresh hand has stacks+contrib==2000.
    st2, t2 = None, time.time() + 75
    while time.time() < t2:
        time.sleep(3); st2 = load_state(bot_hex, dtag)
        if st2 and st2.get("hand_no", 1) > 1 and st2.get("status") == "betting":
            break
    redealt = bool(st2) and st2.get("hand_no", 1) > 1 and st2.get("status") == "betting"
    fresh_board = redealt and not (st2.get("board") or [])    # a re-dealt hand must start pre-flop
    chips = (sum((st2 or {}).get("stacks", {}).values()) + sum((st2 or {}).get("contrib", {}).values())) if st2 else 0
    ok = secure and redealt and fresh_board and chips == 2000
    print(f"  after fold: hand_no={st2.get('hand_no') if st2 else '?'} status={st2.get('status') if st2 else '?'} "
          f"board={len((st2 or {}).get('board') or [])} chips={chips} (expect 2000)")
    print("  " + ("OK ✅ deal+bet+persistent re-deal works (fresh pre-flop board), cards stay encrypted" if ok else "CHECK ⚠️"))
    cleanup(A["pk"]); cleanup(B["pk"])
    return ok


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    npubs = cfg_npubs()
    results = {}
    board_games = [g for g in GAMES if which == "all" or which == g]
    for game in board_games:
        npub = npubs.get(GAMES[game]["flag"])
        if not npub:
            print(f"{game}: no bot configured"); results[game] = None; continue
        try:
            results[game] = play(game, npub_to_hex(npub))
        except Exception as e:
            import traceback; traceback.print_exc(); results[game] = False
    if which in ("all", "blackjack", "blackjack-table"):
        npub = npubs.get("blackjack_bot_npub")
        if not npub:
            print("blackjack: no bot configured"); results["blackjack-table"] = None
        else:
            try:
                results["blackjack-table"] = play_blackjack_table(npub_to_hex(npub))
            except Exception as e:
                import traceback; traceback.print_exc(); results["blackjack-table"] = False
    if which in ("all", "holdem", "holdem-table"):
        npub = npubs.get("holdem_bot_npub")
        if not npub:
            print("holdem: no bot configured"); results["holdem-table"] = None
        else:
            try:
                results["holdem-table"] = play_holdem_table(npub_to_hex(npub))
            except Exception as e:
                import traceback; traceback.print_exc(); results["holdem-table"] = False
    print("\n===== SUMMARY =====")
    for g, r in results.items():
        print(f"  {g:<10} {'OK' if r else ('SKIP' if r is None else 'FAIL/CHECK')}")


if __name__ == "__main__":
    main()
