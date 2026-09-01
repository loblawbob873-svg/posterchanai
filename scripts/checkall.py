#!/usr/bin/env python3
"""PosterChanAI — RUN EVERYTHING, and say plainly what passed and what did not.

    ./test.sh                          # everything this machine can check on its own
    ./test.sh --live https://poster.place    # …plus the checks that need a running instance
    ./test.sh --docker                 # all of it inside a container, nothing published

Why this exists
---------------
The checks were already here — 36 browser-driven `scripts/check_*.py` and ~2600 tests — and that is
exactly the problem: nobody can be expected to remember 40 command lines, so in practice two or
three got run before a deploy and the rest were discovered broken by a user. A suite that is not ONE
command is not a suite.

Three rules it is built on, each one learned the hard way in this repo:

  A SKIP IS NOT A PASS. A check that could not run — no Chrome, no instance URL, missing websockets
  — is printed in its own colour, counted separately, and named in the summary with the reason. The
  failure mode this exists to prevent is a green board that quietly covered nothing.

  NOTHING IS SILENTLY LEFT OUT. The check list is DISCOVERED from the filesystem, not typed here.
  A new `scripts/check_*.py` joins the suite the moment it is written; the table below carries only
  what cannot be inferred (does it need a live instance, how long to allow), and anything discovered
  without an entry is run anyway and reported as UNREGISTERED. Every hand-maintained parallel list
  in this codebase has been out of date at least once — see MEDIA_TOOL_COMMANDS, _TRAY_KEEP,
  notifList — so this one is not hand-maintained.

  A FAILURE MUST BE RE-RUNNABLE. Every failed row prints the exact command, so the next step is a
  copy-paste and not an archaeology session.

Groups
------
  unit    pytest tests/          — services, routers, media, relay. No browser.
  client  pytest tests/client/   — the shipped client JS run under node against stubs.
  ui      the self-contained browser checks. They serve the real static/ over a throwaway HTTP
          server and drive headless Chrome. No instance, no network, no keys.
  lint    advisory. Real findings, but not "does the app work" — reported, never fatal (--strict).
  live    browser checks that need a REAL running instance (--live URL). They log in with throwaway
          keys and talk to real relays, so they are the slowest and the only ones that can fail for
          reasons outside this checkout.

Exit code: 0 only if nothing failed. Skips do not fail the run, but they are impossible to miss.
"""
import argparse
import concurrent.futures
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _interpreter(root=None):
    """The venv that has the checks' dependencies — found from a GIT WORKTREE too.

    `root` is a parameter so this can be tested against a REAL git worktree in a temp directory
    (tests/test_check_suite_runs_in_a_worktree.py). Tested only against the live ROOT it would pass
    in the main checkout — where the bug does not happen — and go on failing everywhere it does.

    A worktree has no `venv-unified/` of its own: the venv lives in the main checkout and is not a
    tracked file, so `.claude/worktrees/<name>/venv-unified` does not exist. The old two-liner fell
    straight through to `sys.executable` — a bare system python with no `websockets` — and the whole
    browser half of the suite then reported SKIP ("websockets not installed") or a red
    ModuleNotFoundError. Measured on this box: 45 of 85 checks did not run, the board printed in
    EIGHT SECONDS, and it looked like a suite that had executed.

    That is the worst possible shape for a release gate, and it fires in exactly the situation the
    gate matters most: an agent or a person working in a worktree, where "I ran the tests" is
    answered by a board that checked almost nothing. `git rev-parse --git-common-dir` points at the
    main checkout's `.git` from inside any worktree, so its parent is where the venv actually is.
    """
    root = pathlib.Path(root) if root is not None else ROOT
    cands = [root / "venv-unified" / "bin" / "python"]
    try:
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=str(root),
                                capture_output=True, text=True, timeout=10)
        if common.returncode == 0:
            main = (root / common.stdout.strip()).resolve().parent
            cands.append(main / "venv-unified" / "bin" / "python")
    except Exception:
        pass
    for c in cands:
        if os.path.exists(c):
            return str(c)
    # Docker and a bare `pip install -r requirements.txt` both run the checks with the ACTIVE
    # interpreter and no venv directory, which is legitimate — so this stays a fallback rather than
    # an error. `_warn_if_interpreter_cannot_check()` is what stops it being a silent one.
    return sys.executable


PY = _interpreter()


