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
import logging
import os
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
    `node_exec_node_npubs`; a synthetic ``local`` is always present, and any mapped npub equal to THIS
    host's own worker key collapses to ``local`` (run here — no encrypted round-trip to self). This is
    the single builder shared by the `node` command, `/api/node/state`, and the health report, so the
    node list can never drift or double-count a host."""
    from app.services import nostr_dvm
    out: dict[str, str] = {"local": "local"}
    me = (nostr_dvm.node_pubkey() or "").lower()
    for name, pk in nostr_dvm.agent_node_map().items():
        out[name] = "local" if (pk and pk.lower() == me) else f"nostr:{pk}"
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
    try:
        return max(1, int(_get(db, "node_exec_agent_max_steps", "8")))
    except ValueError:
        return 8


# How many times in a row the model may re-issue a command it has already run before we give up.
# Small models degenerate into re-running the same failing command; re-executing wastes steps and
# feeds back the same output it's already looping on, so we nudge instead and bail if it persists.
_MAX_REPEAT_NUDGES = 3


# Sentinel: start_job/run_to_completion fall back to the global job timeout when no override given.
_USE_JOB_TIMEOUT = object()


def _agent_step_timeout(db: Session) -> Optional[float]:
    """Per-command bound for the AGENT loop. Unlike fire-and-forget jobs (which return after ~8s
    and notify on completion), the agent AWAITS each command, so an unbounded command would
    deadlock the whole loop and the caller. Default 600s; 0 -> fall back to the global job timeout
    (which may itself be unbounded - an explicit admin choice). Truly long fire-and-forget tasks
    should use the non-agentic `node <name> <cmd>` instead."""
    try:
        secs = float(_get(db, "node_exec_agent_step_timeout", "600"))
    except ValueError:
        secs = 600.0
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

Accomplish the user's goal by calling the run_command tool to execute non-interactive shell
commands; each command's output (and exit code) is returned to you so you can decide the next step.

Rules:
- Prefer read-only / diagnostic commands first; make changes only when the goal requires it.
- Run one logical command per step and wait for its output before the next.
- Never run interactive commands that wait for input (use flags like -y, --noninteractive).
- When the goal is achieved, or cannot be, call the finish tool with a short summary."""

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
                    "summary": {"type": "string", "description": "Short summary of the outcome."}
                },
                "required": ["summary"],
            },
        },
    },
]


