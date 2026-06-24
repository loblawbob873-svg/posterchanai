"""Remote node management: run OS commands on configured nodes over SSH (or 'local'
on this host), as long-running background jobs, plus a small agentic loop that lets the
LLM drive commands toward a natural-language goal.

Config lives in admin Settings:
  node_exec_enabled            "true"/"false"
  node_exec_nodes              one per line: name|user@host  (host 'local'/empty = this host)
  node_exec_users              comma/newline-separated npubs allowed (first user/admin always allowed)
  node_exec_agent_max_steps    max LLM iterations in agentic mode
  node_exec_job_timeout        per-job timeout in seconds (0 = no timeout)

Remote nodes need nothing installed: we just SSH in with key-based BatchMode auth. This
works for any SSH-reachable device (servers, routers, switches) - the target never runs
posterchanai code. The system health report (logs_scheduler) drives run_agent over this
same path, so SSH/local execution lives here only.
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


def get_nodes(db: Session) -> dict[str, str]:
    """Parse node_exec_nodes into {name: target}. target 'local' means run on this host."""
    nodes: dict[str, str] = {}
    raw = _get(db, "node_exec_nodes", "")
    for line in raw.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        name, target = line.split("|", 1)
        name, target = name.strip(), target.strip()
        if not name:
            continue
        nodes[name] = target if target else "local"
    return nodes


def _local_host_ids() -> set:
    """Names/IPs that identify THIS host (for collapsing a node that points back at ourselves)."""
    import socket
    ids = {"localhost", "127.0.0.1", "0.0.0.0", "::1", ""}
    try:
        hostname = socket.gethostname()
        ids.add(hostname.lower())
        ids.add(socket.getfqdn().lower())
        ids.add(socket.gethostbyname(hostname))
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if ip and not ip.startswith("::"):
                ids.add(ip)
    except Exception:
        pass
    return ids


def is_local_target(target: str) -> bool:
    """True if an SSH-style target (``user@host``/``host``/``local``/empty) is this machine.
    Used to dedupe a Remote Node Management entry that points at our own LB IP against ``local``."""
    t = (target or "").strip().lower()
    if not t or t == "local":
        return True
    host = t.split("@", 1)[-1].split(":", 1)[0].strip()
    return host in _local_host_ids()


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


def _ssh_argv(target: str, command: str) -> list[str]:
    # List form (exec, no shell) so the command reaches the remote as a single argument,
    # which keeps awk/grep quoting intact in transit.
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, command]


async def _run(job: Job, timeout: Optional[float]) -> None:
    """Spawn the process, stream merged output into job.output, and finalize status."""
    try:
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
        else:
            proc = await asyncio.create_subprocess_exec(
                *_ssh_argv(job.target, job.command),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
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
                    report_mode: bool = False) -> str:
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

    messages = [
        {"role": "system", "content": _AGENT_SYSTEM.format(node=node)},
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
                # Stream each command as live progress, but DON'T fold the per-step command/
                # output into the returned transcript — that's the persisted/final message, which
                # we keep concise (header + the model's ✅ Done summary). The full play-by-play
                # stays in the live stream and in the node job log (`node log <id>`).
                await _say(f"⚙️ `{cmd}`")
                # Bounded per-step: an unbounded command would deadlock the agent + caller.
                job = await run_to_completion(db, node, target, cmd, user_id=user.id,
                                              timeout=_agent_step_timeout(db))
                out = job.output.strip() or "(no output)"
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
