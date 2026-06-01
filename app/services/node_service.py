"""Remote node management: run OS commands on configured nodes over SSH (or 'local'
on this host), as long-running background jobs, plus a small agentic loop that lets the
LLM drive commands toward a natural-language goal.

Config lives in admin Settings:
  node_exec_enabled            "true"/"false"
  node_exec_nodes              one per line: name|user@host  (host 'local'/empty = this host)
  node_exec_users              comma-separated usernames allowed (admins always allowed)
  node_exec_agent_max_steps    max LLM iterations in agentic mode
  node_exec_job_timeout        per-job timeout in seconds (0 = no timeout)

Remote nodes need nothing installed: we just SSH in with key-based BatchMode auth, the
same mechanism used by logs_scheduler.run_ssh_command. This works for any SSH-reachable
device (servers, routers, switches) - the target never runs posterchanai code.
"""
import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models import Setting

if TYPE_CHECKING:
    from app.models import User
    from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)

# Cap stored output per job so a chatty command can't exhaust memory.
_MAX_OUTPUT = 64 * 1024  # 64 KB

# In-memory job registry, shared across the process (guarded by _lock).
_lock = threading.Lock()
_jobs: dict[int, "Job"] = {}
_next_id = 1
_MAX_JOBS = 200  # keep the most recent finished jobs; prune older ones


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get(db: Session, key: str, default: str = "") -> str:
    s = db.query(Setting).filter(Setting.key == key).first()
    return s.value if s and s.value else default


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


def user_allowed(db: Session, user: Optional["User"]) -> bool:
    """Feature must be enabled AND the user must be an admin or in the allowlist."""
    if user is None or not is_enabled(db):
        return False
    if getattr(user, "is_admin", False) or user.id == 1:
        return True
    allowed = {u.strip().lower() for u in _get(db, "node_exec_users", "").split(",") if u.strip()}
    return (user.username or "").lower() in allowed


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
    # List form (exec, no shell) so the command reaches the remote as a single argument;
    # mirrors logs_scheduler.run_ssh_command so awk/grep quoting survives transit.
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, command]


async def _run(job: Job, timeout: Optional[float]) -> None:
    """Spawn the process, stream merged output into job.output, and finalize status."""
    try:
        if job.target == "local":
            proc = await asyncio.create_subprocess_shell(
                job.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *_ssh_argv(job.target, job.command),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
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
                proc.kill()
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


def _terminate(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.terminate()
    except ProcessLookupError:
        pass
    except Exception as e:
        logger.debug(f"[node] terminate failed: {e}")


def start_job(db: Session, node: str, target: str, command: str,
              user_id: Optional[int] = None,
              on_complete: Optional[Callable[["Job"], Awaitable[None]]] = None) -> Job:
    """Create a job and launch it as a background task. Returns immediately."""
    global _next_id
    with _lock:
        job = Job(id=_next_id, node=node, target=target, command=command, user_id=user_id)
        job._on_complete = on_complete
        _jobs[job.id] = job
        _next_id += 1
        # Prune oldest finished jobs so the registry can't grow without bound.
        if len(_jobs) > _MAX_JOBS:
            for jid in sorted(j.id for j in _jobs.values() if j.done)[: len(_jobs) - _MAX_JOBS]:
                del _jobs[jid]
    job._task = asyncio.create_task(_run(job, _job_timeout(db)))
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
                            user_id: Optional[int] = None) -> Job:
    """Start a job and wait for it to fully finish (respecting the configured timeout)."""
    job = start_job(db, node, target, command, user_id=user_id)
    if job._task is not None:
        try:
            await asyncio.shield(job._task)
        except asyncio.CancelledError:
            pass
    return job


def get_job(job_id: int) -> Optional[Job]:
    return _jobs.get(job_id)


def list_jobs(user_id: Optional[int] = None, limit: int = 20) -> list[Job]:
    with _lock:
        jobs = list(_jobs.values())
    if user_id is not None:
        jobs = [j for j in jobs if j.user_id == user_id]
    jobs.sort(key=lambda j: j.id, reverse=True)
    return jobs[:limit]


def kill_job(job_id: int) -> bool:
    job = _jobs.get(job_id)
    if not job or job.done:
        return False
    job.status = "killed"
    if job._proc:
        _terminate(job._proc)
    if job._task:
        job._task.cancel()
    return True


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------

_AGENT_SYSTEM = """You are a systems administrator managing the host "{node}" by running shell commands.

To run a command, reply with EXACTLY one line and nothing else:
CMD: <single shell command>

The command's output will be sent back to you so you can decide the next step.
When the goal is achieved (or cannot be), reply with:
DONE: <short summary of what you found or did>

Rules:
- One command per turn. No explanations around the CMD line.
- Prefer read-only/diagnostic commands first. Be concise.
- Never wait for interactive input; commands run non-interactively."""


def _parse_agent_reply(text: str) -> tuple[str, str]:
    """Return ('cmd'|'done'|'none', payload)."""
    for line in text.splitlines():
        s = line.strip()
        if s.upper().startswith("CMD:"):
            return "cmd", s[4:].strip()
        if s.upper().startswith("DONE:"):
            return "done", s[5:].strip()
    return "none", text.strip()


async def run_agent(db: Session, user: "User", node: str, target: str, goal: str,
                    chat_service: "ChatService") -> str:
    """Let the LLM drive commands on `node` toward `goal`. Returns a markdown transcript."""
    max_steps = _max_steps(db)
    messages = [
        {"role": "system", "content": _AGENT_SYSTEM.format(node=node)},
        {"role": "user", "content": f"Goal: {goal}"},
    ]
    transcript = [f"## Agent on `{node}` — goal: {goal}\n"]

    for step in range(1, max_steps + 1):
        reply = await chat_service.chat(messages)
        kind, payload = _parse_agent_reply(reply)

        if kind == "done":
            transcript.append(f"\n**✅ Done:** {payload or '(no summary)'}")
            return "\n".join(transcript)
        if kind != "cmd" or not payload:
            transcript.append(f"\n**⚠️ Stopped:** model did not issue a command.\n\n{reply.strip()}")
            return "\n".join(transcript)

        transcript.append(f"\n**Step {step}** — `{payload}`")
        messages.append({"role": "assistant", "content": f"CMD: {payload}"})

        job = await run_to_completion(db, node, target, payload, user_id=user.id)
        out = job.output.strip() or "(no output)"
        transcript.append(f"```\n{out[:1500]}\n```")
        # Feed the (truncated) output back to the model for the next decision.
        messages.append({"role": "user", "content": f"Output (exit {job.exit_code}):\n{out[:4000]}"})

    transcript.append(f"\n**⏹️ Stopped:** reached step limit ({max_steps}).")
    return "\n".join(transcript)
