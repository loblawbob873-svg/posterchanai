# Testing — one command, and what it means

```bash
./test.sh                               # everything this machine can check on its own  (~10 min)
./test.sh --live https://poster.place   # …plus the checks that need a running instance (~25 min)
./test.sh --docker                      # all of it in a container, nothing published to the host
```

Exit code 0 means nothing failed. Run it **before** `./sync.sh`, and again after, on the node.

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

### The prompt

Paste this into the AI Chat agent (or `node <name> agent …`). It is written flat and short on
purpose: no branching, no judgement, no formatting decisions.

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

---

## Known standing state

- `check_css_scale` reports ~330 off-scale CSS values. Real, worth paying down, **advisory**: it is
  a design-scale lint over `client.css`, not a question of whether the app works. A check that is
  red on every single run is a check everybody learns to scroll past — which is the same disease as
  a green board that covered nothing — so it has its own verdict and does not block a deploy.
  `./test.sh --strict` makes it fail if you want to hold the line.
- In the container, ~80 tests skip because the AI stack is not installed. They say so.