def _interpreter_is_equipped():
    """Can the chosen interpreter actually run a browser check? Returns (ok, missing-module).

    `websockets` is the one, and deliberately the ONLY one asked for: it is what every check that
    drives a browser or a relay imports, and "websockets not installed" is verbatim what 30-odd of
    them printed while skipping. The browser itself is driven over CDP, so the `playwright` PYTHON
    package is not a dependency here even though a playwright-installed Chrome is what gets driven
    — demanding it made this probe report a broken interpreter that had just run a check to
    completion. Asked by RUNNING the interpreter, not by importing here: the checks are
    subprocesses, and this process's own imports say nothing about theirs.
    """
    try:
        r = subprocess.run([PY, "-c", "import websockets"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return True, ""
        m = re.search(r"No module named '([^']+)'", r.stderr or "")
        return False, (m.group(1) if m else (r.stderr or "").strip()[:80])
    except Exception as e:
        return False, str(e)[:80]

# ---------------------------------------------------------------------------------------------
# What cannot be inferred from the filename. Everything absent from here is assumed to be a
# self-contained `ui` check with the default timeout — which is the common case, and which means a
# new check needs no edit here to be RUN. It is only listed when it needs an instance, needs longer,
# or has to be told something.
#
#   group    'ui' (self-contained) | 'live' (needs --live URL) | 'skip' (with a reason)
#   secs     timeout. Generous: a check killed by the clock reports as a failure, which is a lie
#            about the code, and the one thing worse than a slow suite is a suite nobody trusts.
#   why      printed when the check is skipped, so a skip always says what would make it run.
#   env      fixed environment required by a check. This is applied after the runner's generated
#            browser port/profile, so an external installed process can deliberately name its port.
# ---------------------------------------------------------------------------------------------
CHECKS = {
    # Security release gate: deletion from HEAD is insufficient for a private signing key. This
    # requires a full clone, reports object ID + historical path only, and intentionally remains
    # red while any private signing container is reachable from any ref.
    "check_no_android_signing_history": dict(group="unit", secs=120, serial=True),
    # --- need a live instance -------------------------------------------------------------------
    "check_auth_gate":                 dict(group="live", secs=300),
    "check_client_icon_themes":        dict(group="live", secs=600),
    "check_client_mobile":             dict(group="live", secs=600),
    "check_dm_video_live":             dict(group="live", secs=420),
    "check_drive_blob_fetch":          dict(group="live", secs=420,
                                              live_args=[], live_env={"PC_ORIGIN": "{live}"}),
    "check_music_mobile":              dict(group="live", secs=420, serial=True),
    # Exercises real NIP-34 repository/issue events plus a live timeline.  It used to be
    # unregistered, so the nominally self-contained UI group silently hit the script's hard-coded
    # production default and then reported a repository-dependent skip.  Keep external state in the
    # explicit --live gate where it belongs.
    "check_os_back":                   dict(group="live", secs=600),
    # Kills a relay under a live session and takes the signer away under a live request. Slow by
    # construction: the waits ARE the check (7s down is longer than the retry that used to be all
    # there was, and the "answered in under 55s" bound is what separates a re-send from the ceiling).
    # These two create their own ws:// loopback relays. Running the page over HTTPS makes Chromium
    # correctly block that mixed-content socket and reports the security policy as a signer fault.
    "check_nip46_reconnect":           dict(group="ui", secs=600),
    # Four complete remote-signer negotiations, including a deliberately 16s approval. Running it
    # beside six Chrome-heavy UI checks starves its timers and produced two false login failures;
    # alone it repeatedly passes. This is a protocol timing test, not a CPU contention benchmark.
    "check_nip46_signer":              dict(group="ui", secs=420, serial=True),
    "check_os_apps":                   dict(group="live", secs=900),
    # Two browsers, three pairings, and a clock-skew case that has to time out to prove it works.
    "check_qr_device_login":           dict(group="live", secs=900, serial=True),
    "check_repo_view_mobile":          dict(group="live", secs=420),
    # These three drive real authenticated/client state. Treating them as self-contained made
    # Settings and QR silently target an absent localhost:3051, while timeline uniformity queried
    # production and called an empty incidental feed an environment skip. They must use the one
    # instance the release runner was explicitly given, or be skipped before Chrome starts.
    "check_user_settings_tabs":        dict(group="live", secs=420),
    "check_timeline_uniformity":        dict(group="live", secs=600),
    "check_qr_scan":                   dict(group="live", secs=900, serial=True),
    # Twenty cold browser sessions, each querying the production relays. Beside the parallel live
    # batch this can starve the very relay it is measuring: the full gate produced one empty first
    # profile and one timed-out first search, while the same five-session rate passed 20/20 alone.
    # Isolation is part of a deterministic integration test, not a relaxation of its assertions.
    "check_search_profile_stability":  dict(group="live", secs=1800, serial=True,
                                              live_args=["5", "{live}"]),
    # This is the complete two-device byte/sweep/trash/restore loop over the selected instance. It
    # signs two cold clients in, so running beside the six-way live batch can starve login and turn
    # a real gate into "SKIP login failed". It also used to be unregistered and silently targeted
    # whichever service happened to listen on localhost:3051.
    "check_sync_full":                 dict(group="live", secs=900, serial=True,
                                              live_args=["{live}"]),
    "check_timeline_ghosts":           dict(group="live", secs=600),
    "check_websearch_pages":           dict(group="live", secs=420),
    # Both checks put sustained pressure on resources shared by the production instance. Running
    # them beside the other Chrome drivers made Webxdc inspect a route before it rendered and made
    # URL reading receive a partial profile body. Each passes repeatedly in isolation, so serialize
    # the resource-heavy integration work instead of publishing from a race-prone gate.
    "check_webxdc_gallery":            dict(group="live", secs=420, serial=True),
    "check_url_reading":               dict(group="live", secs=900, serial=True,
                                              live_args=["--base", "{live}"]),
    # Bundles the desktop app's www/ and then wants an instance for the non-standalone half.
    "check_desktop_standalone":        dict(group="ui", secs=600),

    # --- self-contained, but slower than the default ---------------------------------------------
    "check_os_desktop":                dict(group="ui", secs=900),
    "check_meme_mobile":               dict(group="ui", secs=600),
    # Opens a 100-video player after its grid. Running beside five other Chromium instances can
    # delay the player repaint past the probe and report its already-rendered Back button missing.
    "check_shorts_mobile":             dict(group="ui", secs=600, serial=True),
    "check_meme_render_match":         dict(group="ui", secs=600),
    "check_calendar_mobile":           dict(group="ui", secs=600),
    # Both drive a local stub server and need no instance, so they are `ui`, not `live`.
    "check_code_editor":               dict(group="ui", secs=420),
    # Unlike every self-contained Chrome check, this attaches to an already-running installed
    # Electron process. Its port is intentionally fixed and must not be replaced by the per-check
    # collision-avoidance port assigned below.
    "check_installed_desktop_account": dict(group="ui", secs=420, serial=True,
                                              env={"PC_CHECK_PORT": "9223"}),
    "check_installed_native_files": dict(group="ui", secs=90, serial=True,
                                           env={"PC_CHECK_PORT": "9223"}),
    # These attach to the installed renderer on the same fixed loopback CDP endpoint. Keep them
    # serial with the account/native gates: both temporarily change the active app/window focus.
    "check_installed_admin_prune_preview": dict(group="ui", secs=240, serial=True,
                                                   env={"PC_CHECK_PORT": "9223"}),
    "check_installed_system_settings": dict(group="ui", secs=90, serial=True,
                                               env={"PC_CHECK_PORT": "9223"}),
    # Extracts Code + the native host bridge from app.asar, then drives disposable Git restore and
    # the packaged browser editor. It owns a Chrome process and must not overlap installed gates.
    "check_installed_code_package_release": dict(group="ui", secs=600, serial=True),
    # Reads Office workspace and Email attachment behavior from the immutable installed ASAR.
    # The Python entry point delegates to the extraction/browser shell gate and makes it discoverable.
    "check_installed_document_apps_release": dict(group="ui", secs=420, serial=True),
    # Extracts the immutable installed ASAR and runs the native-window ancestry, clipboard and
    # cross-output Alt+Tab simulators. Keep installed-artifact reads serial with the other installed
    # gates; the discoverable Python entry point delegates to check_installed_wm_package.sh.
    "check_installed_wm_release": dict(group="ui", secs=90, serial=True),
    # Real Sway/Foot pixels under continuous output. It intentionally skips off PosterChanOS, and
    # must run alone because it changes compositor focus/geometry for its disposable window.
    "check_installed_foot_flicker": dict(group="ui", secs=120, serial=True),
    "check_sharelink":                 dict(group="ui", secs=420),
    "check_contacts_mobile":           dict(group="ui", secs=600),
    "check_vault_mobile":              dict(group="ui", secs=600),
    "check_websearch_mobile":          dict(group="ui", secs=600),
    "check_notes_mobile":              dict(group="ui", secs=600),
    # Picture messages in Texts, on a device that is not the phone. The node simulator has no DOM,
    # and the bug that hid every attachment on the old messages lived entirely in one.
    "check_texts_media":               dict(group="ui", secs=420,
                                            why="pictures draw, survive a repaint, read once"),
    "check_files_explorer":            dict(group="ui", secs=600),
    "check_mail_mobile":               dict(group="ui", secs=600),
    "check_concord_mobile":            dict(group="ui", secs=420),
    "check_article_editor":            dict(group="ui", secs=600),

    # --- browser checks that drive a page they build themselves ----------------------------------
    "check_composer_toolbar":          dict(group="ui", secs=420),
    "check_quote_modal":               dict(group="ui", secs=420),
    "check_meme_timeline":             dict(group="ui", secs=420),
    "check_terminal_mobile":           dict(group="ui", secs=420),
    "check_terminal_resize":           dict(group="ui", secs=420),
    "check_extension_autofill":        dict(group="ui", secs=420),
    "check_extension_popup":           dict(group="ui", secs=420),

    # --- no browser at all: they read the source / the stylesheet --------------------------------
    "check_button_themes":             dict(group="ui", secs=420),
    "check_client_icons":              dict(group="ui", secs=180),
    "check_stream_chat":               dict(group="ui", secs=180),

    # --- ADVISORY. Reported, never fatal (unless --strict) ----------------------------------------
    # This one is a design-scale lint over a stylesheet that has ~330 accumulated drifts. It is real
    # and worth paying down, but it says nothing about whether the app WORKS — and a check that is
    # red on every single run is a check everybody learns to scroll past, which is the same disease
    # as a green board that covered nothing. It gets its own verdict so the number is visible and
    # the deploy signal stays meaningful.
    "check_css_scale":                 dict(group="lint", secs=180),
}

# The pytest suites. Split because one is 3 minutes of pure Python and the other is 5 minutes of
# node subprocesses, and knowing WHICH half went red is most of the diagnosis.
SUITES = [
    dict(name="tests", group="unit", secs=2700,
         argv=["-m", "pytest", "tests/", "-q", "--ignore=tests/client", "-p", "no:cacheprovider"],
         detail="services, routers, relay, media — no browser"),
    dict(name="tests/client", group="client", secs=2700,
         argv=["-m", "pytest", "tests/client/", "-q", "-p", "no:cacheprovider"],
         detail="the shipped client JS, run under node against stubs"),
]

DEFAULT_SECS = 420
# Each browser check opens its own Chrome on its own debugging port. They used to be hardcoded and
# four of them shared 9473, so two running at once attached to one browser. The runner hands every
# check a unique port and a unique profile directory instead (PC_CHECK_PORT / PC_CHECK_PROFILE),
# which is also what lets this run on a host that already has something on those ports.
PORT_BASE = 18400

C = dict(dim="\033[2m", red="\033[31m", grn="\033[32m", yel="\033[33m",
         cya="\033[36m", bold="\033[1m", off="\033[0m")
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = {k: "" for k in C}


def discover():
    """Every check on disk, whether or not anyone remembered to register it."""
    found = []
    for p in sorted((ROOT / "scripts").glob("check_*.py")):
        meta = dict(CHECKS.get(p.stem) or {})
        found.append(dict(name=p.stem, path=p, group=meta.get("group", "ui"),
                          secs=meta.get("secs", DEFAULT_SECS), why=meta.get("why", ""),
                          live_args=meta.get("live_args"), live_env=meta.get("live_env", {}),
                          env=meta.get("env", {}),
                          serial=meta.get("serial", p.stem == "check_drive_fresh_pair"),
                          registered=p.stem in CHECKS))
    return found


def have_chrome():
    return (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
            or shutil.which("chromium") or shutil.which("chromium-browser"))


def have_node():
    return shutil.which("node")


def git_head():
    """Which tree was checked. On a node this is the whole question — a green board for a commit
    two behind production is worse than no board (see the sync.sh drift note in CLAUDE.md)."""
    try:
        r = subprocess.run(["git", "log", "-1", "--format=%h %s"], cwd=ROOT,
                           capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip()[:90] or "unknown"
    except Exception:
        return "unknown"


def _captured(argv, cwd, env, timeout, output_path):
    """Run without a captured PIPE that grandchildren can keep open forever.

    Several browser checks launch Chrome and then exit. If Chrome inherits subprocess.PIPE,
    `communicate()` waits for Chrome to close the pipe even though the check process has already
    finished; the full suite then appears hung after pytest. A real file has no EOF handshake, so
    the parent result is available the instant the process exits. A timed-out job owns a process
    group so its browser children are stopped with it instead of leaking into later checks.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w+", encoding="utf-8", errors="replace") as out:
        kwargs = dict(cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT)
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        p = subprocess.Popen(argv, **kwargs)
        timed_out = False
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                else:
                    os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                p.kill()
            p.wait()
        finally:
            # A successful CHECK process is not proof that its browser tree stopped. Most drivers
            # merely send Chromium SIGTERM in their finally block and do not wait; Chrome can still
            # be flushing its profile when Python exits, becomes orphaned, and later owns the next
            # check's port/profile. This process created a private session above, so every remaining
            # member of that exact group belongs to this one check. Reap it on success as well as on
            # timeout. Never use a name/profile-wide pkill: that could touch a user's browser.
            if os.name != "nt":
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    try:
                        os.killpg(p.pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        out.flush()
        out.seek(0)
        text = out.read()
    if timed_out:
        text += f"\n[checkall] killed after {timeout}s"
        return 124, text
    return p.returncode, text


def run_one(job, live, tmp, idx):
    """Run one check in its own process, with its own port, profile and clock."""
    env = dict(os.environ)
    env["PC_CHECK_PORT"] = str(PORT_BASE + idx)
    # Logs may intentionally share --tmp across reruns; Chromium profiles may not. A check killed
    # outside this runner can leave a browser holding yesterday's SingletonLock, so include the
    # runner PID as well as the job index. No second invocation can attach to or delete this one.
    env["PC_CHECK_PROFILE"] = str(tmp / "profiles" / f"{job['name']}-{os.getpid()}-{idx}")
    env.setdefault("PYTHONUNBUFFERED", "1")
    for key, value in (job.get("env") or {}).items():
        env[key] = str(value).replace("{live}", live or "")
    argv = [PY, str(job["path"])]
    if job["group"] == "live":
        spec = job.get("live_args")
        argv.extend([str(x).replace("{live}", live) for x in
                     ([live] if spec is None else spec)])
        for key, value in (job.get("live_env") or {}).items():
            env[key] = str(value).replace("{live}", live)
    t0 = time.time()
    code, out = _captured(argv, ROOT, env, job["secs"], tmp / (job["name"] + ".log"))
    return dict(job, secs_took=time.time() - t0, code=code, out=out.strip(),
                cmd=" ".join(argv[1:]))


def run_suite(suite, tmp):
    t0 = time.time()
    argv = [PY] + suite["argv"]
    code, out = _captured(argv, ROOT, None, suite["secs"], tmp / (suite["name"] + ".log"))
    return dict(suite, secs_took=time.time() - t0, code=code, out=out.strip(),
                cmd=" ".join(argv[1:]), name=suite["name"], registered=True)


def summarise(res):
    """The ONE line under a check: what it actually said, not a restatement of the exit code.

    Every check here prints its own verdict — `OK …`, `FAIL 3 problem(s):`, `2 passed` — and that
    sentence is more use than anything this runner could invent, so it is quoted rather than
    replaced. A check that failed silently is reported as exactly that, which is itself a finding.
    """
    out = res["out"]
    if not out:
        return "(no output)"
    lines = [l.rstrip() for l in out.split("\n") if l.strip()]
    # pytest's own summary line is the most informative thing it prints.
    for l in reversed(lines):
        if re.search(r"\d+ (passed|failed|error)", l):
            return re.sub(r"\s+", " ", l.strip("= ")).strip()
    for l in reversed(lines):
        if re.match(r"^(OK|FAIL|SKIP|PASS)\b", l):
            return l
    # A failure with no verdict line: show the last thing it said before dying.
    return lines[-1][:160]


def verdict(res):
    """PASS / FAIL / LINT / SKIP — and for anything but a pass, the reason, always."""
    if res["code"] == 0:
        return "PASS", ""
    # Advisory: reported with its real number, but it does not decide whether you may deploy.
    if res["group"] == "lint":
        return "LINT", summarise(res)
    # Exit 2 is this repo's convention for "could not run": no Chrome, no site, nothing to test
    # against. It is NOT a failure of the code, and calling it one trains people to ignore red.
    if res["code"] == 2:
        return "SKIP", summarise(res)
    if res["code"] == 124:
        return "FAIL", f"timed out after {res['secs']}s"
    return "FAIL", summarise(res)


def _serving_live_traffic():
    """True when this box is currently serving the app — i.e. somebody is using it right now."""
    try:
        import subprocess as _sp
        for unit in ("posterchanai.service", "posterchanai-relay.service"):
            r = _sp.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5)
            if r.stdout.strip() == "active":
                return True
    except Exception:
        pass
    return False


def _default_jobs():
    """HOW MANY BROWSERS AT ONCE — AND NOT SIX ON A BOX SOMEBODY IS USING.

    The browser checks are ~20 cold headless Chrome sessions. On a dev machine, half the cores is
    right. On a node that is ALSO serving the app and the relay it is not: measured on server1
    mid-run, load 3.04 and the relay's newest event two minutes stale, reported from a phone as
    "no posts coming in" and "1 min behind" — while the box was simply busy running the suite that
    was supposed to be checking it.

    A deploy node is the commonest place this gets run, because it is where the code is. So it
    notices, halves itself, and says so; `--jobs N` still wins for anyone who means it."""
    cores = os.cpu_count() or 4
    jobs = max(1, min(6, cores // 2))
    if _serving_live_traffic():
        jobs = max(1, min(2, jobs))
    return jobs


def main():
    ap = argparse.ArgumentParser(description="Run every PosterChanAI check and report.")
    ap.add_argument("--live", metavar="URL",
                    help="a running instance to drive (e.g. https://poster.place). Without it the "
                         "live checks are SKIPPED and said to be skipped.")
    ap.add_argument("--group", action="append", choices=["unit", "client", "ui", "lint", "live"],
                    help="only these groups (repeatable). Default: all but live, unless --live.")
    ap.add_argument("--strict", action="store_true",
                    help="advisory lint counts as a failure too")
    ap.add_argument("--only", help="comma-separated check names (substring match)")
    ap.add_argument("--jobs", type=int, default=0,
                    help="browser checks to run at once (default: cpus/2, capped at 6). Each one "
                         "is a Chrome, so this is a memory setting as much as a speed one.")
    ap.add_argument("--list", action="store_true", help="print what would run, and stop")
    ap.add_argument("--brief", action="store_true",
                    help="print ONLY a short fixed-format report between BEGIN/END markers, for a "
                         "small model to relay verbatim (see docs/TESTING.md)")
    ap.add_argument("--json", metavar="FILE", help="also write the full result as JSON")
    ap.add_argument("--tmp", default="", help="scratch dir for chrome profiles")
    args = ap.parse_args()

    checks = discover()
    suites = list(SUITES)
    groups = set(args.group or (["unit", "client", "ui", "lint"]
                                + (["live"] if args.live else [])))
    if args.only:
        want = [w.strip() for w in args.only.split(",") if w.strip()]
        checks = [c for c in checks if any(w in c["name"] for w in want)]
        suites = [s for s in suites if any(w in s["name"] for w in want)]
        groups = {"unit", "client", "ui", "lint", "live"}
    checks = [c for c in checks if c["group"] in groups]
    suites = [s for s in suites if s["group"] in groups]

    chrome, node = have_chrome(), have_node()
    tmp = pathlib.Path(args.tmp or os.environ.get("PC_CHECK_TMP")
                       or f"/tmp/pc-checkall-{os.getpid()}")
    tmp.mkdir(parents=True, exist_ok=True)

    if args.brief:                       # the model's copy must not be coloured
        for k in C:
            C[k] = ""
    # --brief prints ONE fixed block and nothing else, so a small model relaying it cannot
    # accidentally summarise, reorder or drop a line. Everything the humans read is silenced.
    say = (lambda *a, **k: None) if args.brief else print
    say(f"{C['bold']}PosterChanAI — full check suite{C['off']}")
    say(f"  python  {PY}")
    say(f"  chrome  {chrome or C['yel'] + 'NOT FOUND — every browser check will SKIP' + C['off']}")
    say(f"  node    {node or C['yel'] + 'NOT FOUND — tests/client will fail' + C['off']}")
    say(f"  live    {args.live or C['dim'] + '(not given — live checks will SKIP)' + C['off']}")
    say(f"  running {len(suites)} suite(s) + {len(checks)} check(s)\n")

    if args.list:
        for s in suites:
            say(f"  {s['group']:<7} {s['name']}")
        for c in checks:
            mark = "" if c["registered"] else "  (UNREGISTERED — defaults applied)"
            say(f"  {c['group']:<7} {c['name']}{mark}")
        return 0

    t0 = time.time()
    results = []

    def report(res):
        v, why = verdict(res)
        col = {"PASS": C["grn"], "FAIL": C["red"], "SKIP": C["yel"], "LINT": C["cya"]}[v]
        detail = why or summarise(res)
        mark = "" if res.get("registered", True) else f" {C['yel']}[unregistered]{C['off']}"
        say(f"  {res['group']:<7} {res['name']:<34} {col}{v}{C['off']} "
              f"{res['secs_took']:6.0f}s  {C['dim']}{detail[:110]}{C['off']}{mark}")
        res["verdict"] = v
        res["detail"] = detail
        results.append(res)
        if args.brief:
            # --brief keeps STDOUT to exactly one block so a small model cannot garble the report.
            # It also meant ten minutes of total silence, and silence is indistinguishable from a
            # hang: this run was reported as "hung at the git clone step" twice while it was simply
            # working (the clone had finished in 5s). So liveness goes to STDERR — the agent merges
            # stderr into a job's captured output (stderr=STDOUT in node_service), a human tailing it
            # sees progress, and the report on stdout is untouched. The prompt asks the model for the
            # text BETWEEN the markers, so these lines cannot reach the relayed report.
            print(f"[{time.time() - t0:5.0f}s] {v:<4} {res['name']}", file=sys.stderr, flush=True)

    # The pytest suites first and one at a time: they are the cheapest signal, they need no browser,
    # and a broken import there makes every browser check meaningless anyway.
    for s in suites:
        report(run_suite(s, tmp))

    runnable = [c for c in checks]
    if not chrome:
        # Say it once, per check, rather than letting 30 browsers fail to start.
        for c in runnable:
            report(dict(c, secs_took=0.0, code=2, cmd=str(c["path"]),
                        out="SKIP no Chrome on this machine — install chromium, or use ./test.sh --docker"))
        runnable = []
    if runnable and not args.live:
        for c in [c for c in runnable if c["group"] == "live"]:
            report(dict(c, secs_took=0.0, code=2, cmd=str(c["path"]),
                        out="SKIP needs a running instance — re-run with --live <URL>"))
        runnable = [c for c in runnable if c["group"] != "live"]

    jobs = args.jobs or _default_jobs()
    if runnable:
        serial = [c for c in runnable if c.get("serial")]
        parallel = [c for c in runnable if not c.get("serial")]
        gentle = (not args.jobs) and _serving_live_traffic()
        say(f"  {C['dim']}…{len(runnable)} browser check(s), {jobs} at a time"
            + (" (this node is serving live traffic — throttled; --jobs N overrides)" if gentle else "")
            + (f", {len(serial)} memory-heavy check(s) serialized" if serial else "")
            + f"{C['off']}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            futs = {pool.submit(run_one, c, args.live, tmp, i): c
                    for i, c in enumerate(parallel)}
            for f in concurrent.futures.as_completed(futs):
                report(f.result())
        for i, c in enumerate(serial, start=len(parallel)):
            report(run_one(c, args.live, tmp, i))

    took = time.time() - t0
    passed = [r for r in results if r["verdict"] == "PASS"]
    failed = [r for r in results if r["verdict"] == "FAIL"]
    skipped = [r for r in results if r["verdict"] == "SKIP"]
    lint = [r for r in results if r["verdict"] == "LINT"]
    if args.strict:
        failed, lint = failed + lint, []
    unreg = [r for r in results if not r.get("registered", True)]

    say("\n" + "─" * 78)
    say(f"  {len(results)} checks   "
          f"{C['grn']}{len(passed)} passed{C['off']}   "
          f"{(C['red'] if failed else C['dim'])}{len(failed)} failed{C['off']}   "
          f"{(C['yel'] if skipped else C['dim'])}{len(skipped)} skipped{C['off']}   "
          f"{(C['cya'] if lint else C['dim'])}{len(lint)} advisory{C['off']}"
          f"      {took/60:.1f} min")

    if failed:
        say(f"\n{C['red']}{C['bold']}FAILED{C['off']}")
        for r in failed:
            say(f"  {C['red']}✗{C['off']} {r['name']} — {r['detail']}")
            say(f"      {C['dim']}rerun:{C['off']} {PY} {r['cmd']}")
    if skipped:
        # Loud on purpose. A suite that skipped half of itself and printed a green total is the
        # thing this file exists to make impossible.
        say(f"\n{C['yel']}{C['bold']}SKIPPED — these were NOT run{C['off']}")
        for r in skipped:
            say(f"  {C['yel']}–{C['off']} {r['name']} — {r['detail']}")
    if lint:
        say(f"\n{C['cya']}{C['bold']}ADVISORY — worth fixing, does not block a deploy{C['off']}"
              f" {C['dim']}(--strict makes these fail){C['off']}")
        for r in lint:
            say(f"  {C['cya']}~{C['off']} {r['name']} — {r['detail']}")
            say(f"      {C['dim']}rerun:{C['off']} {PY} {r['cmd']}")
    if unreg:
        say(f"\n{C['yel']}Not in scripts/checkall.py's table (run with defaults):{C['off']} "
              + ", ".join(r["name"] for r in unreg))
        say("  Add an entry if it needs a live instance or longer than "
              f"{DEFAULT_SECS}s, or it will fail here for the wrong reason.")

    # THE INTERPRETER, SAID OUT LOUD — last, where the verdict is read.
    #
    # Every "websockets not installed" skip and every ModuleNotFoundError above has ONE cause when
    # it happens in bulk, and it is not the code under test: this run is driving a python that
    # cannot import what a check needs. From a git worktree that used to be silent and total — 45
    # of 85 checks gone, a board in eight seconds, and nothing on screen naming the interpreter.
    equipped, missing = _interpreter_is_equipped()
    if not equipped:
        say(f"\n{C['red']}{C['bold']}THE CHECKS RAN UNDER AN INTERPRETER THAT CANNOT RUN THEM"
            f"{C['off']}")
        say(f"  interpreter: {PY}")
        say(f"  missing:     {missing}")
        say("  Every skip and ModuleNotFoundError above is this, not the code. Point it at the "
            "venv that\n  has the dependencies (venv-unified/bin/python), or install them here. "
            "Until then this\n  board is not a release gate.")

    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(
            [{k: v for k, v in r.items() if k != "path"} for r in results], indent=2, default=str))
        say(f"\n  json → {args.json}")

    if args.brief:
        # THE AGENT REPORT. Rendered HERE, in Python, from what was measured — never described by a
        # model. This repo's whole experience with the /logs health board is that a small model
        # gathers reliably and RETELLS badly: it called a healthy 3-of-3 array degraded, invented a
        # swap partition on a host with none, and silently dropped a drive it had been given. So the
        # node agent's only job is to run one command and paste what comes back between these two
        # markers, and the prompt in docs/TESTING.md asks for exactly that and nothing else.
        print("=== POSTERCHAN CHECK REPORT BEGIN ===")
        print(f"result: {'FAIL' if failed else 'PASS'}")
        print(f"host: {os.uname().nodename}")
        print(f"commit: {git_head()}")
        print(f"totals: {len(passed)} passed, {len(failed)} failed, "
              f"{len(skipped)} skipped, {len(lint)} advisory, {took/60:.1f} min")
        for r in failed:
            print(f"FAILED: {r['name']} — {r['detail'][:150]}")
        for r in skipped:
            print(f"SKIPPED: {r['name']} — {r['detail'][:150]}")
        for r in lint:
            print(f"ADVISORY: {r['name']} — {r['detail'][:150]}")
        if not failed and not skipped:
            print("no failures, nothing skipped")
        print("=== POSTERCHAN CHECK REPORT END ===")
        return 1 if failed else 0

    print()
    if failed:
        print(f"{C['red']}{C['bold']}FAIL{C['off']} — {len(failed)} check(s) went red. "
              f"Do not deploy.")
        return 1
    if skipped:
        print(f"{C['grn']}PASS{C['off']} — everything that RAN is green, "
              f"{C['yel']}but {len(skipped)} check(s) never ran{C['off']}.")
        return 0
    if not passed:
        print(f"{C['yel']}nothing ran.{C['off']}")
        return 0
    print(f"{C['grn']}{C['bold']}PASS{C['off']} — all {len(passed)} checks green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
