# Testing — one command, and what it means

```bash
./test.sh                               # everything this machine can check on its own
./test.sh --live https://poster.place   # …plus the checks that need a running instance
./test.sh --docker                      # all of it in a container, nothing published to the host
```

Exit code 0 means nothing failed. Run it **before** `./sync.sh`, and again after, on the node.

## Required deployment regressions

`sync.sh` runs `scripts/deploy_regression_gate.py` before committing, pushing, updating the
overlay, or restarting services. The Linux desktop build runs the same gate before packaging. Changes anywhere under `tests/`
trigger that build; a regression check verifies that every required test remains covered by
the workflow trigger.
It covers native window reloads, PDF/image controls, the Office-to-Files round trip, and
Folder Sync ownership, cancellation, and request timeouts. Files checks cover all nine
themes, readable controls, and saved theme changes reaching an already-open Files window
without losing its directory. Deployment harnesses reject failed commits or production pushes
before the mirror or nodes can advance. Each test uses isolated profiles
and test filesystem/network adapters.

Failures, collection errors, skipped tests, missing reports, and empty runs all block release.
`SKIP_LINT` does not bypass this gate. Developer pytest filters and alternate-source overrides
are cleared so the tests exercise the checkout being deployed. Install its Python dependencies
from `scripts/deploy-regression-requirements.txt`; Node, a JDK, Chrome, Electron, and Xvfb are also
required. The gate also checks reminder history retention in the API and client cache, including offline
startup, account isolation, stale deliveries and changes to the configured history window.
Real Chrome checks open the desktop notification centre, preserve unread acknowledgement across
offline reloads, follow Calendar links, update open shell/native panels after delayed history
responses, and preserve focus and scroll. Coordinate clicks distinguish an author from their post, including an update arriving between
mouse-down and mouse-up. Releasing outside the row must update the panel without opening anything.
Browser startup checks cover delayed DevTools discovery with a bounded retry; UI actions and
assertions are never retried to hide a failure.
The API checks load the shipped router and models with isolated database metadata and deny
production connections, so the same coverage runs in the minimal desktop build environment.
The required list is a fast minimum that also runs in the desktop CI build. **`sync.sh` runs the
gate with `--full`**, which then runs EVERY discovered `tests/**/test_*.py` file in parallel pytest
shards (as many as `checkall.py` would run browsers at once — fewer on a node serving live traffic),
balanced by per-file durations cached in `~/.cache/posterchanai/test-durations.json`. In the full pass
skips are allowed (hardware/tool-dependent tests skip honestly) but any failure, error, collection
error or shard that does not finish blocks the deploy, names the failing tests, and removes the
receipt. There is no flag to skip it: the required list alone let unlisted tests fail for days against
shipped code while every deploy passed. A full serial run was 36 minutes (2026-09-16).

The gate fingerprints the tested backend, client, desktop, Android app, templates, scripts, tests and release workflows
before and after running. `sync.sh` checks that receipt again immediately before committing.
Edits or a concurrent commit invalidate it; new source files must be staged so `git commit -a`
cannot silently omit them. The overlay package pin can still update after tests because it does
not change these tested inputs.

The gate runs pytest in its own process group and cleans up that group on completion, timeout,
Ctrl-C or termination. Its native display and Electron fixtures inherit that group, so aborting
a check does not leave those test processes running. Cleanup targets that run's processes only.

---

## Why this exists

The checks were all here already — 36 browser-driven `scripts/check_*.py` and ~2600 tests — and that
was the problem. Nobody remembers 40 command lines, so in practice two or three got run before a
deploy and the rest were found broken by a user. **A suite that is not one command is not a suite.**

Three rules it is built on, each learned here the hard way:

**A skip is not a pass.** A check that could not run — no Chrome, no instance URL, no websockets — is
printed in its own colour, counted separately, and named at the bottom with the reason. The board
this replaces could show green having covered nothing.

**Nothing is silently left out.** The check list is *discovered from the filesystem*, not typed into
the runner. A new `scripts/check_*.py` joins the suite the moment it is written. The table in
`scripts/checkall.py` carries only what cannot be inferred — does it need a live instance, how long
to allow — and anything discovered without an entry is run anyway and reported as `[unregistered]`.
Every hand-maintained parallel list in this repo has been out of date at least once, so this one is
not hand-maintained.

