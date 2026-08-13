"""Node management: run OS commands + an agentic loop toward a natural-language goal.

Transport is **Nostr-only** (SSH removed): a REMOTE node is a worker addressed by its npub —
the command rides an encrypted NIP-90 event to it (`run_agent_over_nostr` → `nostr_dvm.run_remote`),
the worker runs it LOCALLY and returns an encrypted result. The single exception is ``local``:
commands for THIS host run directly here as subprocesses (no round-trip to self). The node registry
is `node_exec_node_npubs` (`name npub…`); see `all_nodes()`. The system health report
(logs_scheduler) drives the same two paths.

Config lives in admin Settings:
  node_exec_enabled            "true"/"false"
  node_exec_node_npubs         one per line: name npub…   (the worker for each remote node)
  node_exec_trusted_npubs      controllers allowed to run commands on THIS host (worker side)
  node_exec_users              comma/newline-separated npubs allowed (first user/admin always allowed)
  node_exec_agent_max_steps    max LLM iterations in agentic mode
  node_exec_job_timeout        per-job timeout in seconds (0 = no timeout)
"""
import asyncio
import base64
import logging
import os
import re
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, TYPE_CHECKING

from sqlalchemy.orm import Session

from app.services import settings_store

if TYPE_CHECKING:
    from app.models import User
    from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)

# Cap stored output per job so a chatty command can't exhaust memory. Output beyond the
# inline threshold is delivered as a .txt attachment rather than truncated in the chat.
_MAX_OUTPUT = 1024 * 1024  # 1 MB retained per job
INLINE_LIMIT = 3500  # chars shown inline; longer output is also attached as a file
# Per-STEP cap for an agent run's play-by-play. Same idea as INLINE_LIMIT (and safely under Telegram's
# 4096-char message limit once the ``` fence and header are added); anything longer rides along as a
# .txt attachment rather than being thrown away.
STEP_INLINE_LIMIT = 3000

# In-memory job registry, shared across the process (guarded by _lock).
_lock = threading.Lock()
_jobs: dict[int, "Job"] = {}
_next_id = 1
_MAX_JOBS = 200  # keep the most recent finished jobs; prune older ones


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get(db: Session, key: str, default: str = "") -> str:
    return settings_store.get(key) or default


def is_enabled(db: Session) -> bool:
    return _get(db, "node_exec_enabled", "false").strip().lower() == "true"


def all_nodes(db: Session) -> dict[str, str]:
    """The Nostr-only node registry: ``name -> target`` where target is ``"local"`` (run directly on
    THIS host) or ``"nostr:<pkhex>"`` (a worker addressed over Nostr). Sourced from
    `node_exec_node_npubs`; any mapped npub equal to THIS host's own worker key becomes ``local`` (run
    here — no encrypted round-trip to self). The synthetic ``local`` is added ONLY when no NAMED node is
    already this host — otherwise the box was double-counted (once as ``local`` AND once as e.g.
    ``server1``), which ran the health-report agent on the same GPU twice. This is the single builder
    shared by the `node` command, `/api/node/state`, and the health report, so the node list can never
    drift or double-count a host."""
    from app.services import nostr_dvm
    me = (nostr_dvm.node_pubkey() or "").lower()
    out: dict[str, str] = {}
    have_self = False
    for name, pk in nostr_dvm.agent_node_map().items():
        if pk and pk.lower() == me:
            out[name] = "local"          # this host, addressed by its own name → run directly
            have_self = True
        else:
            out[name] = f"nostr:{pk}"
    if not have_self:
        out = {"local": "local", **out}  # nothing named IS this host → the synthetic entry represents it
    return out


def user_allowed(db: Session, user: Optional["User"]) -> bool:
    """Feature must be enabled AND the user must be the first user/admin or an allowlisted npub.

    The allowlist (`node_exec_users`) holds Nostr npubs (or hex pubkeys / nprofiles), one per line or
    comma-separated. The first signup (id==1) and any admin are always allowed — so the first user's
    own key is the de-facto default without seeding a value. Identities are compared by canonical
    pubkey hex via the shared `nostr_service.to_pubkey_hex` (same parser as auth/blossom/client)."""
    if user is None or not is_enabled(db):
        return False
    if getattr(user, "is_admin", False) or user.id == 1:
        return True
    from app.services.nostr import nostr_service
    me = nostr_service.to_pubkey_hex(getattr(user, "nostr_npub", None) or "")
    if not me:
        return False
    me = me.lower()
    raw = _get(db, "node_exec_users", "").replace("\n", ",")
    allowed = {h.lower() for h in
               (nostr_service.to_pubkey_hex(x.strip()) for x in raw.split(",") if x.strip()) if h}
    return me in allowed


def has_ai_access(user: Optional["User"]) -> bool:
    """AI features are gated by admin OR the admin-granted `can_ai` flag (mirrors auth.get_ai_user)."""
    return bool(user is not None and (getattr(user, "is_admin", False) or getattr(user, "can_ai", False)))


def sandbox_allowed(db: Session, user: Optional["User"]) -> bool:
    """True if this user may run agentic tasks in a per-user Debian sandbox: the feature is enabled AND
    they have AI access. This is how NON-admin AI users get agentic access (confined to their container),
    and admins can opt into it too. Docker availability is checked lazily at run time (sandbox_service)."""
    from app.services import sandbox_service
    return sandbox_service.enabled() and has_ai_access(user)


