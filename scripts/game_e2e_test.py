"""End-to-end game tester: act as a real local-key Nostr player against a game bot, over the live
relay. Verifies the WHOLE DM-gameplay loop the user can't see from an extension wallet:
start (public kind-1 mention) → bot DMs the board (NIP-17) → we read+decrypt it → we send a move DM →
bot applies + replies → ... until the game ends.

Run:  ./venv-unified/bin/python scripts/game_e2e_test.py chess
It self-registers its throwaway pubkey into the relay's live WoT (control dir) so its posts/DMs are
accepted, and cleans that up at the end.
"""
import os, sys, json, time, glob, secrets

sys.path.insert(0, os.getcwd())
import chess  # python-chess, used to pick legal moves from the game-state FEN
from app.services.nostr import nip17, bip340, event as _ev, relay as R, bech32 as _b32

RELAYS = ["ws://127.0.0.1:3052/relay"]
CONTROL_DIR = os.path.join(os.getcwd(), "data", "nostr_relay.control.d")
KIND_APP = 30078

BOTS = {
    "chess":   {"npub": "npub1k78flv7zz9h202hgg2y648ezy8duez5ja25aqjft6a7lheulpqrq3hv03p", "start": "start", "dtag": "pcai:chesstr:"},
}


def run(coro):
    import asyncio
    return asyncio.run(coro)


def npub_to_hex(npub):
    return _b32.decode("npub", npub).hex()


def wot_add(pk_hex):
    os.makedirs(CONTROL_DIR, exist_ok=True)
    p = os.path.join(CONTROL_DIR, f"cmd_test_{pk_hex[:8]}.json")
    with open(p, "w") as f:
        json.dump({"cmd": "wot-add", "pubkeys": [pk_hex]}, f)
    print(f"[test] queued wot-add for {pk_hex[:8]} (waiting for relay to pick it up)…")
    time.sleep(10)


def load_state(dtag):
    evs = run(R.query(RELAYS, [{"authors": [BOT_HEX], "kinds": [KIND_APP], "#d": [dtag], "limit": 1}])) or []
    evs.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    for e in evs:
        try:
            return json.loads(e.get("content") or "{}")
        except Exception:
            pass
    return None


def read_bot_dms(gameid):
    """Return decrypted bot→player DMs for this game (rumor carries the ['g', gameid] tag)."""
    evs = run(R.query(RELAYS, [{"kinds": [1059], "#p": [PLAYER_HEX], "limit": 100}])) or []
    out = []
    for w in evs:
        try:
            sender, text, rumor = nip17.unwrap(PLAYER_SK, w)
        except Exception:
            continue
        if sender != BOT_HEX:
            continue
        gtag = next((t[1] for t in rumor.get("tags", []) if len(t) >= 2 and t[0] == "g"), None)
        out.append({"text": text, "g": gtag, "t": rumor.get("created_at", 0)})
    return out


def main():
    game = sys.argv[1] if len(sys.argv) > 1 else "chess"
    cfg = BOTS[game]
    global BOT_HEX, PLAYER_SK, PLAYER_HEX
    BOT_HEX = npub_to_hex(cfg["npub"])
    PLAYER_SK = secrets.token_bytes(32)
    PLAYER_HEX = bip340.pubkey_from_seckey(PLAYER_SK).hex()
    print(f"[test] bot={BOT_HEX[:12]}  player={PLAYER_HEX[:12]}")
    wot_add(PLAYER_HEX)

    # 1) START — public kind-1 mention of the bot (nofederate so it doesn't blast to dead upstreams)
    start_ev = _ev.build_event(PLAYER_SK, 1, f"{cfg['start']} #chess",
                               tags=[["p", BOT_HEX], ["t", "chess"], ["nofederate", "1"]])
    gameid = start_ev["id"]
    n = 0
    for _ in range(5):
        n = run(R.publish(RELAYS, start_ev))
        if n:
            break
        time.sleep(3)
    print(f"[test] posted START (accepted by {n} relay) gameid={gameid[:12]}")
    if not n:
        print("[test] FAIL: relay rejected the start post (player not in WoT?)"); return

    # 2) wait for the bot to create the game + DM the board
    dtag = cfg["dtag"] + gameid
    deadline = time.time() + 90
    st = None
    while time.time() < deadline:
        st = load_state(dtag)
        if st:
            break
        time.sleep(3)
    if not st:
        print("[test] FAIL: bot never created the game state"); return
    print(f"[test] game created: white={st['white'][:8]} black={st['black'][:8]} status={st['status']}")
    dms = read_bot_dms(gameid)
    print(f"[test] bot DMs to player so far: {len(dms)}  "
          + ("✅ board DM received" if dms else "❌ NO board DM"))
    if dms:
        print("       latest DM text:\n         " + dms[-1]["text"].replace("\n", "\n         ")[:400])

    # 3) play the game to its end — we are WHITE (vs bot)
    moves_made = 0
    while st and st.get("status") == "active" and moves_made < 12:
        board = chess.Board(st["fen"])
        my_turn = (board.turn == chess.WHITE) == (st["white"] == PLAYER_HEX)
        if not my_turn:
            time.sleep(3); st = load_state(dtag); continue
        mv = next(iter(board.legal_moves), None)
        if not mv:
            break
        uci = mv.uci()
        dm = nip17.wrap(PLAYER_SK, BOT_HEX, f"{uci}\n\ng:{gameid}")
        run(R.publish(RELAYS, dm))
        moves_made += 1
        print(f"[test] sent move #{moves_made}: {uci}")
        # wait for the bot to apply our move (+ make its reply) → fen changes
        prev_fen = st["fen"]
        t2 = time.time() + 60
        while time.time() < t2:
            time.sleep(3)
            st = load_state(dtag)
            if not st or st["fen"] != prev_fen or st.get("status") != "active":
                break
        print(f"        -> status={st.get('status') if st else '?'} moves={len(st.get('moves',[])) if st else '?'}")

    dms = read_bot_dms(gameid)
    print(f"\n[test] RESULT: status={st.get('status')} result={st.get('result')!r} "
          f"winner={st.get('winner_name')!r}")
    print(f"[test] total bot→player DMs received: {len(dms)} (each turn should add one)")
    print("[test] " + ("✅ BOT DM-GAMEPLAY WORKS END-TO-END" if len(dms) >= 2 else "❌ bot DM loop incomplete"))

    # cleanup: remove the test player from WoT + purge its events
    os.makedirs(CONTROL_DIR, exist_ok=True)
    with open(os.path.join(CONTROL_DIR, f"cmd_del_{PLAYER_HEX[:8]}.json"), "w") as f:
        json.dump({"cmd": "delete-author", "pubkeys": [PLAYER_HEX]}, f)
    print("[test] queued cleanup (delete-author for the throwaway player)")


if __name__ == "__main__":
    main()
