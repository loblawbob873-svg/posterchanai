# main.py
import argparse
import threading
import time



def main():
    parser = argparse.ArgumentParser(description="Poster Chan AI Bot for Pleroma and Nostr")
    parser.add_argument(
        "--image", action="store_true", help="Post Images every so often"
    )
    parser.add_argument(
        "--autopost", action="store_true", help="Generate one in-character standalone post and exit (manager schedules it)"
    )
    parser.add_argument(
        "--autopost-print", action="store_true", help="Generate one in-character post and PRINT it without posting (preview)"
    )
    parser.add_argument(
        "--nitter", action="store_true", help="Post new Nitter RSS items to the configured targets"
    )
    parser.add_argument(
        "--ping", action="store_true", help="Pings Ollama every so often to make sure it is OK"
    )
    parser.add_argument(
        "--pleroma", action="store_true", help="Listen to Pleroma Events"
    )
    parser.add_argument(
        "--nostr", action="store_true", help="Listen to Nostr mentions (p-tagged kind-1 notes)"
    )
    parser.add_argument(
        "--dvm", action="store_true", help="Nostr NIP-90 Data Vending Machine (fulfil kind-5xxx AI jobs)"
    )
    parser.add_argument(
        "--chess", action="store_true", help="#chesstr — referee chess games between Nostr users"
    )
    parser.add_argument(
        "--ttt", action="store_true", help="#tictactoe — referee Tic-Tac-Toe games"
    )
    parser.add_argument(
        "--hangman", action="store_true", help="#hangman — referee Hangman games"
    )
    parser.add_argument(
        "--connect4", action="store_true", help="#connect4 — referee Connect Four games"
    )
    parser.add_argument(
        "--blackjack", action="store_true", help="#blackjack — deal Blackjack (21) vs the bot dealer"
    )
    parser.add_argument(
        "--holdem", action="store_true", help="#holdem — deal multiplayer Texas Hold'em"
    )
    parser.add_argument(
        "--blockbot", action="store_true", help="Start the Pleroma Blockbot daemon"
    )
    parser.add_argument(
        "--blocks", action="store_true", help="Run blockbot blocks function once"
    )
    parser.add_argument(
        "--blocks-print", action="store_true", help="Run blockbot blocks in print-only mode"
    )
    parser.add_argument(
        "--scalps", action="store_true", help="Run blockbot scalps function once"
    )
    parser.add_argument(
        "--scalps-print", action="store_true", help="Run blockbot scalps in print-only mode"
    )
    parser.add_argument(
        "--fba", action="store_true", help="Run blockbot FBA function once"
    )
    parser.add_argument(
        "--topposts", action="store_true", help="Run daily top posts function once"
    )
    parser.add_argument(
        "--topposts-print", action="store_true", help="Run daily top posts in print-only mode"
    )
    parser.add_argument(
        "--welcome", action="store_true", help="Start the Welcome Bot daemon"
    )
    parser.add_argument(
        "--welcome-print", action="store_true", help="Run welcome bot in print-only mode once"
    )
    parser.add_argument(
        "--report", action="store_true", help="Start the Report Bot daemon"
    )
    parser.add_argument(
        "--report-print", action="store_true", help="Run report bot in print-only mode once"
    )
    parser.add_argument(
        "--hashtagbot", action="store_true", help="Start the Hashtag Bot daemon (posts at 6am and 6pm)"
    )
    parser.add_argument(
        "--hashtagbot-print", action="store_true", help="Run hashtag bot in print-only mode once"
    )
    parser.add_argument(
        "--unfollowbot", action="store_true", help="Start the Unfollow Bot daemon"
    )
    parser.add_argument(
        "--unfollows", action="store_true", help="Run unfollowbot unfollows function once"
    )
    parser.add_argument(
        "--unfollows-print", action="store_true", help="Run unfollowbot unfollows in print-only mode"
    )
    args = parser.parse_args()

    # Validate that at least one platform is specified
    if not args.nostr and not args.dvm and not args.chess and not args.ttt and not args.hangman and not args.connect4 and not args.blackjack and not args.holdem and not args.nitter and not args.ping and not args.image and not args.autopost and not args.autopost_print and not args.blockbot and not args.pleroma and not args.blocks and not args.blocks_print and not args.scalps and not args.scalps_print and not args.fba and not args.topposts and not args.topposts_print and not args.welcome and not args.welcome_print and not args.report and not args.report_print and not args.hashtagbot and not args.hashtagbot_print and not args.unfollowbot and not args.unfollows and not args.unfollows_print:
        print("ERROR: Please specify at least one mode: --nostr, --dvm, --chess, --nitter, --ping, --pleroma, --blockbot, --blocks, --blocks-print, --scalps, --scalps-print, --fba, --topposts, --topposts-print, --welcome, --welcome-print, --report, --report-print, --hashtagbot, --hashtagbot-print, --unfollowbot, --unfollows, --unfollows-print, --image, --autopost, or --autopost-print")
        return


    # Publish/refresh this bot's Nostr profile (name/nip05/avatar) on startup — runs in a thread so a
    # slow relay doesn't delay the listeners. By now the bot is an operator key → the WoT relay
    # accepts its kind-0 (provision-time publishing races the WoT and often gets rejected).
    import os as _os
    if _os.getenv("NOSTR_NSEC"):
        def _pub_profile():
            try:
                import nostr as _nk
                _nk.ensure_profile()
                _nk.ensure_server_list()   # kind-10063 BUD-03 Blossom failover list
            except Exception as e:
                print(f"[ERROR] ensure_profile failed: {e}", flush=True)
        threading.Thread(target=_pub_profile, daemon=True).start()

    # EVERY listener below is imported BEFORE its thread starts, never inside the thread body.
    # An import that first runs in a worker thread can land while the interpreter is FINALISING —
    # the manager stops this bot, main() returns, and the daemon thread is still on its first line.
    # By then importlib._bootstrap has lost its own module globals and the import dies with a bare
    # `NameError: name 'sys' is not defined` several frames inside CPython, which reads as a broken
    # dependency rather than a shutdown race (it was reported as a PIL/defusedxml bug in the ttt
    # referee). Importing per-mode keeps the laziness that matters — a bot without --ttt still never
    # loads tttListener — while making sure no import can run at teardown.

    # Track threads for parallel execution of listener modes
    threads = []

    # Define daemon modes once for reuse
    daemon_modes = (args.blockbot, args.welcome, args.unfollowbot, args.report, args.hashtagbot)
    has_daemon = any(daemon_modes)

    # Nitter RSS → fediverse poster
    if args.nitter:
        from nitterListener import nitter_poster   # before the thread — see above

        def run_nitter():
            print("Starting Nitter RSS poster mode...")
            nitter_poster()
        # Run in a thread when combined with a listener (--pleroma/--nostr)
        # or a daemon, so it doesn't block them; run directly when it's the only mode.
        # (Omitting --nostr here made nitter run directly + return, so a --nostr --nitter
        # bot never started its Nostr listener.)
        if args.pleroma or args.nostr or threads or has_daemon:
            t = threading.Thread(target=run_nitter, daemon=True)
            t.start()
            threads.append(t)
        else:
            run_nitter()
            return

    if args.pleroma:
        from pleromaListener import process_notifications   # before the thread — see above


        def run_pleroma():
            print("Starting Pleroma listener...")
            while True:
                try:
                    process_notifications()
                except Exception as e:
                    print(f"[ERROR] process_notifications failed: {e}", flush=True)
                time.sleep(20)
        if threads or has_daemon:
            # Run in thread if other listeners running or daemon mode needed
            t = threading.Thread(target=run_pleroma, daemon=True)
            t.start()
            threads.append(t)
        else:
            # Run directly if only mode
            run_pleroma()
            return

    # Nostr listener (can run alongside --blockbot, etc.)
    if args.nostr:
        import os as _os
        # Presence-only: a stats / identity bot runs a live --nostr process for its kind-0 profile +
        # green status, but the operator did NOT enable the reply listener (Admin → Bots "reply"
        # unchecked → empty modes → the manager sets NOSTR_PRESENCE_ONLY). It must NOT reply to
        # mentions. The profile publish above already ran; here we just keep the process alive.
        _nostr_presence_only = (_os.getenv("NOSTR_PRESENCE_ONLY", "") or "").strip().lower() in ("1", "true", "yes", "on")
        # Before the thread — see run_nitter. Still skipped in presence-only mode, which deliberately
        # never touches nostrListener: the point of that mode is to publish the profile and idle.
        if not _nostr_presence_only:
            from nostrListener import process_mentions, process_random_replies

        def run_nostr():
            if _nostr_presence_only:
                print("Nostr presence-only mode (profile published; NOT replying to mentions).", flush=True)
                while True:
                    time.sleep(3600)
            print("Starting Nostr listener...")
            while True:
                try:
                    process_mentions()
                except Exception as e:
                    print(f"[ERROR] nostr process_mentions failed: {e}", flush=True)
                try:
                    process_random_replies()   # opt-in firehose random-reply (no-op unless enabled)
                except Exception as e:
                    print(f"[ERROR] nostr process_random_replies failed: {e}", flush=True)
                # Poll cadence: relays push fast, so a short gap keeps replies snappy
                # (each poll is one short-lived REQ per relay). Overridable via NOSTR_POLL_SECONDS.
                time.sleep(int(_os.getenv("NOSTR_POLL_SECONDS", "8")))
        if threads or has_daemon:
            t = threading.Thread(target=run_nostr, daemon=True)
            t.start()
            threads.append(t)
        else:
            run_nostr()
            return

    # DVM listener (NIP-90 job fulfilment) — can run alongside --nostr or standalone.
    if args.dvm:
        from dvmListener import process_job_requests   # before the thread — see above


        def run_dvm():
            print("Starting DVM (NIP-90) listener...")
            while True:
                try:
                    process_job_requests()
                except Exception as e:
                    print(f"[ERROR] dvm process_job_requests failed: {e}", flush=True)
                import os as _os
                time.sleep(int(_os.getenv("DVM_POLL_SECONDS", _os.getenv("NOSTR_POLL_SECONDS", "15"))))
        if threads or has_daemon:
            t = threading.Thread(target=run_dvm, daemon=True)
            t.start()
            threads.append(t)
        else:
            run_dvm()
            return

    # Every game referee waits out the relay-subprocess startup race, so the helper is imported
    # ONCE here — before any of their threads start, for the reason given above.
    if args.chess or args.ttt or args.hangman or args.connect4 or args.blackjack or args.holdem:
        from nostr import wait_for_relay

    # Chess referee (#chesstr) — can run alongside --nostr or standalone.
    if args.chess:
        from chessListener import process_chess   # before the thread — see above

        def run_chess():
            print("Starting #chesstr chess listener...")
            wait_for_relay()   # wait out the relay-subprocess startup race (see nostr.wait_for_relay)
            while True:
                try:
                    process_chess()
                except Exception as e:
                    print(f"[ERROR] chess process_chess failed: {e}", flush=True)
                import os as _os
                time.sleep(int(_os.getenv("CHESS_POLL_SECONDS", _os.getenv("NOSTR_POLL_SECONDS", "10"))))
        if threads or has_daemon:
            t = threading.Thread(target=run_chess, daemon=True)
            t.start()
            threads.append(t)
        else:
            run_chess()
            return

    # Tic-Tac-Toe referee (#tictactoe)
    if args.ttt:
        from tttListener import process_ttt   # before the thread — see above

        def run_ttt():
            print("Starting #tictactoe listener...")
            wait_for_relay()   # wait out the relay-subprocess startup race (see nostr.wait_for_relay)
            while True:
                try:
                    process_ttt()
                except Exception as e:
                    print(f"[ERROR] ttt process_ttt failed: {e}", flush=True)
                import os as _os
                time.sleep(int(_os.getenv("TTT_POLL_SECONDS", _os.getenv("NOSTR_POLL_SECONDS", "10"))))
        if threads or has_daemon:
            t = threading.Thread(target=run_ttt, daemon=True); t.start(); threads.append(t)
        else:
            run_ttt(); return

    # Hangman referee (#hangman)
    if args.hangman:
        from hangmanListener import process_hangman   # before the thread — see above

        def run_hangman():
            print("Starting #hangman listener...")
            wait_for_relay()   # wait out the relay-subprocess startup race (see nostr.wait_for_relay)
            while True:
                try:
                    process_hangman()
                except Exception as e:
                    print(f"[ERROR] hangman process_hangman failed: {e}", flush=True)
                import os as _os
                time.sleep(int(_os.getenv("HANGMAN_POLL_SECONDS", _os.getenv("NOSTR_POLL_SECONDS", "10"))))
        if threads or has_daemon:
            t = threading.Thread(target=run_hangman, daemon=True); t.start(); threads.append(t)
        else:
            run_hangman(); return

    # Connect Four referee (#connect4)
    if args.connect4:
        from connect4Listener import process_connect4   # before the thread — see above

        def run_connect4():
            print("Starting #connect4 listener...")
            wait_for_relay()   # wait out the relay-subprocess startup race (see nostr.wait_for_relay)
            while True:
                try:
                    process_connect4()
                except Exception as e:
                    print(f"[ERROR] connect4 process_connect4 failed: {e}", flush=True)
                import os as _os
                time.sleep(int(_os.getenv("CONNECT4_POLL_SECONDS", _os.getenv("NOSTR_POLL_SECONDS", "10"))))
        if threads or has_daemon:
            t = threading.Thread(target=run_connect4, daemon=True); t.start(); threads.append(t)
        else:
            run_connect4(); return

    if args.blackjack:
        from blackjackListener import process_blackjack   # before the thread — see above

        def run_blackjack():
            print("Starting #blackjack listener...")
            wait_for_relay()   # wait out the relay-subprocess startup race (see nostr.wait_for_relay)
            while True:
                try:
                    process_blackjack()
                except Exception as e:
                    print(f"[ERROR] blackjack process_blackjack failed: {e}", flush=True)
                import os as _os
                time.sleep(int(_os.getenv("BLACKJACK_POLL_SECONDS", _os.getenv("NOSTR_POLL_SECONDS", "10"))))
        if threads or has_daemon:
            t = threading.Thread(target=run_blackjack, daemon=True); t.start(); threads.append(t)
        else:
            run_blackjack(); return

    if args.holdem:
        from holdemListener import process_holdem   # before the thread — see above

        def run_holdem():
            print("Starting #holdem listener...")
            wait_for_relay()   # wait out the relay-subprocess startup race (see nostr.wait_for_relay)
            while True:
                try:
                    process_holdem()
                except Exception as e:
                    print(f"[ERROR] holdem process_holdem failed: {e}", flush=True)
                import os as _os
                time.sleep(int(_os.getenv("HOLDEM_POLL_SECONDS", "4")))   # snappy interactive moves (command channel)
        if threads or has_daemon:
            t = threading.Thread(target=run_holdem, daemon=True); t.start(); threads.append(t)
        else:
            run_holdem(); return

    # If we have threads but no daemon modes, wait for them
    if threads and not has_daemon:
        print(f"Running {len(threads)} listener(s) in parallel...")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nShutting down...")
            return

    # Single-mode handlers (not combinable with other listeners)
    if args.ping:
        print("Starting Ping mode...")
        while True:
            from ai import ai_ping
            ai_ping()
            time.sleep(90)
    elif args.image:
        from config import PLEROMA_ENDPOINT
        from config import NOSTR_NSEC
        imageposter = None
        if PLEROMA_ENDPOINT:
            from pleromaListener import imageposter
        elif NOSTR_NSEC:
            from nostrListener import imageposter
        if imageposter is None:
            print("ERROR: image bot has no platform configured (need "
                  "PLEROMA_ENDPOINT or NOSTR_NSEC)")
            return
        print("Starting Image Poster mode (one-shot)...")
        # One-shot mode - post once and exit (botctl handles scheduling)
        imageposter()
        print("Image posted, exiting.")
    elif args.autopost:
        from autopost import autopost
        print("Starting Auto-post mode (one-shot)...")
        # One-shot mode - generate one in-character post and exit (manager handles scheduling)
        autopost()
        print("Auto-post done, exiting.")
    elif args.autopost_print:
        from autopost import autopost
        print("Starting Auto-post preview (no posting)...")
        autopost(print_only=True)
    elif args.blockbot:
        from blockbot import background, waitToStart
        from config import PLEROMA_ENDPOINT
        waitToStart()
        if PLEROMA_ENDPOINT:
            print("Starting Pleroma blockbot daemon...")
            background()
        else:
            print("ERROR: PLEROMA_ENDPOINT is not configured")
            return
    elif args.blocks:
        from blockbot import blocks, init_db
        init_db()
        blocks(print_only=False)
    elif args.blocks_print:
        from blockbot import blocks, init_db
        init_db()
        blocks(print_only=True)
    elif args.scalps:
        from blockbot import scalps, init_db
        init_db()
        scalps(print_only=False)
    elif args.scalps_print:
        from blockbot import scalps, init_db
        init_db()
        scalps(print_only=True)
    elif args.fba:
        from blockbot import fba
        fba()
    elif args.topposts:
        from engagement import daily_top_posts, post_active_user_stats, init_db
        init_db()
        try:
            daily_top_posts(print_only=False)
            time.sleep(5)  # Brief delay between posts
            post_active_user_stats(print_only=False)
        except KeyboardInterrupt:
            print("\n\nShutting down...")
            return
    elif args.topposts_print:
        from engagement import daily_top_posts, post_active_user_stats, init_db
        init_db()
        try:
            daily_top_posts(print_only=True)
            print("\n--- DAU/MAU Stats ---\n")
            post_active_user_stats(print_only=True)
        except KeyboardInterrupt:
            print("\n\nShutting down...")
            return
    elif args.welcome:
        from welcomebot import background, waitToStart, init_db
        from config import PLEROMA_ENDPOINT
        init_db()
        waitToStart()
        if PLEROMA_ENDPOINT:
            print("Starting Pleroma welcome bot daemon...")
            background()
        else:
            print("ERROR: PLEROMA_ENDPOINT is not configured")
            return
    elif args.welcome_print:
        from welcomebot import welcome_pleroma, init_db
        init_db()
        welcome_pleroma(print_only=True)
    elif args.report:
        from reportbot import background, waitToStart, init_db
        from config import PLEROMA_ENDPOINT
        init_db()
        waitToStart()
        if PLEROMA_ENDPOINT:
            print("Starting Pleroma report bot daemon...")
            background()
        else:
            print("ERROR: PLEROMA_ENDPOINT is not configured")
            return
    elif args.report_print:
        from reportbot import report_pleroma, init_db
        init_db()
        report_pleroma(print_only=True)
    elif args.hashtagbot:
        from hashtagbot import background, waitToStart, get_config
        get_config()
        waitToStart()
        print("Starting Hashtag bot daemon (posts at 6am and 6pm)...")
        background()
    elif args.hashtagbot_print:
        from hashtagbot import post_trending_hashtags, get_config
        get_config()
        post_trending_hashtags(print_only=True)
    elif args.unfollowbot:
        from unfollowbot import background, waitToStart, init_db
        from config import PLEROMA_ENDPOINT
        init_db()
        waitToStart()
        if PLEROMA_ENDPOINT:
            print("Starting Pleroma unfollowbot daemon...")
            background()
        else:
            print("ERROR: PLEROMA_ENDPOINT is not configured")
            return
    elif args.unfollows:
        from unfollowbot import pleroma_unfollows, init_db
        init_db()
        pleroma_unfollows(print_only=False)
    elif args.unfollows_print:
        from unfollowbot import pleroma_unfollows, init_db
        init_db()
        pleroma_unfollows(print_only=True)

if __name__ == "__main__":
    main()