**A failure must be re-runnable.** Every failed row prints the exact command to reproduce it.

---

## What runs

| group | what | needs | time |
|-------|------|-------|------|
| `unit` | `pytest tests/` — services, routers, relay, media | nothing | ~3.5 min |
| `client` | `pytest tests/client/` — the shipped client JS, run under node against stubs | node | ~5.5 min |
| `ui` | 20 browser checks that serve the real `static/` themselves and drive headless Chrome | chrome | ~2 min (parallel) |
| `lint` | advisory — real findings that are not "does the app work" | nothing | seconds |
| `live` | browser checks against a REAL running instance | `--live URL` | ~15 min |

The `ui` group is where the UI regressions actually get caught: mobile layout at 360/390px, the
Meme Builder, the windowed desktop, Notes, Calendar, Contacts, Mail, the vault, Web Search, the
Files explorer, the composer and quote modals, the terminal, the browser extension. They need no
server, no keys and no network — they are the ones to run on every change.

`live` needs a URL because those checks log in with throwaway keys and talk to real relays. They are
the slowest and the only ones that can go red for reasons outside this checkout.

### Useful flags

```bash
./test.sh --group ui                  # just the browser checks
./test.sh --only os_desktop,notes     # by name (substring match)
./test.sh --list                      # what would run, and stop
./test.sh --jobs 2                    # fewer Chromes at once (each is ~400MB)
./test.sh --strict                    # advisory lint counts as a failure
./test.sh --json /tmp/checks.json     # machine-readable, for CI
```

---

## Docker

```bash
./test.sh --docker
```

