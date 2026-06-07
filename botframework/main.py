# main.py
import argparse
import threading
import time



def main():
    parser = argparse.ArgumentParser(description="Poster Chan AI Bot for Matrix, Pleroma, and Misskey")
    parser.add_argument(
        "--matrix", action="store_true", help="Enable Matrix bot functionality"
    )
    parser.add_argument(
        "--misskey", action="store_true", help="Enable Misskey bot functionality"
    )
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
        "--nitter", action="store_true", help="Post new Nitter RSS items to configured Matrix rooms"
    )
    parser.add_argument(
        "--ping", action="store_true", help="Pings Ollama every so often to make sure it is OK"
    )
    parser.add_argument(
        "--pleroma", action="store_true", help="Listen to Pleroma Events"
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
    if not args.matrix and not args.misskey and not args.nitter and not args.ping and not args.image and not args.autopost and not args.autopost_print and not args.blockbot and not args.pleroma and not args.blocks and not args.blocks_print and not args.scalps and not args.scalps_print and not args.fba and not args.topposts and not args.topposts_print and not args.welcome and not args.welcome_print and not args.report and not args.report_print and not args.hashtagbot and not args.hashtagbot_print and not args.unfollowbot and not args.unfollows and not args.unfollows_print:
        print("ERROR: Please specify at least one mode: --matrix, --misskey, --nitter, --ping, --pleroma, --blockbot, --blocks, --blocks-print, --scalps, --scalps-print, --fba, --topposts, --topposts-print, --welcome, --welcome-print, --report, --report-print, --hashtagbot, --hashtagbot-print, --unfollowbot, --unfollows, --unfollows-print, --image, --autopost, or --autopost-print")
        return

    # Validate Matrix configuration only if Matrix mode is enabled

    # Track threads for parallel execution of listener modes
    threads = []

    # Define daemon modes once for reuse
    daemon_modes = (args.blockbot, args.welcome, args.unfollowbot, args.report, args.hashtagbot)
    has_daemon = any(daemon_modes)

    # Nitter RSS → Matrix room / fediverse poster
    if args.nitter:
        def run_nitter():
            from nitterListener import nitter_poster
            print("Starting Nitter RSS poster mode...")
            nitter_poster()
        # Run in a thread when combined with a listener (--pleroma/--misskey/--matrix)
        # or a daemon, so it doesn't block them; run directly when it's the only mode.
        if args.misskey or args.pleroma or args.matrix or threads or has_daemon:
            t = threading.Thread(target=run_nitter, daemon=True)
            t.start()
            threads.append(t)
        else:
            run_nitter()
            return

    # Misskey listener (can run alongside --blockbot, etc.)
    if args.misskey:
        def run_misskey():
            from misskeyListener import process_mentions
            print("Starting Misskey listener...")
            while True:
                try:
                    process_mentions()
                except Exception as e:
                    print(f"[ERROR] process_mentions failed: {e}", flush=True)
                time.sleep(20)
        if threads or has_daemon:
            # Run in thread if other listeners running or daemon mode needed
            t = threading.Thread(target=run_misskey, daemon=True)
            t.start()
            threads.append(t)
        else:
            # Run directly if only mode
            run_misskey()
            return

    # Pleroma listener (can run alongside --blockbot, etc.)
    if args.pleroma:
        def run_pleroma():
            from pleromaListener import process_notifications
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

    # Matrix listener (can run alongside --nitter, daemons, etc.)
    if args.matrix:
        def run_matrix():
            from matrixListener import process_messages
            print("Starting Matrix listener...")
            while True:
                try:
                    process_messages()
                except Exception as e:
                    print(f"[ERROR] process_messages failed: {e}", flush=True)
                time.sleep(5)
        if threads or has_daemon:
            t = threading.Thread(target=run_matrix, daemon=True)
            t.start()
            threads.append(t)
        else:
            # Run directly if only mode
            run_matrix()
            return

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
        from config import MISSKEY_SERVER
        from config import PLEROMA_ENDPOINT
        if MISSKEY_SERVER:
            from misskeyListener import imageposter
        if PLEROMA_ENDPOINT:
            from pleromaListener import imageposter
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
        from blockbot import background, misskey_background, waitToStart
        from config import MISSKEY_SERVER, PLEROMA_ENDPOINT
        waitToStart()
        # Choose daemon based on platform configuration
        if MISSKEY_SERVER:
            print("Starting Misskey blockbot daemon...")
            misskey_background()
        elif PLEROMA_ENDPOINT:
            print("Starting Pleroma blockbot daemon...")
            background()
        else:
            print("ERROR: Neither MISSKEY_SERVER nor PLEROMA_ENDPOINT is configured")
            return
    elif args.blocks:
        from blockbot import blocks, misskey_blocks, init_db
        from config import MISSKEY_SERVER
        init_db()
        if MISSKEY_SERVER:
            misskey_blocks(print_only=False)
        else:
            blocks(print_only=False)
    elif args.blocks_print:
        from blockbot import blocks, misskey_blocks, init_db
        from config import MISSKEY_SERVER
        init_db()
        if MISSKEY_SERVER:
            misskey_blocks(print_only=True)
        else:
            blocks(print_only=True)
    elif args.scalps:
        from blockbot import scalps, misskey_scalps, init_db
        from config import MISSKEY_SERVER
        init_db()
        if MISSKEY_SERVER:
            misskey_scalps(print_only=False)
        else:
            scalps(print_only=False)
    elif args.scalps_print:
        from blockbot import scalps, misskey_scalps, init_db
        from config import MISSKEY_SERVER
        init_db()
        if MISSKEY_SERVER:
            misskey_scalps(print_only=True)
        else:
            scalps(print_only=True)
    elif args.fba:
        from blockbot import fba
        fba()
    elif args.topposts:
        from engagement import daily_top_posts, misskey_daily_top_posts, post_active_user_stats, misskey_post_active_user_stats, init_db
        from config import MISSKEY_SERVER
        init_db()
        try:
            if MISSKEY_SERVER:
                misskey_daily_top_posts(print_only=False)
                time.sleep(5)  # Brief delay between posts
                misskey_post_active_user_stats(print_only=False)
            else:
                daily_top_posts(print_only=False)
                time.sleep(5)  # Brief delay between posts
                post_active_user_stats(print_only=False)
        except KeyboardInterrupt:
            print("\n\nShutting down...")
            return
    elif args.topposts_print:
        from engagement import daily_top_posts, misskey_daily_top_posts, post_active_user_stats, misskey_post_active_user_stats, init_db
        from config import MISSKEY_SERVER
        init_db()
        try:
            if MISSKEY_SERVER:
                misskey_daily_top_posts(print_only=True)
                print("\n--- DAU/MAU Stats ---\n")
                misskey_post_active_user_stats(print_only=True)
            else:
                daily_top_posts(print_only=True)
                print("\n--- DAU/MAU Stats ---\n")
                post_active_user_stats(print_only=True)
        except KeyboardInterrupt:
            print("\n\nShutting down...")
            return
    elif args.welcome:
        from welcomebot import background, misskey_background, waitToStart, init_db
        from config import MISSKEY_SERVER, PLEROMA_ENDPOINT
        init_db()
        waitToStart()
        if MISSKEY_SERVER:
            print("Starting Misskey welcome bot daemon...")
            misskey_background()
        elif PLEROMA_ENDPOINT:
            print("Starting Pleroma welcome bot daemon...")
            background()
        else:
            print("ERROR: Neither MISSKEY_SERVER nor PLEROMA_ENDPOINT is configured")
            return
    elif args.welcome_print:
        from welcomebot import welcome_pleroma, welcome_misskey, init_db
        from config import MISSKEY_SERVER
        init_db()
        if MISSKEY_SERVER:
            welcome_misskey(print_only=True)
        else:
            welcome_pleroma(print_only=True)
    elif args.report:
        from reportbot import background, misskey_background, waitToStart, init_db
        from config import MISSKEY_SERVER, PLEROMA_ENDPOINT
        init_db()
        waitToStart()
        if MISSKEY_SERVER:
            print("Starting Misskey report bot daemon...")
            misskey_background()
        elif PLEROMA_ENDPOINT:
            print("Starting Pleroma report bot daemon...")
            background()
        else:
            print("ERROR: Neither MISSKEY_SERVER nor PLEROMA_ENDPOINT is configured")
            return
    elif args.report_print:
        from reportbot import report_pleroma, report_misskey, init_db
        from config import MISSKEY_SERVER
        init_db()
        if MISSKEY_SERVER:
            report_misskey(print_only=True)
        else:
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
        from unfollowbot import background, misskey_background, waitToStart, init_db
        from config import MISSKEY_SERVER, PLEROMA_ENDPOINT
        init_db()
        waitToStart()
        if MISSKEY_SERVER:
            print("Starting Misskey unfollowbot daemon...")
            misskey_background()
        elif PLEROMA_ENDPOINT:
            print("Starting Pleroma unfollowbot daemon...")
            background()
        else:
            print("ERROR: Neither MISSKEY_SERVER nor PLEROMA_ENDPOINT is configured")
            return
    elif args.unfollows:
        from unfollowbot import pleroma_unfollows, misskey_unfollows, init_db
        from config import MISSKEY_SERVER
        init_db()
        if MISSKEY_SERVER:
            misskey_unfollows(print_only=False)
        else:
            pleroma_unfollows(print_only=False)
    elif args.unfollows_print:
        from unfollowbot import pleroma_unfollows, misskey_unfollows, init_db
        from config import MISSKEY_SERVER
        init_db()
        if MISSKEY_SERVER:
            misskey_unfollows(print_only=True)
        else:
            pleroma_unfollows(print_only=True)

if __name__ == "__main__":
    main()