def _out_filename(label: str) -> str:
    """A safe .txt name for an attached step output, derived from the command/file label."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", (label or "output").strip())[:48].strip("-") or "output"
    return f"{base}.txt"


def tail(text: str, limit: int) -> str:
    """Return the LAST `limit` chars of command output. Command results put the
    meaningful part (final status, errors, summary) at the end, so we show the tail,
    not the head, when output is long."""
    text = text or ""
    if len(text) <= limit:
        return text
    return f"…(showing last {limit} chars)…\n{text[-limit:]}"


def _job_timeout(db: Session) -> Optional[float]:
    try:
        secs = float(_get(db, "node_exec_job_timeout", "0"))
    except ValueError:
        secs = 0.0
    return secs if secs > 0 else None


def _max_steps(db: Session) -> int:
    # 30, not the old 8. Eight was a context limit wearing a step limit's clothes: nothing ever
    # shrank the transcript, so a longer run overran the model's window. _digest_old_results /
    # _trim_to_budget fixed that, and a real task needs far more than eight tool calls.
    try:
        return max(1, int(_get(db, "node_exec_agent_max_steps", "30")))
    except ValueError:
        return 30


# How many times in a row the model may re-issue a command it has already run before we give up.
# Small models degenerate into re-running the same failing command; re-executing wastes steps and
# feeds back the same output it's already looping on, so we nudge instead and bail if it persists.
_MAX_REPEAT_NUDGES = 5

# Same-ACTION (not same-string) loop control. The exact-match breaker above only fires on a byte-identical
# command, so it is blind to the way models actually spin: rewriting the same file, or re-running the same
# one-liner, with the body tweaked each time. A real run burned 20 steps re-writing one script through
# coincurve -> nostr_sdk -> manual bech32 without the breaker ever firing, because no two attempts were
# byte-identical. Raising _MAX_REPEAT_NUDGES could never have caught that — the counter was never
# incrementing. So we compare a SIGNATURE (the command with quoted/heredoc bodies stripped) instead.
_SIG_WARN = 3    # nth attempt at the same action → tell the model plainly that it is going in circles
_SIG_MAX = 6     # nth → stop; it is not converging and the remaining steps are wasted GPU


def _cmd_signature(cmd: str) -> str:
    """Collapse a command to the ACTION it performs, dropping the payload that varies between attempts.

    `cat > /app/x.py << 'EOF' <200 lines>`  ->  `cat > /app/x.py`
    `cd /v && python3 -c "<any code>"`      ->  `cd /v && python3 -c ""`

    so N rewrites of the same file, or N runs of the same inline script, share one signature while
    genuinely different commands keep distinct ones."""
    s = (cmd or "").strip()
    cut = s.find("<<")                      # heredoc: everything from the operator on is the body
    if cut > 0:
        s = s[:cut]
    s = re.sub(r'"(?:[^"\\]|\\.)*"', '""', s)     # quoted payloads -> empty, keeping the flag shape
    s = re.sub(r"'(?:[^'\\]|\\.)*'", "''", s)
    return re.sub(r"\s+", " ", s).strip()[:160]


# Sentinel: start_job/run_to_completion fall back to the global job timeout when no override given.
_USE_JOB_TIMEOUT = object()


def _agent_default_step_timeout() -> float:
    """The declared default, read from SettingsResponse — never a second copy of the number.

    It WAS a second copy ("600" here, "600" there), and that is not a tidiness point: the value in
    production is BLANK, so every run took this fallback and the schema's value was decorative.
    Raising the documented default would have changed nothing at all."""
    from app.schemas import SettingsResponse
    try:
        return float(SettingsResponse.model_fields["node_exec_agent_step_timeout"].default)
    except Exception:
        return 1800.0


def _agent_step_timeout(db: Session) -> Optional[float]:
    """Per-command bound for the AGENT loop. Unlike fire-and-forget jobs (which return after ~8s
    and notify on completion), the agent AWAITS each command, so an unbounded command would
    deadlock the whole loop and the caller. 0 -> fall back to the global job timeout (which may
    itself be unbounded - an explicit admin choice). Truly long fire-and-forget tasks should use the
    non-agentic `node <name> <cmd>` instead.

    The default has to outlast the longest command this repo asks an agent to run. That is the check
    suite, which MEASURES 10m22s; at the old 600s the agent killed it 22 seconds short, and since
    `./test.sh --brief` prints one block at the very end, what came back was empty."""
    fallback = _agent_default_step_timeout()
    try:
        secs = float(_get(db, "node_exec_agent_step_timeout", str(fallback)))
    except ValueError:
        secs = fallback
    return secs if secs > 0 else _job_timeout(db)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@dataclass
class Job:
    id: int
    node: str
    target: str
    command: str
    user_id: Optional[int]
    status: str = "running"  # running | done | failed | killed
    output: str = ""
    exit_code: Optional[int] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    _proc: Optional[asyncio.subprocess.Process] = None
    _task: Optional[asyncio.Task] = None
    _on_complete: Optional[Callable[["Job"], Awaitable[None]]] = None

    @property
    def done(self) -> bool:
        return self.status != "running"


async def _run(job: Job, timeout: Optional[float]) -> None:
    """Spawn the LOCAL process, stream merged output into job.output, and finalize status.

    Jobs only ever run on ``local`` now — remote nodes go over Nostr (`run_agent_over_nostr`), where the
    worker runs its own local job. A non-local target here is a routing bug, so fail loudly."""
    _sbx_uid = None       # set for sandbox jobs → refcount the container so the reaper can't pull it mid-exec
    _sbx_acquired = False  # True only AFTER acquire() incremented — so a cancel DURING acquire doesn't make
    try:                   # the finally decrement a CONCURRENT same-user run's hold (§4)
        # start_new_session=True puts each job in its OWN process group/session, so on
        # timeout/kill we can signal the whole group (the shell AND its children, e.g. a `sleep`
        # in `cmd && sleep 10`) instead of orphaning grandchildren. Also makes getpgid(pid)==pid,
        # so _terminate's killpg can only ever hit this job's group, never the service.
        if job.target == "local":
            proc = await asyncio.create_subprocess_shell(
                job.command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        elif job.target.startswith("sandbox:"):
            # Per-user Debian container — the command runs INSIDE it (docker exec), never on the host.
            from app.services import sandbox_service
            _sbx_uid = job.target.split(":", 1)[1]
            await sandbox_service.acquire(_sbx_uid)   # ensure + refcount (released in finally) so the
            _sbx_acquired = True                      # increment happened → the finally release is now paired
            proc = await asyncio.create_subprocess_exec(   # idle reaper / a concurrent run can't pull it out
                *sandbox_service.exec_argv(_sbx_uid, job.command),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        else:
            raise RuntimeError(f"unknown job target '{job.target}' (expect 'local' or 'sandbox:<uid>'; "
                               "remote nodes go over Nostr)")
        job._proc = proc

        async def pump() -> None:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                chunk = raw.decode("utf-8", "replace")
                # Keep only the tail once we exceed the cap.
                combined = job.output + chunk
                if len(combined) > _MAX_OUTPUT:
                    combined = "...[output truncated]...\n" + combined[-_MAX_OUTPUT:]
                job.output = combined
            await proc.wait()

        try:
            await asyncio.wait_for(pump(), timeout=timeout)
        except asyncio.TimeoutError:
            _terminate(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)  # reap so we don't leak the child
            except asyncio.TimeoutError:
                _signal_group(proc, signal.SIGKILL)
            if not job.done:  # don't clobber a kill that raced in
                job.status = "failed"
                job.output += f"\n[killed: exceeded {timeout:.0f}s timeout]"
        else:
            job.exit_code = proc.returncode
            if not job.done:  # may already be 'killed'
                job.status = "done" if proc.returncode == 0 else "failed"
    except asyncio.CancelledError:
        if job._proc:
            _terminate(job._proc)
        job.status = "killed"
        raise
    except Exception as e:
        logger.warning(f"[node] job #{job.id} ({job.node}) error: {e}")
        job.status = "failed"
        job.output += f"\n[error launching command: {e}]"
    finally:
        if _sbx_uid is not None and _sbx_acquired:   # release ONLY the hold this exec actually took (§4)
            try:
                from app.services import sandbox_service
                await sandbox_service.release(_sbx_uid)
            except Exception:
                pass
        if job.finished_at is None:
            job.finished_at = time.time()
        logger.info(f"[node] job #{job.id} {job.node!r} cmd={job.command!r} -> {job.status} (exit={job.exit_code})")
        # Deliver the result if a callback is registered. It may be attached after launch
        # (see notify_on_done), so read it under the lock to avoid racing registration.
        with _lock:
            cb = job._on_complete
            job._on_complete = None  # deliver at most once
        if cb:
            try:
                await cb(job)
            except Exception as e:
                logger.warning(f"[node] on_complete for job #{job.id} failed: {e}")


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Signal the job's whole process group (jobs start their own session), so a shell's children
    die too instead of orphaning. Falls back to signalling just the process if the group lookup
    fails. Safe: each job is its own session leader, so the group only contains that job's tree."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, Exception):
            pass


def _terminate(proc: asyncio.subprocess.Process) -> None:
    _signal_group(proc, signal.SIGTERM)


def start_job(db: Session, node: str, target: str, command: str,
              user_id: Optional[int] = None,
              on_complete: Optional[Callable[["Job"], Awaitable[None]]] = None,
              timeout=_USE_JOB_TIMEOUT) -> Job:
    """Create a job and launch it as a background task. Returns immediately.

    `timeout` overrides the per-job kill timeout (seconds; None = unbounded). Defaults to the
    global node_exec_job_timeout; the agent passes its own bound so a command can't hang the loop."""
    global _next_id
    _timeout = _job_timeout(db) if timeout is _USE_JOB_TIMEOUT else timeout
    with _lock:
        job = Job(id=_next_id, node=node, target=target, command=command, user_id=user_id)
        job._on_complete = on_complete
        _jobs[job.id] = job
        _next_id += 1
        # Prune oldest finished jobs so the registry can't grow without bound.
        if len(_jobs) > _MAX_JOBS:
            for jid in sorted(j.id for j in _jobs.values() if j.done)[: len(_jobs) - _MAX_JOBS]:
                del _jobs[jid]
    job._task = asyncio.create_task(_run(job, _timeout))
    return job