async def run_agent(db: Session, user: "User", node: str, target: str, goal: str,
                    chat_service: "ChatService", notify: Optional[Callable] = None,
                    report_mode: bool = False, should_stop: Optional[Callable] = None) -> str:
    """Drive commands on `node` toward `goal` via native tool-calling. Returns a concise summary
    (header + the model's ✅ Done line); the per-command play-by-play is streamed live via `notify`
    and recorded in the node job log, not folded into the returned/persisted message.

    `report_mode` returns ONLY the model's final summary (no `## Agent on…` header, no `**✅ Done:**`
    prefix, no commands footer) so the caller can compose it into its own document — used by the
    agentic system-health report (logs_scheduler), where the model's finish summary IS the report.

    Uses the same tool-calling backend as opencode (chat_completion with tools -> generate_message),
    so the model emits structured run_command/finish calls instead of a fragile CMD:/DONE: text
    protocol. Defaults to the agentic-tuned Claude-Code model; falls back to the configured default
    when that gguf isn't present (resolve_model_path handles the fallback). `notify`, when given,
    streams each step to the originating channel (Telegram/web)."""
    import json as _json
    from app.services.inference_factory import get_inference_service

    max_steps = _max_steps(db)
    # Agentic model for the tool-call loop. Prefer the unified `llm_tools_model` (shared with the
    # /v1 agentic path); fall back to the legacy `node_exec_agent_model` for back-compat, then the
    # tuned default. Empty/missing gguf -> backend falls back to the default model (resolve_model_path).
    model = (_get(db, "llm_tools_model", "").strip()
             or _get(db, "node_exec_agent_model", "Qwen3.5-9B-Claude-Code-Q4_K_M.gguf").strip()) or None
    service = get_inference_service(db)

    _sys = _AGENT_SYSTEM.format(node=node)
    if str(target).startswith("sandbox:"):
        # The agent is inside a fresh disposable Debian container (root). Steer it past the two things
        # that trip up package tasks here: Debian's PEP-668 externally-managed pip, and guessed package names.
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
    transcript = [] if report_mode else [f"## Agent on `{node}` — goal: {goal}\n"]
    cmds_run: list[str] = []   # for a footer on the stop/error paths so they're never blank
    cmd_outputs: dict[str, str] = {}   # command -> its last output, to detect/short-circuit repeats
    repeat_nudges = 0          # consecutive "you already ran this" nudges (reset by a fresh command)
    last_job_id = None

    async def _say(text: str):
        if notify:
            try:
                await notify(text)
            except Exception:
                pass

    def _footer() -> str:
        # The happy path returns the model's summary; on stop/error the concise transcript would
        # otherwise be empty, so point the user at what actually ran and where to find the output.
        if not cmds_run:
            return ""
        cmds = ", ".join(f"`{c}`" for c in cmds_run)
        tail_ref = f" Full output: `node log {last_job_id}`." if last_job_id is not None else ""
        return f"\nRan {len(cmds_run)} command(s): {cmds}.{tail_ref}"

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
        try:
            result = await service.chat_completion(
                messages=messages, model=model,
                tools=_NODE_TOOLS, tool_choice="auto", temperature=0.2,
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

            if name == "finish":
                summary = (args.get("summary") or "").strip()
                if report_mode:
                    return summary or "(no summary)"
                transcript.append(f"\n**✅ Done:** {summary or '(no summary)'}")
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
                    await _say(f"**⚙️ `{cmd}`**{_ex}\n```\n{tail(out, 700)}\n```")
                cmds_run.append(cmd)
                cmd_outputs[cmd] = out
                repeat_nudges = 0   # made progress with a fresh command; reset the stuck counter
                last_job_id = job.id
                # Tell the model explicitly when a command was killed for running too long, so it
                # can adapt (e.g. add a count/limit, background it, or finish) instead of retrying.
                _status = " [killed: timed out]" if job.status == "killed" or "timeout]" in out else ""
                messages.append({"role": "tool", "tool_call_id": tcid, "name": name,
                                 "content": f"exit {job.exit_code}{_status}\n{tail(out, 4000)}"})
            else:
                messages.append({"role": "tool", "tool_call_id": tcid, "name": name,
                                 "content": f"(unknown tool: {name})"})

    if report_mode:
        return "⏹️ reached the step limit before finishing the health check."
    transcript.append(f"\n**⏹️ Stopped:** reached step limit ({max_steps}).{_footer()}")
    return "\n".join(transcript)


async def run_agent_over_nostr(worker_pubkey: str, text: str, mode: str = "agent",
                               report: bool = False, dangerous: bool = False,
                               sandbox_uid: Optional[str] = None) -> str:
    """Dispatch an agent/shell/claude task to a Nostr WORKER (by pubkey) and return a summary string —
    the Nostr drop-in for a local run_agent. Shared by _node_command and the health report so a node
    migrated to the npub transport behaves the same everywhere. NIP-44 encrypted end to end (nostr_dvm).
    `sandbox_uid` (sandbox load-balancing): the worker runs the task INSIDE that user's Debian container
    (`pcai-sbx-<uid>`) on its host, so a user's sandbox can live on a placed node, not just the controller."""
    from app.services import nostr_dvm
    params = {"mode": mode, "dangerous": bool(dangerous), "report": bool(report)}
    params["command" if mode == "shell" else "goal"] = text
    if sandbox_uid:
        params["sandbox_uid"] = str(sandbox_uid)
    out = await nostr_dvm.run_remote("agent", params, worker_pubkey=worker_pubkey)
    if not out:
        return "⚠️ no response over Nostr (worker offline, not trusting this controller, or timed out)"
    if out.get("error"):
        return f"⚠️ {out['error']}"
    return (out.get("summary") or out.get("output") or "").strip()
