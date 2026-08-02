#!/usr/bin/env python3
import argparse
import os
import sys
import uvicorn


def check_ipex_environment():
    """Check if IPEX backend is configured and oneAPI environment is available."""
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(__file__), "posterchanai.db")
        if not os.path.exists(db_path):
            return  # Fresh install, no DB yet

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = 'llm_backend'")
        row = cursor.fetchone()
        conn.close()

        if row and row[0] == "ipex":
            # Check if oneAPI is in environment
            ld_path = os.environ.get("LD_LIBRARY_PATH", "")
            if "/intel/oneapi" not in ld_path and "/opt/intel" not in ld_path:
                print("=" * 60, file=sys.stderr)
                print("WARNING: IPEX backend selected but oneAPI environment not found!", file=sys.stderr)
                print("", file=sys.stderr)
                print("For Intel GPU acceleration, start with:", file=sys.stderr)
                print("  ./run-intel.sh --port 3051", file=sys.stderr)
                print("", file=sys.stderr)
                print("Or source oneAPI first:", file=sys.stderr)
                print("  source /opt/intel/oneapi/setvars.sh", file=sys.stderr)
                print("  python run.py --port 3051", file=sys.stderr)
                print("", file=sys.stderr)
                print("Continuing anyway (will fall back to CPU mode)...", file=sys.stderr)
                print("=" * 60, file=sys.stderr)
    except Exception as e:
        # Don't fail startup on check errors
        pass


ROLE_HELP = """Which part of the stack this process IS. Default 'all' = the historical single-process
behaviour: the web app plus every child it supervises (relay, worker, mediamtx, TURN, tor, bots).

Splitting them into separate units is what makes a deploy non-destructive. Under 'all', restarting to
ship a one-line router change also kills the relay (dropping every connected Nostr client and
federation), mediamtx (killing live streams MID-BROADCAST), pion-turn (dropping active calls) and all
nine bots — the web app, the least stable component, supervises the most stable ones.

  all     app + everything below (default; unchanged behaviour)
  app     the web app ONLY — spawns no relay/worker/media/bots
  relay   the Nostr relay process
  worker  background pollers/schedulers
  media   mediamtx (streams) + pion-turn (calls)
  bots    the bot manager (must run WITH the app — see app/role.roles)
  tor     the Tor daemons (.onion + SOCKS egress)
  proxy   the HTTP proxy fronting Tor
  git     the GRASP git host

Roles are comma-separated: the deployed layout is `app,bots`.

DEFAULTS TO 'all' ON PURPOSE. A node whose unit file has not been updated keeps working exactly as
before after a code deploy, and rolling back is repointing the unit — not a code revert."""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Posterchanai Server")
    parser.add_argument("--port", type=int, default=None, help="Port to run on (default: 3051)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    # No argparse `choices=`: the value may be COMMA-SEPARATED (e.g. "app,bots"), which
    # choices= rejects outright — argparse exits 2 and systemd restart-loops the unit.
    # app.role.current() does the validating, and falls back to 'all' for anything it does
    # not recognise, so a bad value degrades to the old single-process layout rather than
    # taking the node down.
    parser.add_argument("--role", default=None, help=ROLE_HELP)
    args = parser.parse_args()

    # CLI beats env; the env var is what the unit files and compose set. Exported either way so every
    # child (and app/main.py's gating) sees the same answer without re-parsing argv.
    from app.role import current as _current_role
    if args.role:
        os.environ["POSTERCHANAI_ROLE"] = args.role.strip().lower()
    role = _current_role()          # validates; unknown -> "all"
    os.environ["POSTERCHANAI_ROLE"] = role

    # Non-app roles are NOT web servers — they must never bind the app port. Each one is a process
    # that already exists today as a child of the app; here it is simply the whole job of this
    # process instead. Dispatch before uvicorn so no role can accidentally start a second API.
    if role == "relay":
        import runpy
        sys.argv = [sys.argv[0]]
        runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "relay_main.py"),
                       run_name="__main__")
        sys.exit(0)
    if role == "worker":
        import runpy
        sys.argv = [sys.argv[0]]
        runpy.run_module("app.worker", run_name="__main__")
        sys.exit(0)
    # Every role the runner knows about. Derived from _ROLE_SERVICES rather than hardcoded: a role
    # added there but missed here would fall through to uvicorn and start a SECOND web server on the
    # app's port instead of the component.
    from app.role_runner import _ROLE_SERVICES
    if role in _ROLE_SERVICES:
        from app.role_runner import run_role
        sys.exit(run_role(role))

    # Check IPEX environment before starting
    check_ipex_environment()

    # Port priority: CLI arg > env var > default
    port = args.port or int(os.environ.get("POSTERCHANAI_PORT", "3051"))

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=port,
        reload=False,
        # Chat uploads (images/PDFs/videos for compress/convert) are sent as
        # base64 over the WebSocket; the 16 MB default drops large frames and
        # the message never arrives. Raise to 64 MB.
        ws_max_size=64 * 1024 * 1024,
        # Restart speed: without this, uvicorn's graceful shutdown waits INDEFINITELY for in-flight
        # requests to finish. The bots generate near-constantly, so there's almost always a 30-300s LLM
        # request open → the process never exits on SIGTERM → systemd hits TimeoutStopSec (10s) and
        # SIGKILLs it ("Failed with result 'timeout'") on EVERY restart. Cap the drain at 3s so uvicorn
        # abandons in-flight requests, runs the lifespan shutdown cleanly, and exits well under the 10s
        # kill deadline — turning every restart from a 10s hard-kill into a ~fast, clean stop.
        timeout_graceful_shutdown=3,
    )