def notify_on_done(job: Job, cb: Optional[Callable[["Job"], Awaitable[None]]]) -> None:
    """Register a completion callback on an already-running job. Used by callers that
    return a job's output inline if it finishes fast, and only want the callback to fire
    for jobs still running after that wait — avoiding double delivery. If the job already
    finished (narrow race), deliver immediately here instead."""
    if cb is None:
        return
    with _lock:
        if not job.done:
            job._on_complete = cb
            return
    asyncio.create_task(cb(job))


async def await_job(job: Job, wait: float = 8.0) -> Job:
    """Wait up to `wait` seconds for a job to finish (used so fast commands return inline)."""
    if job._task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(job._task), timeout=wait)
        except asyncio.TimeoutError:
            pass
    return job


async def run_to_completion(db: Session, node: str, target: str, command: str,
                            user_id: Optional[int] = None, timeout=_USE_JOB_TIMEOUT) -> Job:
    """Start a job and wait for it to fully finish (respecting the configured/overridden timeout)."""
    job = start_job(db, node, target, command, user_id=user_id, timeout=timeout)
    if job._task is not None:
        try:
            await asyncio.shield(job._task)
        except asyncio.CancelledError:
            pass
    return job


# --- file download: pull a file's raw bytes OFF a node/sandbox/worker ------------------------------
# The agent can BUILD things in its sandbox (a script, a checkout, a whole project), but its /workspace
# is a Docker volume on the worker — the user had no way to get a produced file back out. `node get`
# closes that loop: it runs a tiny reader ON THE TARGET (same executor as run_command / the file tools,
# so it works identically on the host, in a sandbox, and on a Nostr worker), and the caller hands the
# bytes to the chat's `type:files` path → an encrypted Blossom artifact + a download link.
#
# The reader base64-encodes the file so no byte of content is ever seen by the shell (same discipline
# as agent_file_tools), and REFUSES anything above _DOWNLOAD_MAX up front: a job retains at most
# _MAX_OUTPUT (1 MB), and base64 inflates ~4/3, so a larger file would be silently truncated -> corrupt.
# 700 KB * 4/3 ≈ 933 KB stays safely under the cap. (Bigger files: read them in the sandbox, or split.)
_DOWNLOAD_MAX = 700 * 1024
_DOWNLOAD_PROG = (
    "import sys,base64,os\n"
    "p=base64.b64decode(sys.argv[1]).decode('utf-8','surrogateescape')\n"
    "if not os.path.isfile(p):\n"
    "    sys.stdout.write('PCAI_NOFILE'); sys.exit(0)\n"
    f"if os.path.getsize(p)>{_DOWNLOAD_MAX}:\n"
    "    sys.stdout.write('PCAI_TOOBIG:%d'%os.path.getsize(p)); sys.exit(0)\n"
    "sys.stdout.write('PCAI_B64:'+base64.b64encode(open(p,'rb').read()).decode('ascii'))\n"
)
_DOWNLOAD_PROG_B64 = base64.b64encode(_DOWNLOAD_PROG.encode("utf-8")).decode("ascii")


def download_command(path: str) -> str:
    """Shell command that emits `PCAI_B64:<base64>` for `path` on the target (or PCAI_NOFILE /
    PCAI_TOOBIG). Program and path are both base64, so the shell never touches the file content
    or the path's own quoting."""
    _path_b64 = base64.b64encode((path or "").encode("utf-8")).decode("ascii")
    return f"python3 -c \"$(printf %s '{_DOWNLOAD_PROG_B64}' | base64 -d)\" '{_path_b64}'"


def decode_download(output: str) -> tuple[Optional[bytes], str]:
    """Parse `download_command`'s output into (bytes, "") on success, or (None, reason)."""
    out = output or ""
    idx = out.find("PCAI_B64:")
    if idx < 0:
        if "PCAI_NOFILE" in out:
            return None, "no such file (it's missing, or a directory) on the target."
        m = re.search(r"PCAI_TOOBIG:(\d+)", out)
        if m:
            return None, (f"file is {int(m.group(1)):,} bytes — too large to fetch in one go "
                          f"(limit {_DOWNLOAD_MAX:,}). Split it, or read it in the sandbox.")
        return None, ("could not read the file — the target may lack python3, or the command "
                      f"errored:\n{tail(out, 400)}")
    payload = out[idx + len("PCAI_B64:"):].strip()
    if payload == "":
        return b"", ""          # a legitimately empty (0-byte) file — not an error
    m = re.match(r"[A-Za-z0-9+/=]+", payload)
    if not m:
        return None, "the download payload was malformed."
    try:
        return base64.b64decode(m.group(0)), ""
    except Exception as e:
        return None, f"could not decode the download payload: {e}"


async def run_shell_on_target(db: Session, user: "User", node: str, target: str,
                              command: str) -> tuple[Optional[str], Optional[str]]:
    """Run ONE shell command on any node shape and return (output, error). Centralizes the
    local-job / Nostr-worker / placed-sandbox routing so `get`, `backup` and anything else read
    a target identically (the same three shapes `_node_command` resolves from the registry)."""
    from app.services import nostr_dvm
    try:
        if target.startswith("sandboxnostr:"):        # placed sandbox → its worker, inside the container
            _, _pk, _u = target.split(":", 2)
            out = await nostr_dvm.run_remote("agent", {"mode": "shell", "command": command, "sandbox_uid": _u},
                                             worker_pubkey=_pk)
        elif target.startswith("nostr:"):             # full Nostr worker (runs on its host)
            out = await nostr_dvm.run_remote("agent", {"mode": "shell", "command": command},
                                             worker_pubkey=target[len("nostr:"):])
        else:                                          # local host or local sandbox container
            job = await run_to_completion(db, node, target, command, user_id=getattr(user, "id", None))
            return job.output, None
    except Exception as e:
        return None, str(e)
    if not out:
        return None, "no response (worker offline, not trusting this controller, or timed out)"
    if out.get("error"):
        return None, out["error"]
    return out.get("output"), None


# --- directory archive: tar.gz a working dir OFF a node/sandbox, streamed back in chunks -----------
# `node get` pulls ONE file; the far more useful thing is "give me everything the agent built". This
# tars a directory (default the sandbox's persistent /workspace) to gzip ON THE TARGET, then streams
# it back in _MAX_OUTPUT-safe windows — so it isn't bounded by the single-file 700 KB cap and can carry
# a real project. The archive is staged to a temp file on the target (a stable name, because make/read/
# clean run as SEPARATE processes), read offset-by-offset, then removed. Each chunk rides the same
# PCAI_B64 transport decode_download already parses.
_ARCHIVE_MAX = 25 * 1024 * 1024   # gzipped ceiling — beyond this, grab a subfolder instead
_ARCHIVE_CHUNK = 512 * 1024       # raw bytes/read → base64 ≈ 683 KB, safely under _MAX_OUTPUT (1 MB)
_ARCHIVE_PROG = (
    "import sys,base64,os,tarfile\n"
    "d=base64.b64decode(sys.argv[1]).decode('utf-8','surrogateescape')\n"
    "op=sys.argv[2]; TMP='/tmp/pcai_ws_archive.tgz'\n"
    "if op=='make':\n"
    "    if not os.path.isdir(d): sys.stdout.write('PCAI_NODIR'); sys.exit(0)\n"
    "    t=tarfile.open(TMP,'w:gz'); t.add(d,arcname='.'); t.close()\n"
    "    sys.stdout.write('PCAI_SIZE:%d'%os.path.getsize(TMP))\n"
    "elif op=='read':\n"
    "    f=open(TMP,'rb'); f.seek(int(sys.argv[3])); b=f.read(int(sys.argv[4])); f.close()\n"
    "    sys.stdout.write('PCAI_B64:'+base64.b64encode(b).decode('ascii'))\n"
    "elif op=='clean':\n"
    "    try: os.remove(TMP)\n"
    "    except OSError: pass\n"
    "    sys.stdout.write('PCAI_CLEAN')\n"
)
_ARCHIVE_PROG_B64 = base64.b64encode(_ARCHIVE_PROG.encode("utf-8")).decode("ascii")