Builds `posterchanai-test` (chromium + node + the app's non-AI dependencies) and runs the suite
inside it. Use it when you do not want to install Chrome on a node, or when you want the same answer
on a laptop, a node and in CI.

**It publishes no ports.** Every listener the checks open — the throwaway static servers, Chrome's
debugging port — binds inside the container. It is safe to run on a node already serving
PosterChanAI on 3051. There is no `-p` in `scripts/test-docker.sh` and there must never be one.

The repo is **bind-mounted, not copied**, so it checks the tree you are about to deploy, uncommitted
work included. The image is rebuilt automatically when either requirements file changes.

Two things the container does differently, both deliberate:

- The **AI stack is not installed** (no torch, no llama-cpp). ~80 tests skip themselves accordingly
  and say so. The image would be 8GB otherwise and nobody would rebuild it.
- It runs as **root**, which is why `--shm-size=1g` matters: Chrome's default 64MB `/dev/shm` makes
  tabs die under load, and that reads as a flaky check rather than the resource limit it is.

---

## Running it from the node agent (AI Chat)

The goal is to ask any node to check itself and report back. The model's **only** job is to run one
command and paste what comes back — it must never summarise, reformat or judge the result.

That is not caution for its own sake. This repo already learned it with the `/logs` health board: a
small model *gathers* reliably and *retells* badly. It called a healthy 3-of-3 RAID array
"degraded", invented 2GB of swap on a host with none, reported a `/raid` mount that does not exist,
and dropped a drive from a list it had been handed. So `--brief` renders the report **in Python**,
from what was measured, between two markers — and the prompt asks for the markers.

### On the node, or in the sandbox — and the difference is not isolation

Both work. `posterchanai-sandbox:3` carries chromium, node and the app's non-AI dependencies, so an
agent can run the suite inside its own throwaway container:

```
git clone --depth 1 https://github.com/loblawbob873-svg/posterchanai /tmp/pc
cd /tmp/pc && ./test.sh --brief
```

The sandbox has no host filesystem by design, so it clones the **public mirror** rather than reading
the node's checkout. That is the real difference, and it is not in the sandbox's favour: a sandbox
run reports on `main`, not on what the node is serving. If the node is behind, or carries
uncommitted work, the sandbox will report PASS for code that is not running anywhere. The `commit:`
line exists to make that visible — read it every time.

Running on the node checks the tree that is actually serving:

```
cd ~/posterchanai && ./test.sh --brief
```

…and if what you wanted from the sandbox was isolation, that is `--docker`, which gives its own
container and its own Chrome **while still checking the node's real tree**:

```
cd ~/posterchanai && ./test.sh --brief --docker
```

So: sandbox for a clean room, node for the truth about that node.

Baking the toolchain in was measured, not assumed: a bare `:2` container can fetch the repo with
nothing but stdlib and run the browser checks — after ~4 minutes of `apt-get` and `pip` on every
run, which is longer than the checks. The cost is image size, ~250MB → ~2.3GB. Bumping that tag
means editing all five places at once (`sandbox_service._DEFAULT_IMAGE`, `schemas`,
`scripts/install/sandbox.sh` ×2, the `Dockerfile.sandbox` comment), and the running app holds the
old name in memory — set `node_exec_sandbox_image` live in Admin → Services, or restart.

### The prompt

Paste this into the AI Chat agent (or `node <name> agent …`). It is written flat and short on
purpose: no branching, no judgement, no formatting decisions.

**Mind the agent's step timeout.** The agent AWAITS each command, so `node_exec_agent_step_timeout`
(Admin → Nodes → Agent Step Timeout) is a hard ceiling on this one. The suite MEASURES **10m22s** on
server1, and the old 600s default killed it 22 seconds short — with `--brief` printing one block at
the very end and nothing before it, what came back was **empty**. Ten minutes of apparently nothing,
then nothing, and an idle GPU throughout (the model only runs between steps). The default is 1800s
now; if you shorten it, shorten it to more than the suite takes.

For a run you don't want to sit inside the agent loop at all, the non-agentic form has **no** step
timeout, returns a job id after ~8s and posts the result back to the channel when it finishes —
which is what `node_service` itself recommends for long tasks:

```
node local cd ~/posterchanai && ./test.sh --brief
```

```
Run this command on the node and wait for it to finish. It takes up to 15 minutes.

cd ~/posterchanai && ./test.sh --brief

Then reply with the text between "=== POSTERCHAN CHECK REPORT BEGIN ===" and
"=== POSTERCHAN CHECK REPORT END ===", copied exactly, and nothing else.
Do not summarise it. Do not reformat it. Do not add your own opinion about whether it is good.
If the command printed no such block, reply with the last 20 lines of its output instead.
```

For a node whose checkout is at a different path (`/srv/posterchanai` on router.lan,
`~/posterchanai` on nas), change the `cd`. For the faster subset, use
`./test.sh --brief --group ui`.

### What comes back

```
=== POSTERCHAN CHECK REPORT BEGIN ===
result: PASS
host: server1
commit: a3535fe6 The start menu opened behind the windows, because a focus counter
totals: 22 passed, 0 failed, 0 skipped, 1 advisory, 11.4 min
ADVISORY: check_css_scale — 331 off-scale value(s).
no failures, nothing skipped
=== POSTERCHAN CHECK REPORT END ===
```

`commit:` is there because it is the whole question on a node. A green board for a commit two behind
production is worse than no board — that is the drift `sync.sh` now exits 1 on, and it is worth
seeing in the same block as the result.

`result:` is `FAIL` if and only if something failed. Skips and advisories never make it `FAIL`, and
both are printed by name so a run that covered less than you think cannot look like a clean one.

---

## Adding a check

Write `scripts/check_<thing>.py`. Exit **0** clean, **1** regressions (printed), **2** could not run.
That exit convention is what the suite reads — `2` becomes a SKIP with your message attached, never
a pass and never a failure of the code.

It joins the suite automatically. Add a line to `CHECKS` in `scripts/checkall.py` only if it needs a
live instance (`group="live"`) or more than the default 420s.

Two conventions to honour, both because the suite runs checks concurrently:

- Read the chrome debugging port from **`PC_CHECK_PORT`** and the profile dir from
  **`PC_CHECK_PROFILE`**, falling back to your own defaults:
  ```python
  PORT = int(os.environ.get("PC_CHECK_PORT") or 9473)
  PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-mything-check"
  ```
  The ports used to be hardcoded and four scripts shared 9473, so two running at once attached to
  one browser.
- **Never write into the working tree's live state** — `streamserver/`, `caldav-data/`, the
  database. Use pytest's `tmp_path` or a temp dir. A test that touched `streamserver/mediamtx.pid`
  passed on a laptop and failed with PermissionError on every node that was actually serving, which
  is the machine where the answer matters.

### And make sure the check can FAIL

Every check here is written by breaking the thing first. Before you trust a new assertion, put the
bug back — revert the fix, or patch the shipped file in place — and confirm the check goes red. An
assertion that cannot fail is worse than no assertion: it is a green row that says the area is
covered.

**Then make the suite prove it, every run.** By hand is a one-off; `tests/
test_the_suite_can_actually_fail.py` is the same proof run for ever. Add a line naming the file, a
single-occurrence needle, the replacement that reintroduces the bug, and the test that must go red:

```python
(
    "the theme's CRT sheet goes back on top of every photo, document and wallpaper",
    "static/css/client.css",
    ".scanlines{position:fixed;inset:0;pointer-events:none;z-index:-1;",
    ".scanlines{position:fixed;inset:0;pointer-events:none;z-index:9990;",
    "tests/client/test_decoration_never_crosses_a_document.py",
),
```

It mutates a throwaway worktree (never the working tree — concurrent browser checks read these files
from disk), runs the target **clean first** and requires exit 0, then applies the mutation and
requires exit **1** exactly. Both halves are load-bearing and both were added after they went wrong
here:

- **the clean run** — every assertion below it reads an exit code, and a test that cannot RUN in the
  worktree returns a non-zero one for reasons that have nothing to do with the mutation. Without it,
  an entry whose target had been renamed reported that the suite catches a bug it had never seen.
- **exactly 1** — pytest answers `5` for "collected no tests", `4` for a usage error and `2`/`3` when
  it breaks. Accepting any non-zero read every one of those as proof that the guard works.

**Name the file that exercises the BRANCH, not the one whose title matches.** The folder-sync entry
was first pointed at `test_delete_and_restore_symmetry.py`, which stubs `hasBlob: async () => true`
throughout — the store always confirms, so deleting the confirmation changed nothing there and the
entry passed while proving nothing. The refusal is owned by `test_fs_bridge.py` (`store_has="false"`
/ `"null"`) and `exec_sim.js`. The harness found that itself, on its first run, which is the point.

Keep the list short and load-bearing: each entry costs two pytest subprocesses, and a slow suite is
a suite people skip.

Mutation probes reuse pytest's current interpreter so they also run in worktrees and containers
without a local `venv-unified/`. Each probe has a five-minute timeout; unexpected results include
the last 8 KB of pytest output. The snapshot includes binary diffs as well as staged new tests.

### Service regression coverage

- Scheduled posts: cancellation during a publish pass, retry/recovery, ownership, pruning, and
  UTC timestamps on a forced non-UTC host.
- Saved searches: account isolation, duplicate pins, query normalization, and length limits.
- GPU health: failed probes cannot trigger resets; Intel free memory is converted to used memory.
- XRP codec: real isolated SDK signing, wallet ownership, address/tag validation, and testnet
  rejection. Uses throwaway keys without broadcasting; skips explicitly when the SDK is absent.
- Screenshot comparisons disable animation so frame timing cannot masquerade as overlay damage.

The suite runner also isolates Python's bytecode cache for each run. `-B` prevents cache writes,
but cannot prevent stale cache reads by itself. A runtime test creates stale bytecode with the
same source timestamp and size, then verifies that the runner imports the updated source.

`check_gentoo_overlay` belongs to the live group: it compares the published overlay with the
checkout, so run it **after** publishing a package-pin change:
`./test.sh --only gentoo_overlay --live https://poster.place`.

Live search-rate, fresh-drive pairing, sync-card, public Concord invite, and signer-transport checks
also require `--live`. They run serially because they contact real services. Search-rate and signer
transport check this node's services; the Concord check uses its configured public invite. The two
account checks use the supplied instance URL. Default offline runs do not start these checks.

The signer, reconnect, and bulk-lane NIP-46 browser checks also require `--live`: their relay
fixtures are disposable, but their web page comes from the existing app at `http://127.0.0.1:3051`.
They retain that node-local HTTP endpoint when `--live` is supplied because their loopback `ws://`
fixtures would be blocked as mixed content from an HTTPS page. The supplied URL instead controls
`check_nostrconnect_remote_signer` (which also contacts Primal's relay), `check_files_home_navigates`,
`check_composer_survives_a_desktop_click`, and `check_os_window_controls_are_reachable`.

Every check that attaches to an existing Electron renderer requires `--live`: account/Office,
Admin prune-preview, native Files, System Settings, Code, Code focus, native focus, snap, and
handoff. They use CDP port 9223 and the renderer's configured account/backend, regardless of the
supplied URL. Even account-independent checks change focus or move desktop windows. Run them
against an isolated diagnostic renderer with disposable native windows. Installed ASAR extraction
checks remain in the default suite; they inspect packaged code without a live account. Explicit
`--only` selection does not bypass the `--live` requirement.

### Reliable selection and retry timing

Every comma-separated `--only` selector must match an available check or suite. Empty selectors
and typos exit 2 before executing anything, including when another selector is valid. Use `--list`
to find names. Negative `--jobs` values also fail before starting the suites.

One `checkall.py` run may own a repository at a time, including its linked worktrees. A competing
run exits 2 and names the owner's PID and checkout; wait for that run instead of starting duplicate
browsers. `--list` remains available. The operating system releases the advisory lock when the
runner exits or crashes; never delete the lock file to bypass a running suite. Direct focused
pytest commands remain available, so coordinate those with any active full run.

Ctrl-C cancels queued browser checks, stops each running check's own process group and exits 130.
It does not wait for the rest of the queue or signal unrelated application services.

Missing-event retry tests use a virtual timeout clock while executing the shipped JavaScript.
They preserve debounce, backoff, delayed query responses and recovery timing without waiting for
real minutes. The clock itself checks deadline ordering, cancellation and promise continuations.
Mutation probes require these tests to catch dropped retry queues and frozen sockets spending the
permanent retry budget. Production timers are unchanged. Suite logs also record the 20 slowest pytest cases, so future
slowdowns can be traced to individual tests.

---

## Webxdc realtime regression coverage

`tests/test_webxdc_realtime_backpressure.py` runs the complete shipped module with a deferred signer.
A movement burst must keep one request in flight, send the newest pending position, recover after
signer rejection and stop queued movement when the game closes. Native realtime must publish a
burst through all open relays using one local session key, without awaiting account approval.
These checks count requests and explicitly release promises; they do not depend on wall-clock
speed. They cover transport behavior, not actual Doom frame rate or end-to-end network latency.

## Calendar cache lifecycle

`tests/client/test_calendar_stale_cache.py` delays queue and snapshot reads across account changes
and newer calendar loads. Late results must not replace current state, repaint private events,
push a stale home-screen widget or start an abandoned refresh. Positive controls also verify that
current cached appointments and queued-write counts still appear. The tests execute the shipped
functions with explicitly resolved promises, without network requests or timing thresholds.

`tests/client/test_calendar_queue_ownership.py` also pauses offline writes at queue-read,
authentication, request and persistence boundaries. An account switch must preserve the original
owner's pending appointments and leave the new account's queue and badge alone. Flushes stop before
sending another account's data; interrupted writes remain retryable. Runtime controls verify edit
replacement, permanent refusal reporting, temporary failure retention and structured HTTP errors.

## Calendar defaults and moves

`tests/client/test_calendar_default_and_move_full_app.py` uses the bundled app in Chrome. It
checks saved defaults across page reloads and accounts, read-only or removed defaults, event
creation, moving a recurring event without losing its raw ICS, and refusing moves with pending
offline edits. An edited move retry advances the clock two minutes so a regenerated timestamp
cannot accidentally pass as the same payload.

`tests/test_calendar_move_api.py` registers the real FastAPI router with isolated authentication
and storage boundaries. Destination failures must retain the source; collisions and stale edits
must be refused. Partial copies remain visible to CalDAV, and an identical retry can finish
removing the source. Strict-read outages must not look like missing events. The storage API has
no atomic compare-and-delete, so these cases do not prove safety against every simultaneous
write from another node.

## Start-menu keyboard and browser cleanup

The required bundled-browser cases type Firefox, use Tab to select it, release delayed local
search results, and press Enter. The same action must retain focus and launch once. Focus rings
must stay inside the scrolling results and follow the theme accent. Browser setup and assertion
failures must still close Chrome before deleting its private profile; cleanup errors remain
failures rather than being ignored.

## Known standing state

- `check_css_scale` passes on the current stylesheet. It remains an **advisory** design-scale lint;
  `./test.sh --strict` makes future violations block the run.
- In the container, ~80 tests skip because the AI stack is not installed. They say so.


## OS update regression coverage

The installer refreshes its own dependency policy before resolving an update, preserving other
Portage configuration files. It checks a read-only world plan for skipped 32-bit rebuilds and
retains only the ABI flags named by those warnings. The parser excludes unrelated version pins,
including QEMU firmware, and clears its state before later binary-package diagnostics.

Bash execution tests cover the old-image Wayfire/glslang dependency settings, preservation of
operator configuration, bounded ABI convergence, and failed sync/resolution/update/cleanup steps.
A failed update cannot continue into package cleanup or bootloader changes. On a live desktop,
verify with a pretend update first and defer package installation or restarts as appropriate.

### Android instrumentation evidence

`scripts/android_instrumented.sh` runs `:app:connectedDebugAndroidTest` on the disposable
emulator. Gradle success alone does not pass this gate: it first removes previous connected
reports, then requires fresh, parseable JUnit XML with actual named test cases, consistent
counts, and zero failures, errors, or skips. A conditional assumption still appears with its
test name as incomplete coverage; it cannot silently become a passing device check.

Missing test sources or a lost/unavailable emulator return 2 (did not run). Gradle failures
retain their exit status; absent, empty, malformed, failed, or skipped result evidence returns
1. The final emulator workflow treats both 1 and 2 as unsuccessful. Reports and logcat remain
available in the existing `androidTest-report` artifact, and the verified count or evidence
failure is written to the job summary.

Run the isolated gate regression tests without an emulator:

```bash
venv-unified/bin/python -m pytest -q tests/test_android_instrumented_evidence.py tests/test_android_instrumented_permissions.py
```

These execute the shipped shell script with temporary fake adb/Gradle boundaries and real XML
files, including stale previous results, missing reports despite Gradle success, invalid
reports, skipped cases, genuine failures, and successful controls.

### SMS attachments and APK publication

The required deployment gate also executes native Java MMS callback, retry, and share-intent
checks. Ambiguous attachment I/O callbacks must remain unconfirmed; provider-confirmed success clears
older failure details, and a failed provider row cannot be overwritten by transport success. Android device tests exercise
saved attachment status and details, gallery routing, recipient selection, real activity recreation,
and replacement of a shared draft. These tests never submit a carrier message.

Before changing the rolling APK release or Zapstore listing, the APK workflow requires a successful
emulator run for the exact source commit and current run attempt. Its emulator job and device
verification steps must have actually succeeded. A failed, cancelled, skipped, or missing run blocks
publication and preserves the existing release. A manual APK build needs matching emulator evidence;
run the emulator workflow for that commit first if none exists. Emulator tests cannot prove delivery
through a real SIM and carrier.

MMS transport success is not the same as protocol acceptance. Android's
[request processing](https://android.googlesource.com/platform/packages/services/Mms/+/refs/tags/android-mainline-11.0.0_r13/src/com/android/mms/service/MmsRequest.java)
can retain the transport result while
[SendConf processing](https://android.googlesource.com/platform/packages/services/Mms/+/refs/tags/android-mainline-11.0.0_r13/src/com/android/mms/service/SendRequest.java)
marks the provider row failed. Regression cases preserve that failure and reject bare HTTP 2xx
as proof of acceptance when the callback itself is unconfirmed.

## Fullscreen game mouse input

`tests/test_wayfire_pointer_confinement_runtime.py` compiles the shipped plugin and starts a
private headless Wayfire session. Its separate Wayland client receives real relative-pointer
protocol events at both monitor edges, with different accelerated and raw deltas. Repeated raw
movement must remain unchanged; a native pointer lock must also preserve accelerated movement.
These tests require the Wayfire SDK, compiler, foot, and Wayland protocols. Run them before
publishing compositor changes; ordinary desktop CI does not have that SDK.

The custom cursor guard confines accelerated cursor displacement. It preserves raw input used
by XWayland raw-motion consumers. This does not prove every game's camera behavior or measure
frame rate; the affected game still needs validation on its desktop.