def _archive_cmd(directory: str, op: str, off: int = 0, n: int = 0) -> str:
    d_b64 = base64.b64encode((directory or "").encode("utf-8")).decode("ascii")
    args = f"'{d_b64}' '{op}'"
    if op == "read":
        args += f" '{int(off)}' '{int(n)}'"
    return f"python3 -c \"$(printf %s '{_ARCHIVE_PROG_B64}' | base64 -d)\" {args}"


async def archive_dir(db: Session, user: "User", node: str, target: str,
                      directory: str) -> tuple[Optional[bytes], str]:
    """tar.gz `directory` on the target and stream it back. Returns (bytes, "") or (None, reason)."""
    async def _run(op, off=0, n=0):
        return await run_shell_on_target(db, user, node, target, _archive_cmd(directory, op, off, n))

    out, err = await _run("make")
    if err:
        return None, err
    out = out or ""
    if "PCAI_NODIR" in out:
        return None, f"no such directory `{directory}` on the target."
    m = re.search(r"PCAI_SIZE:(\d+)", out)
    if not m:
        return None, ("could not build the archive (the target may lack python3, or tar failed):\n"
                      f"{tail(out, 400)}")
    size = int(m.group(1))
    if size <= 0:
        return None, "the archive came out empty."
    if size > _ARCHIVE_MAX:
        await _run("clean")
        return None, (f"archive is {size:,} bytes gzipped — over the {_ARCHIVE_MAX:,} limit. "
                      "Back up a subfolder instead: `node backup <name> <path>`.")
    buf = bytearray()
    while len(buf) < size:
        out, err = await _run("read", len(buf), _ARCHIVE_CHUNK)
        if err:
            await _run("clean")
            return None, err
        chunk, derr = decode_download(out)
        if derr:
            await _run("clean")
            return None, derr
        if not chunk:                    # no forward progress → never spin forever
            await _run("clean")
            return None, f"archive transfer stalled at {len(buf):,}/{size:,} bytes."
        buf += chunk
    await _run("clean")
    return bytes(buf), ""


def get_job(job_id: int, user_id: Optional[int] = None) -> Optional[Job]:
    """Fetch a job. If user_id is given, only return it when that user owns it
    (defense-in-depth so callers can't accidentally expose another user's job)."""
    job = _jobs.get(job_id)
    if job is None:
        return None
    if user_id is not None and job.user_id != user_id:
        return None
    return job


def list_jobs(user_id: Optional[int] = None, limit: int = 20) -> list[Job]:
    with _lock:
        jobs = list(_jobs.values())
    if user_id is not None:
        jobs = [j for j in jobs if j.user_id == user_id]
    jobs.sort(key=lambda j: j.id, reverse=True)
    return jobs[:limit]


def kill_job(job_id: int, user_id: Optional[int] = None) -> bool:
    job = _jobs.get(job_id)
    if not job or job.done:
        return False
    if user_id is not None and job.user_id != user_id:
        return False  # can't kill another user's job
    job.status = "killed"
    if job._proc:
        _terminate(job._proc)
    if job._task:
        job._task.cancel()
    return True


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------

_AGENT_SYSTEM = """You are a systems administrator managing the host "{node}".

Accomplish the user's goal with the tools below. Each tool's result is returned to you so you can
decide the next step.

- run_command — run a non-interactive shell command; returns its output and exit code.
- read_file — read a file with line numbers (paged; use offset to continue).
- edit_file — change part of a file by replacing an EXACT string.
- write_file — create a file, or replace one wholesale.
- grep — search file contents under a path.
- finish — stop, with a summary.

Rules:
- Prefer read-only / diagnostic steps first; make changes only when the goal requires it.
- Use the FILE TOOLS for files. Do not `cat`/`sed` a file you can read_file, and never rewrite a
  file with shell redirection to change a few lines — edit_file is what that is for.
- Always read_file before you edit_file: old_string must match the file byte for byte, and text
  retyped from memory will not match.
- If an edit fails, read the file again and copy the text exactly. Do NOT retry the same edit, and
  do NOT fall back to rewriting the whole file.
- One logical step per turn; wait for its result before the next.
- Never run interactive commands that wait for input (use flags like -y, --noninteractive).
- For anything needing more than two or three steps, call update_plan FIRST with the steps you
  intend to take, then keep it current as you go — mark a step done before starting the next.
- When the goal is achieved, or cannot be, call the finish tool with a short summary."""

# Re-stated into the SYSTEM message every turn (see _apply_plan). Deliberately not a mid-transcript
# "reminder" message: a trailing user/system turn after tool results is handled inconsistently by chat
# templates, whereas messages[0] is always valid and is what models attend to hardest.
_PLAN_HEADER = "\n\nYOUR CURRENT PLAN (keep it current with update_plan):\n"
_PLAN_MARK = {"done": "[x]", "doing": "[>]", "pending": "[ ]"}

# OpenAI-style tool schema for the agent. Driven through the same tool-calling path as opencode
# (chat_completion with tools -> generate_message), so it benefits from the native tool template
# and the <function-calls>/<tool_call> parsers.
_NODE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a non-interactive shell command on the managed host and return its combined stdout/stderr and exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Call when the goal is achieved or cannot be, with a short summary of what was found or done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Short summary of the outcome."},
                    "success": {"type": "boolean",
                                "description": "true only if the GOAL was actually achieved. false if you "
                                               "are giving up, blocked, or only partially done — say why in "
                                               "the summary."},
                },
                "required": ["summary", "success"],
            },
        },
    },
]

_NODE_TOOLS.append({
    "type": "function",
    "function": {
        "name": "update_plan",
        "description": "Record the steps you intend to take, and keep them current as you work. Call "
                       "this once up front for any multi-step task, then again whenever a step is "
                       "finished or the plan changes. Send the WHOLE list each time — it replaces the "
                       "previous one. Exactly one step should be 'doing' at a time.",
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": "The full plan, in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step": {"type": "string", "description": "Short description of the step."},
                            "status": {"type": "string", "enum": ["pending", "doing", "done"]},
                        },
                        "required": ["step", "status"],
                    },
                },
            },
            "required": ["steps"],
        },
    },
})


def _render_plan(plan: list) -> str:
    """The plan as a checklist, for both the system message and the user-facing play-by-play."""
    return "\n".join(f"{_PLAN_MARK.get(s.get('status'), '[ ]')} {s.get('step', '')}" for s in plan)


def _apply_plan(messages: list, base_sys: str, plan: list) -> None:
    """Rewrite the system message so it always carries the CURRENT plan. Rewriting (rather than
    appending a reminder turn) keeps exactly one copy in context no matter how many times the plan is
    updated, and survives _trim_to_budget — which never touches messages[0]."""
    messages[0]["content"] = base_sys + (_PLAN_HEADER + _render_plan(plan) if plan else "")


# Structured file tools (read/edit/write/grep) ride the same executor as run_command, so they act
# on whichever machine the agent is managing — host, sandbox container, or a Nostr worker. See
# agent_file_tools for why shelling out to heredocs was the agent's biggest source of wasted steps.
from app.services import agent_file_tools  # noqa: E402  (after _NODE_TOOLS so the list reads top-down)

_NODE_TOOLS = _NODE_TOOLS + agent_file_tools.FILE_TOOLS

# The READ-ONLY tool set, used by the two callers that must never change the machine:
#   * a sub-agent, which only INVESTIGATES — that is what makes it safe to delegate to without the
#     parent losing track of the machine's state (and no spawn_agent, so recursion stops at depth 1);
#   * the system-health report (report_mode), which is read-only diagnostics BY DEFINITION. It used to
#     be safe by accident, because the whole tool list was just run_command+finish; once file tools were
#     added it silently gained the ability to edit and write files on every node it audits. It gets
#     read_file/grep here, which genuinely help it read configs and logs, and nothing that mutates.
_READONLY_TOOLS = [t for t in _NODE_TOOLS
                   if t["function"]["name"] in ("run_command", "finish", "read_file", "grep")]
_MAX_SPAWNS = 3        # per run — a sub-agent costs real GPU steps, so it cannot be free-for-all
_SUB_MAX_STEPS = 12
# The health report runs on a CRON across every node, so its budget is pinned here rather than riding
# node_exec_agent_max_steps — raising that for interactive agent work must not quietly multiply the
# cost of a scheduled job. Still well above the 8 it effectively had before.
_REPORT_MAX_STEPS = 12

_NODE_TOOLS.append({
    "type": "function",
    "function": {
        "name": "spawn_agent",
        "description": "Delegate a self-contained READ-ONLY question to a helper agent that "
                       "investigates on its own and reports back one short answer. Use it when finding "
                       "something out would take several noisy commands whose full output you do not "
                       "need — 'which unit is listening on 8080 and what is its config path?'. The "
                       "helper cannot change anything, so do the actual work yourself afterwards. Give "
                       "it ONE specific question, with any context it needs, since it cannot see your "
                       "conversation.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "description": "The self-contained question, including needed context."},
            },
            "required": ["question"],
        },
    },
})


# --- context control -------------------------------------------------------------------------
# The transcript grows by one tool result per step, each up to _TOOL_RESULT_CHARS. Nothing ever
# shrank it, so a long run overran the model's context (the Arc is pinned at 32k) long before it
# ran out of steps — which is why max_steps had to be 8 to be safe. Ageing old results out is what
# makes a real step budget affordable.
_TOOL_RESULT_CHARS = 4000    # how much of a FRESH tool result the model sees
_KEEP_FULL_RESULTS = 3       # the most recent N results stay verbatim; older ones are digested
_ELIDED = "… [older output elided — read the file or run it again if you still need it]"
_TRIMMED_NOTE = ("\n\n[Earlier steps were dropped to fit the context window. Do not assume anything "
                 "you can no longer see — check again with a tool if you need it.]")


def _context_budget(db: Session) -> int:
    """Character budget for the whole transcript (~4 chars/token). Deliberately conservative: the
    model still needs room to generate, and the smallest node in the fleet sets the ceiling."""
    try:
        return max(4000, int(_get(db, "node_exec_agent_context_chars", "48000")))
    except ValueError:
        return 48000


def _digest_old_results(messages: list, keep_full: int = _KEEP_FULL_RESULTS) -> None:
    """Shrink tool results older than the last `keep_full`, in place. A step's full output matters
    while the model is acting on it and is dead weight ten steps later, so keep the recent ones
    verbatim and reduce the rest to their first line. Idempotent — already-digested messages carry
    the _ELIDED marker and are skipped, so this can run every turn."""
    idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in (idxs[:-keep_full] if keep_full else idxs):
        c = messages[i].get("content") or ""
        if len(c) <= 220 or c.endswith(_ELIDED):
            continue
        messages[i]["content"] = c.split("\n", 1)[0][:160] + "\n" + _ELIDED


def _trim_to_budget(messages: list, budget: int) -> int:
    """Drop the OLDEST exchanges until the transcript fits `budget` chars; returns how many went.

    Exchanges are dropped WHOLE (the assistant turn carrying tool_calls plus the tool results
    answering it): a tool message orphaned from its assistant turn is a malformed conversation for
    most backends, which would fail the request rather than merely lose history. The system prompt,
    the goal, and the two most recent exchanges are always kept."""
    def _size() -> int:
        return sum(len(m.get("content") or "") for m in messages)

    dropped = 0
    while _size() > budget:
        starts = [i for i, m in enumerate(messages)
                  if i >= 2 and m.get("role") == "assistant" and m.get("tool_calls")]
        if len(starts) <= 2:
            break                      # only the goal and the last two exchanges remain
        del messages[starts[0]:starts[1]]
        dropped += 1
    if dropped and len(messages) > 1 and _TRIMMED_NOTE not in (messages[1].get("content") or ""):
        messages[1]["content"] = (messages[1].get("content") or "") + _TRIMMED_NOTE
    return dropped


async def run_agent(db: Session, user: "User", node: str, target: str, goal: str,
                    chat_service: "ChatService", notify: Optional[Callable] = None,
                    report_mode: bool = False, should_stop: Optional[Callable] = None,
                    depth: int = 0) -> str:
    """Drive commands on `node` toward `goal` via native tool-calling. Returns a concise summary
    (header + the model's ✅ Done line); the per-command play-by-play is streamed live via `notify`
    and recorded in the node job log, not folded into the returned/persisted message.

    `report_mode` returns ONLY the model's final summary (no `## Agent on…` header, no `**✅ Done:**`
    prefix, no commands footer) so the caller can compose it into its own document — used by the
    agentic system-health report (logs_scheduler), where the model's finish summary IS the report.

    Uses the same tool-calling backend as opencode (chat_completion with tools -> generate_message),
    so the model emits structured run_command/finish calls instead of a fragile CMD:/DONE: text
    protocol. Defaults to Qwen3-Coder-30B-A3B-Instruct — the model that actually holds up over a long
    tool-calling run; falls back to the configured default when that gguf isn't present
    (resolve_model_path handles the fallback). `notify`, when given,
    streams each step to the originating channel (Telegram/web)."""
    import json as _json
    from app.services.inference_factory import get_inference_service

    # A sub-agent (depth>0) gets a read-only tool set and a small budget of its own, and always
    # returns just its summary — report_mode's return shape is exactly what the parent wants back.
    # The health report (report_mode) is read-only too, and separately budgeted (see _REPORT_MAX_STEPS).
    if depth:
        report_mode = True
    tools = _READONLY_TOOLS if (depth or report_mode) else _NODE_TOOLS
    if depth:
        max_steps = min(_SUB_MAX_STEPS, _max_steps(db))
    elif report_mode:
        max_steps = min(_REPORT_MAX_STEPS, _max_steps(db))
    else:
        max_steps = _max_steps(db)
    # Agentic model for the tool-call loop. Prefer the unified `llm_tools_model` (shared with the
    # /v1 agentic path); fall back to the legacy `node_exec_agent_model` for back-compat, then the
    # tuned default. Empty/missing gguf -> backend falls back to the default model (resolve_model_path).
    model = (_get(db, "llm_tools_model", "").strip()
             or _get(db, "node_exec_agent_model", "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf").strip()) or None
    service = get_inference_service(db)

    _sys = _AGENT_SYSTEM.format(node=node)
    if str(target).startswith("sandbox:"):
        # The agent is inside a fresh disposable Debian container (root). Steer it past the two things
        # that trip up package tasks here: Debian's PEP-668 externally-managed pip, and guessed package names.
        from app.services import sandbox_service as _sbx
        if _sbx.workspace_enabled():
            _sys += (f"\n\nWORKSPACE: {_sbx.WORKSPACE_DIR} is your persistent working directory and "
                     "survives between runs — keep checkouts, scripts and results there. Everything "
                     "OUTSIDE it is wiped when this run ends, so do not leave anything worth keeping "
                     "elsewhere.")
        _sys += ("\n\nENVIRONMENT: this host is a FRESH, disposable Debian container and you are root, so "
                 "install freely — but a bare `pip install` FAILS with 'externally-managed-environment'. "
                 "Either use a venv (`apt-get install -y python3-venv && python3 -m venv /venv && "
                 "/venv/bin/pip install <pkg>`) or `pip install --break-system-packages <pkg>`. VERIFY a "
                 "package actually exists (e.g. `pip index versions <pkg>`) before depending on it — do not "
                 "guess PyPI names. `apt-get update` once before installing system packages.")
    messages = [
        {"role": "system", "content": _sys},
        {"role": "user", "content": f"Goal: {goal}"},
    ]
    _allowed_tools = {t["function"]["name"] for t in tools}
    transcript = [] if report_mode else [f"## Agent on `{node}` — goal: {goal}\n"]
    cmds_run: list[str] = []   # for a footer on the stop/error paths so they're never blank
    cmd_outputs: dict[str, str] = {}   # command -> its last output, to detect/short-circuit repeats
    files_read: set = set()    # paths read this run — edit_file is refused until its file is in here
    plan: list = []            # the model's own checklist (update_plan), re-stated into messages[0]
    unverified: set = set()    # paths changed with nothing checked since — see the finish gate
    finish_held = False        # the "verify first" gate fires at most ONCE, so it can never deadlock
    spawns_used = 0            # sub-agents launched this run (capped: each costs real GPU steps)
    repeat_nudges = 0          # consecutive "you already ran this" nudges (reset by a fresh command)
    sig_counts: dict = {}      # command signature -> times attempted (see _cmd_signature): catches a model
                               # re-doing the same ACTION with a tweaked body, which repeat_nudges misses
    last_job_id = None

    async def _say(text: str):
        if notify:
            try:
                await notify(text)
            except Exception:
                pass

    async def _say_output(header: str, body: str, filename: str):
        """Report ONE step's output. Long output is shown up to STEP_INLINE_LIMIT chars AND attached in
        full as a .txt, instead of being silently cut to a few hundred characters.

        The old 700-char tail was the whole bug: the agent had the full output (it goes into the model's
        context), the job kept 1 MB of it, but the human watching the run could only ever see the last
        700 characters and had to know to type `node log <id>` — so a failure whose cause was 800 chars
        up simply wasn't visible. The attachment path already exists (`agent_files`, the same one
        workspace backups ride) and each interface stores it its own way, so nothing new is needed to
        deliver the rest."""
        body = body or "(no output)"
        await _say(f"{header}\n```\n{tail(body, STEP_INLINE_LIMIT)}\n```")
        if notify and len(body) > STEP_INLINE_LIMIT:
            try:
                await notify({"type": "agent_files",
                              "content": f"📄 full output of `{filename}` ({len(body):,} chars)",
                              "files": [{"filename": _out_filename(filename),
                                         "data": body.encode("utf-8", "replace"),
                                         "content_type": "text/plain"}]})
            except Exception as e:
                logger.warning(f"[node] full-output attach failed: {e}")

    def _footer() -> str:
        # The happy path returns the model's summary; on stop/error the concise transcript would
        # otherwise be empty, so point the user at what actually ran and where to find the output.
        if not cmds_run:
            return ""
        cmds = ", ".join(f"`{c}`" for c in cmds_run)
        tail_ref = f" Full output: `node log {last_job_id}`." if last_job_id is not None else ""
        return f"\nRan {len(cmds_run)} command(s): {cmds}.{tail_ref}"

    async def _exec(cmd: str):
        """Run one shell command on the target and return (exit_code, output). The file tools go
        through this too, so they act on the managed machine (host / sandbox / worker), never on
        the controller's filesystem."""
        nonlocal last_job_id
        job = await run_to_completion(db, node, target, cmd, user_id=getattr(user, "id", None),
                                      timeout=_agent_step_timeout(db))
        last_job_id = job.id
        return job.exit_code, job.output

    for step in range(1, max_steps + 1):
        # Cooperative cancel: a delete/kill sets this. Checked between steps so the run ALWAYS ends even
        # when task.cancel() is swallowed by run_to_completion's shield (§3) — the current command finishes,
        # then the loop bails here instead of grinding on for the GPU/step budget.
        if should_stop and should_stop():
            if report_mode:
                return "⏹️ cancelled"
            transcript.append(f"\n**⏹️ Stopped:** cancelled.{_footer()}")
            return "\n".join(transcript)
        # Live progress ping (not persisted) so a long run shows "working… step N/M" instead of looking dead
        # during the model-load gaps. report_mode (health board) has its own delivery, so skip it there.
        if notify and not report_mode:
            try:
                await notify({"type": "agent_progress", "node": node, "step": step, "max": max_steps})
            except Exception:
                pass
        # Keep the transcript inside the model's context BEFORE asking for the next step: old
        # results shrink to a line, and if that is still not enough the oldest exchanges go.
        _digest_old_results(messages)
        _trim_to_budget(messages, _context_budget(db))
        try:
            result = await service.chat_completion(
                messages=messages, model=model,
                tools=tools, tool_choice="auto", temperature=0.2,
            )
        except Exception as e:
            if report_mode:
                return f"⚠️ inference error: {e}"
            transcript.append(f"\n**⚠️ Stopped:** inference error: {e}{_footer()}")
            return "\n".join(transcript)

        msg = (result.get("choices") or [{}])[0].get("message", {}) or {}
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            # No tool call -> treat any text as the final answer/summary.
            content = (msg.get("content") or "").strip()
            if report_mode:
                return content or "(no summary)"
            transcript.append(f"\n**✅ Done:** {content or '(no summary)'}")
            return "\n".join(transcript)

        # Record the assistant turn WITH its tool_calls so the model sees its own history next round.
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})

        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name")
            try:
                args = _json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            tcid = tc.get("id") or ""

            # The `tools` list only ADVERTISES what is available; nothing stops a model emitting any
            # name it likes. So the read-only contract — the health report (report_mode) and a helper
            # agent both promise they cannot change the machine — has to be ENFORCED here rather than
            # implied by omission. Without this a health run that decided to call write_file simply
            # wrote the file (verified: it did).
            if name not in _allowed_tools:
                messages.append({"role": "tool", "tool_call_id": tcid, "name": name,
                                 "content": f"The tool `{name}` is not available in this run"
                                            + (" — you are a read-only helper and cannot change "
                                               "anything." if (depth or report_mode) else ".")
                                            + " Available tools: " + ", ".join(sorted(_allowed_tools))})
                continue

            if name == "finish":
                summary = (args.get("summary") or "").strip()
                if report_mode:
                    return summary or "(no summary)"
                # `finish` means "I am stopping", NOT "I succeeded" — its own description tells the model
                # to call it when the goal "cannot be" achieved. Emitting ✅ Done unconditionally labelled
                # a run that explicitly gave up ("unable to complete the task due to the private key
                # format mismatch") as "✅ Agent run complete", which is worse than no banner: it reports
                # success for a failure. Trust the model's own verdict, and treat a MISSING success flag
                # as achieved so older/looser models that omit it read as they did before.
                _ok = args.get("success")
                # Verification gate. `finish(success=true)` is the model's own unchecked word, and a
                # weak model will happily report a change it never confirmed took effect. So: if it
                # changed files and has checked NOTHING since, hold the finish once and make it look.
                # Once only (finish_held) — a gate that can fire twice could deadlock a run that
                # genuinely cannot verify, and the model can always finish with success=false instead.
                if (_ok is None or bool(_ok)) and unverified and not finish_held:
                    finish_held = True
                    _files = ", ".join(f"`{p}`" for p in sorted(unverified))
                    await _say(f"↩️ finish held — nothing checked since editing {_files}")
                    messages.append({"role": "tool", "tool_call_id": tcid, "name": name,
                                     "content": f"You changed {_files} but have checked nothing since, so "
                                                "you do not know the change actually worked. Verify it now "
                                                "— read the file back, run the thing, or run a test — then "
                                                "call finish again. If you cannot verify it, call finish "
                                                "with success=false and say what is unconfirmed."})
                    continue
                # A plan the model never finished is evidence against its own success claim, and it is
                # the user who has to act on the difference — so name the leftover steps rather than
                # letting a green banner paper over them. Does not override the model's verdict.
                _left = [s["step"] for s in plan if s["status"] != "done"]
                _unfinished = ("\n\n🗒️ Plan steps not marked done: "
                               + ", ".join(f"`{s}`" for s in _left)) if _left else ""
                if _ok is None or bool(_ok):
                    transcript.append(f"\n**✅ Done:** {summary or '(no summary)'}{_unfinished}")
                else:
                    transcript.append(f"\n**⚠️ Stopped:** {summary or '(no summary)'}{_unfinished}")
                return "\n".join(transcript)

            if name == "run_command":
                cmd = (args.get("command") or "").strip()
                if not cmd:
                    messages.append({"role": "tool", "tool_call_id": tcid, "name": name,
                                     "content": "(no command provided)"})
                    continue
                # Loop breaker: if the model re-issues a command it already ran, don't execute it
                # again (re-running feeds back the same output it's looping on). Nudge it to change
                # approach, and bail if it stays stuck.
                if cmd in cmd_outputs:
                    repeat_nudges += 1
                    await _say(f"↩️ skipping repeat: `{cmd}`")
                    if repeat_nudges >= _MAX_REPEAT_NUDGES:
                        if report_mode:
                            return "⏹️ the agent got stuck repeating the same command(s) without making progress."
                        transcript.append(f"\n**⏹️ Stopped:** stuck repeating the same command(s) "
                                          f"without progress.{_footer()}")
                        return "\n".join(transcript)
                    messages.append({"role": "tool", "tool_call_id": tcid, "name": name,
                                     "content": (f"You already ran `{cmd}` earlier; it produced:\n"
                                                 f"{tail(cmd_outputs[cmd], 1000)}\n"
                                                 "Re-running it will not change anything. Try a DIFFERENT "
                                                 "command, or call finish if you are stuck or done.")})
                    continue
                # Same-ACTION loop breaker. Counted BEFORE running: at _SIG_MAX the model has had several
                # explicit warnings and is still circling, so the remaining steps would just burn GPU on
                # an approach that is not converging.
                _sig = _cmd_signature(cmd)
                sig_counts[_sig] = sig_counts.get(_sig, 0) + 1
                _tries = sig_counts[_sig]
                if _tries >= _SIG_MAX:
                    _msg = (f"the agent kept retrying the same approach (`{_sig}`) {_tries}× "
                            "without converging.")
                    if report_mode:
                        return f"⏹️ {_msg}"
                    transcript.append(f"\n**⏹️ Stopped:** {_msg}{_footer()}")
                    return "\n".join(transcript)
                # Bounded per-step: an unbounded command would deadlock the agent + caller.
                job = await run_to_completion(db, node, target, cmd, user_id=user.id,
                                              timeout=_agent_step_timeout(db))
                out = job.output.strip() or "(no output)"
                # Emit each COMPLETED step (command + output) via `notify`. For a node-agent run the web
                # notify PERSISTS each of these to the relay as its own chat message (like a DM), so leaving
                # mid-run and returning shows the full play-by-play instead of a vanished log. report_mode
                # (health board) stays a brief, non-persisted progress ping; full output is in the job log.
                if report_mode:
                    await _say(f"⚙️ `{cmd}`")
                else:
                    _ex = "" if job.exit_code == 0 else f" ⚠️ exit {job.exit_code}"
                    await _say_output(f"**⚙️ `{cmd}`**{_ex}", out, cmd)
                cmds_run.append(cmd)
                cmd_outputs[cmd] = out
                if job.exit_code == 0:
                    # Running something successfully after an edit IS checking your work (a test, a
                    # restart, a re-grep). A FAILING command proves nothing, so it must not clear this.
                    unverified.clear()
                repeat_nudges = 0   # made progress with a fresh command; reset the stuck counter
                last_job_id = job.id
                # Tell the model explicitly when a command was killed for running too long, so it
                # can adapt (e.g. add a count/limit, background it, or finish) instead of retrying.
                _status = " [killed: timed out]" if job.status == "killed" or "timeout]" in out else ""
                # Circling warning: name the repetition explicitly. A model that has rewritten the same
                # file three times usually cannot see the pattern from the transcript alone, and a vague
                # "try something else" is easy to ignore — so say what is being repeated and how often.
                _warn = ""
                if _tries >= _SIG_WARN:
                    _warn = (f"\n\n[!] You have now attempted `{_sig}` {_tries} times. This approach is "
                             "not working. Do something MATERIALLY different (a different tool, library "
                             "or strategy), or call finish and report what blocked you. "
                             f"This run stops after {_SIG_MAX} attempts at the same thing.")
                messages.append({"role": "tool", "tool_call_id": tcid, "name": name,
                                 "content": f"exit {job.exit_code}{_status}\n"
                                            f"{tail(out, _TOOL_RESULT_CHARS)}{_warn}"})
                continue

            if name == "update_plan":
                _steps = args.get("steps")
                if not isinstance(_steps, list) or not _steps:
                    messages.append({"role": "tool", "tool_call_id": tcid, "name": name,
                                     "content": "update_plan needs a non-empty `steps` list; the plan is "
                                                "unchanged."})
                    continue
                # Normalise: an unknown status becomes pending rather than silently rendering as one,
                # so the model's own view and the user's match exactly.
                plan = [{"step": str(s.get("step") or "").strip(),
                         "status": (s.get("status") if s.get("status") in _PLAN_MARK else "pending")}
                        for s in _steps if isinstance(s, dict) and str(s.get("step") or "").strip()][:20]
                _apply_plan(messages, _sys, plan)
                _done = sum(1 for s in plan if s["status"] == "done")
                if not report_mode:
                    await _say(f"**🗒️ Plan** ({_done}/{len(plan)})\n```\n{_render_plan(plan)}\n```")
                messages.append({"role": "tool", "tool_call_id": tcid, "name": name,
                                 "content": f"Plan recorded ({_done}/{len(plan)} done). It is shown in "
                                            "your system message; keep it current."})
                continue

            if name == "spawn_agent":
                _q = str(args.get("question") or "").strip()
                if not _q:
                    messages.append({"role": "tool", "tool_call_id": tcid, "name": name,
                                     "content": "spawn_agent needs a `question`."})
                    continue
                if spawns_used >= _MAX_SPAWNS:
                    messages.append({"role": "tool", "tool_call_id": tcid, "name": name,
                                     "content": f"You have already used all {_MAX_SPAWNS} helper agents "
                                                "for this run. Investigate this one yourself."})
                    continue
                spawns_used += 1
                await _say(f"**🔎 Helper agent** — {_q}")
                _nfy = None
                if notify:
                    async def _nfy(payload):        # noqa: F811 — None when nobody is watching
                        # Nest the helper's play-by-play under the parent's so the transcript reads as
                        # one run. Only STRINGS get the marker: a progress DICT must pass through
                        # untouched or the web notify persists a stringified dict as junk.
                        await notify(f"↳ {payload}" if isinstance(payload, str) else payload)
                try:
                    _ans = await run_agent(db, user, node, target, _q, chat_service,
                                           notify=_nfy, should_stop=should_stop, depth=depth + 1)
                except Exception as e:
                    logger.warning(f"[node] helper agent failed: {e}")
                    _ans = f"(helper agent failed: {e})"
                _ans = (_ans or "(no answer)").strip()
                cmds_run.append(f"helper: {_q[:60]}")
                await _say_output("**🔎 Helper answered**", _ans, "helper-answer")
                messages.append({"role": "tool", "tool_call_id": tcid, "name": name,
                                 "content": _ans[:_TOOL_RESULT_CHARS]})
                continue

            if name in agent_file_tools.FILE_TOOL_NAMES:
                _path = str(args.get("path") or "").strip()
                _label = agent_file_tools.label_for(name, args)

                # Loop breaker for MUTATING file ops. Rewriting one file over and over is the exact
                # spin _cmd_signature was written for, so it shares that counter; reads and greps are
                # cheap and legitimately repeat (paging a big file), so they are not counted.
                # Counted BEFORE the read-before-edit refusal below, deliberately: a refusal costs an
                # LLM step even though nothing runs, so a model that ignores the instruction and calls
                # edit_file again and again must trip this breaker too. Counting after the refusal
                # left that loop uncounted and it could spin for the whole step budget.
                _warn = ""
                if name not in agent_file_tools.READ_ONLY_TOOLS:
                    _sig = f"{name} {_path}"
                    sig_counts[_sig] = sig_counts.get(_sig, 0) + 1
                    _tries = sig_counts[_sig]
                    if _tries >= _SIG_MAX:
                        _msg = f"the agent kept rewriting `{_path}` ({_tries}×) without converging."
                        if report_mode:
                            return f"⏹️ {_msg}"
                        transcript.append(f"\n**⏹️ Stopped:** {_msg}{_footer()}")
                        return "\n".join(transcript)
                    if _tries >= _SIG_WARN:
                        _warn = (f"\n\n[!] You have now changed `{_path}` {_tries} times. If it is still "
                                 "not right, the approach is wrong — read the file and think again, or "
                                 f"call finish and report what blocked you. This run stops after "
                                 f"{_SIG_MAX} attempts at the same file.")

                # Read before edit. A model editing a file it has not seen this run is working from
                # memory, and old_string will not match — so refuse with the fix rather than let it
                # burn a step failing. write_file is exempt: creating a file needs no prior read.
                if name == "edit_file" and _path and _path not in files_read:
                    await _say(f"↩️ {_label} — refused: not read yet")
                    messages.append({"role": "tool", "tool_call_id": tcid, "name": name,
                                     "content": f"You have not read {_path} in this run, so you cannot "
                                                "know its exact contents. Call read_file on it first, "
                                                "then edit using text copied verbatim from what you "
                                                f"read.{_warn}"})
                    continue

                ok, body = await agent_file_tools.run_file_op(_exec, name, args)
                if ok and name == "read_file" and _path:
                    files_read.add(_path)      # an edit of this path is unlocked from here on
                    unverified.discard(_path)  # reading it back is the cheapest possible check
                if ok and name in ("edit_file", "write_file") and _path:
                    unverified.add(_path)      # changed, and nothing has confirmed it yet
                cmds_run.append(_label)
                if report_mode:
                    await _say(f"{'📄' if name in agent_file_tools.READ_ONLY_TOOLS else '✏️'} `{_label}`")
                else:
                    _ic = "📄" if name in agent_file_tools.READ_ONLY_TOOLS else "✏️"
                    _ex = "" if ok else " ⚠️"
                    await _say_output(f"**{_ic} `{_label}`**{_ex}", body, _path or name)
                messages.append({"role": "tool", "tool_call_id": tcid, "name": name,
                                 "content": (body if ok else f"ERROR: {body}")[:_TOOL_RESULT_CHARS] + _warn})
                continue

            messages.append({"role": "tool", "tool_call_id": tcid, "name": name,
                             "content": f"(unknown tool: {name})"})

    if report_mode:
        return "⏹️ reached the step limit before finishing the health check."
    transcript.append(f"\n**⏹️ Stopped:** reached step limit ({max_steps}).{_footer()}")
    return "\n".join(transcript)


async def run_agent_over_nostr(worker_pubkey: str, text: str, mode: str = "agent",
                               report: bool = False, sandbox_uid: Optional[str] = None,
                               notify: Optional[Callable] = None) -> str:
    """Dispatch an agent/shell task to a Nostr WORKER (by pubkey) and return a summary string —
    the Nostr drop-in for a local run_agent. Shared by _node_command and the health report so a node
    migrated to the npub transport behaves the same everywhere. NIP-44 encrypted end to end (nostr_dvm).
    `sandbox_uid` (sandbox load-balancing): the worker runs the task INSIDE that user's Debian container
    (`pcai-sbx-<uid>`) on its host, so a user's sandbox can live on a placed node, not just the controller.
    `notify`: same callable a LOCAL run_agent takes — the worker's per-step updates are streamed into it,
    so a run placed on another node reports live instead of sitting silent until it finishes."""
    from app.services import nostr_dvm
    params = {"mode": mode, "report": bool(report)}
    params["command" if mode == "shell" else "goal"] = text
    if sandbox_uid:
        params["sandbox_uid"] = str(sandbox_uid)
    out = await nostr_dvm.run_remote("agent", params, worker_pubkey=worker_pubkey, on_progress=notify)
    if not out:
        return "⚠️ no response over Nostr (worker offline, not trusting this controller, or timed out)"
    if out.get("error"):
        return f"⚠️ {out['error']}"
    # Field order is MODE-dependent. For `agent` the worker's `summary` IS the report, so it wins.
    # For `shell` the summary is only the status line (`exit 0`) and the command's text lives in `output`
    # — taking summary first there returned a bare "exit 0" and silently dropped the whole result (the
    # health board and the uptime header of every shell-only worker showed nothing else).
    if mode == "shell":
        body = (out.get("output") or "").strip()
        code = out.get("exit")
        if body:
            return body if code in (0, None) else f"{body}\n[exit {code}]"
    body = (out.get("summary") or out.get("output") or "").strip()
    # The worker's NON-report transcript begins with its own "## Agent on `<node>` — goal: …" header
    # (its local run_agent, report_mode=False, labels the node by ITS OWN name — "sandbox"/"local"…).
    # Every caller here re-wraps the body with an authoritative "## Agent on `<name>`" header using the
    # CONTROLLER's node name, so the worker's copy is redundant: leaving it in rendered TWO identical
    # "Agent on sandbox" sections for a Nostr-placed sandbox run (the reported bug). Strip that one
    # leading header so exactly one survives. A report=True summary carries no header → this is a no-op.
    body = re.sub(r"^## Agent on `[^`]*` — goal:[^\n]*\n+", "", body, count=1)
    return body
