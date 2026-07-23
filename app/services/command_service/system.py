"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
import asyncio
from ._common import Callable, Optional, logger


# Background node-agent runs. run_agent() is a long multi-step loop; awaiting it inline pins it to the
# chat request, so closing the app would cancel it and lose the result. Instead we run it as a DETACHED
# task with its OWN DB session and deliver the final summary through the same `notify` channel a finished
# job uses (persisted to the conversation) — so the user can close the chat and the result lands when it's
# done. We hold a reference to each task so it isn't garbage-collected mid-run.
_AGENT_BG_TASKS: set = set()


async def _agent_bg(targets, goal, uid, chat_service, notify):
    """Run the agent over one-or-more (name, target) nodes on a FRESH session, then deliver the combined
    summary via notify({'type':'agent_result', ...}) — chat.py persists + pushes it (queued if offline).
    Each target is "local" (LLM loop runs HERE) or "nostr:<pkhex>" (the worker runs its own loop and
    returns an encrypted summary) — SSH is gone."""
    from app.database import SessionLocal
    from app.models import User as _User
    from app.services import node_service
    db = SessionLocal()
    sections = []
    multi = len(targets) > 1
    try:
        u = db.query(_User).filter(_User.id == uid).first() if uid else None
        for name, target in targets:
            # Prefix live step-progress with the node name only when fanning out (matches the old `all` path).
            nfy = (lambda txt, _p=name: notify(f"[{_p}] {txt}")) if (notify and multi) else notify
            try:
                if target.startswith("nostr:"):
                    if nfy:
                        await nfy(f"🛰️ dispatching to `{name}` over Nostr…")
                    body = await node_service.run_agent_over_nostr(target[len("nostr:"):], goal, mode="agent")
                    sections.append(f"## Agent on `{name}` — goal: {goal}\n\n{body}")
                else:
                    sections.append(await node_service.run_agent(db, u, name, target, goal, chat_service, notify=nfy))
            except Exception as e:
                logger.error(f"[node] background agent error on {name}: {e}", exc_info=True)
                sections.append(f"## Agent on `{name}` — goal: {goal}\n\n**⚠️ Error:** {e}")
            finally:
                # "Delete the container when the job's done": an agent run in a sandbox tears its
                # container down afterward (the idle reaper is only a backstop for bare `node sandbox` use).
                if target.startswith("sandbox:"):
                    try:
                        from app.services import sandbox_service
                        await sandbox_service.reap(target.split(":", 1)[1])
                    except Exception:
                        pass
    finally:
        # A multi-minute agent run holds this session idle → Postgres closes the connection, and then
        # db.close() itself raises OperationalError. Left unguarded that propagates from the `finally`
        # and SKIPS the delivery below, so the whole run's output is lost. Swallow it — the delivery
        # (node_notify → chat_history.append) opens its OWN fresh session, so it's unaffected.
        try:
            db.close()
        except Exception:
            pass
    if notify:
        try:
            await notify({"type": "agent_result", "content": "\n\n---\n\n".join(sections)})
        except Exception as e:
            logger.warning(f"[node] background agent deliver failed: {e}")


_AGENT_BG_BY_CONV: dict = {}   # conversation_id -> task, so deleting the chat cancels the run


def _spawn_agent_bg(targets, goal, uid, chat_service, notify):
    t = asyncio.create_task(_agent_bg(targets, goal, uid, chat_service, notify))
    _AGENT_BG_TASKS.add(t)
    # The chat.py notify closure carries the launch conversation id (set as an attribute), so a delete
    # of that chat can find and cancel THIS run instead of leaving it churning + resurrecting the chat.
    _conv = getattr(notify, "conv_id", None)
    if _conv is not None:
        _AGENT_BG_BY_CONV[_conv] = t

    def _done(_t):
        _AGENT_BG_TASKS.discard(_t)
        if _conv is not None and _AGENT_BG_BY_CONV.get(_conv) is _t:
            _AGENT_BG_BY_CONV.pop(_conv, None)
    t.add_done_callback(_done)


def cancel_agent_for_conv(conv_id) -> bool:
    """Cancel a background agent tied to this conversation (called when the chat is deleted). The task's
    per-target `finally` still reaps any sandbox container; the cancel skips result delivery, so a
    deliberately-deleted chat is NOT resurrected. Returns True if a live run was cancelled."""
    t = _AGENT_BG_BY_CONV.pop(conv_id, None)
    if t is not None and not t.done():
        t.cancel()
        return True
    return False


class _SystemMixin:
    async def _logs_command(self, arg: str, notify: Optional[Callable] = None) -> dict:
        """Run the agentic system health report and store it in the Logs chat (admin only).

        Delegates entirely to logs_scheduler.run_logs_for_admin (shared by the scheduler), which
        drives node_service.run_agent across the configured nodes. `notify`, when given, streams
        the per-command play-by-play to the originating channel (web UI / Telegram)."""
        if not self.user:
            return {"type": "text", "content": "Please log in to use the logs command."}

        # Admin only (user ID 1)
        if self.user.id != 1:
            return {"type": "text", "content": "The logs command is only available to administrators."}

        try:
            from app.services.logs_scheduler import run_logs_for_admin
            text = await run_logs_for_admin(return_text=True, notify=notify, deliver_telegram=False)
            return {"type": "text", "content": text or "No report generated."}
        except Exception as e:
            logger.error(f"Logs command error: {e}")
            return {"type": "text", "content": f"Error generating health report: {str(e)}"}

    async def _node_command(self, arg: str, notify: Optional[Callable] = None) -> dict:
        """Run OS commands on nodes (Nostr workers, or `local`) as background jobs, with an optional
        agentic mode. Full (host + remote) access is gated by the admin allowlist; NON-admin AI users
        get a per-user Debian **sandbox** instead (admins can opt into it too).

        `notify`, when given, is an async callback the caller supplies to deliver a
        finished job's output back to the channel the command came from (web UI
        conversation or Telegram chat). It must not rely on the request's DB session,
        which is closed by the time a long-running job finishes."""
        from app.services import node_service

        _full = node_service.user_allowed(self.db, self.user)         # admin/allowlisted → host + remote nodes
        _sbx = node_service.sandbox_allowed(self.db, self.user)       # AI user + sandbox on → a Debian container
        if not _full and not _sbx:
            return {"type": "text", "content": "⛔ Agentic node management is disabled or you are not authorized. An admin can enable it in Admin → Services → Agentic Node Management."}

        parts = arg.strip().split(maxsplit=2)
        sub = parts[0].lower() if parts else ""
        from app.services import nostr_dvm
        # The per-user node registry. `_reg`: name -> "local" (run on THIS host directly) /
        # "nostr:<pkhex>" (a worker addressed over an encrypted NIP-90 event — the worker runs it
        # locally and returns the result; see docs/NODE_AGENT_NOSTR.md) / "sandbox:<uid>" (a per-user
        # Debian container via docker exec). Full-access users see every node (+ the sandbox if it's on);
        # a sandbox-only user sees ONLY their container. Split into the shapes the rest expects: `nodes`
        # (local-or-sandbox, run via the local job machinery) and `_npub_nodes` (name -> worker pubkey).
        _sbx_target = f"sandbox:{self.user.id}" if self.user else "sandbox:anon"
        if _full:
            _reg = node_service.all_nodes(self.db)
            if _sbx:                                # sandbox enabled → offer it as a node to admins too
                _reg["sandbox"] = _sbx_target
        else:
            _reg = {"sandbox": _sbx_target}         # sandbox-only user: their container is the only target
        nodes = {n: t for n, t in _reg.items() if not t.startswith("nostr:")}
        _npub_nodes = {n: t[len("nostr:"):] for n, t in _reg.items() if t.startswith("nostr:")}

        def _fmt_nodes() -> str:
            if not nodes and not _npub_nodes:
                return "No nodes configured. Add them in Admin → Services → Agentic Node Management (one per line: `name|user@host`, or a Nostr worker `name npub1…`)."
            lines = ["**Configured nodes:**"]
            for _nn, _pk in _npub_nodes.items():
                lines.append(f"- `{_nn}` → 🛰️ {nostr_dvm.nostr_service.npub_of(_pk)[:18]}…")
            for name, target in nodes.items():
                where = ("this host" if target == "local"
                         else "🐳 your Debian sandbox" if target.startswith("sandbox:") else target)
                lines.append(f"- `{name}` → {where}")
            return "\n".join(lines)

        def _result_for(job, header: str) -> dict:
            """Render a finished job. Short output goes inline; long output shows a tail
            preview inline and attaches the full output as a .txt (delivered as a Telegram
            document or a web-UI download link by the existing `type=='files'` handlers)."""
            out = (job.output or "(no output)").strip()
            preview = f"{header}\n\n```\n{node_service.tail(out, node_service.INLINE_LIMIT)}\n```"
            if len(out) > node_service.INLINE_LIMIT:
                return {
                    "type": "files",
                    "content": preview,
                    "files": [{"filename": f"node-{job.node}-job{job.id}.txt", "data": out.encode("utf-8", "replace")}],
                }
            return {"type": "text", "content": preview}

        async def _dispatch_nostr(name: str, worker_pk: str, mode: str, text: str, dangerous: bool = False) -> dict:
            """Send a node/agent command to an npub-addressed worker over Nostr and render its result.
            No SSH — the worker runs it locally and returns an encrypted result (see docs/NODE_AGENT_NOSTR.md)."""
            if notify:
                await notify(f"🛰️ dispatching to `{name}` over Nostr…")
            params = {"mode": mode, "dangerous": bool(dangerous)}
            params["command" if mode == "shell" else "goal"] = text
            out = await nostr_dvm.run_remote("agent", params, worker_pubkey=worker_pk)
            if not out:
                return {"type": "text", "content": f"⚠️ No response from `{name}` (offline, not trusting this controller, or timed out)."}
            if out.get("error"):
                return {"type": "text", "content": f"⚠️ `{name}`: {out['error']}"}
            body = (out.get("output") or out.get("summary") or "").strip() or "(no output)"
            header = f"🛰️ `{name}` ({mode}) — {out.get('status', '?')}" + (f" exit {out.get('exit')}" if out.get("exit") is not None else "")
            preview = f"{header}\n\n```\n{node_service.tail(body, node_service.INLINE_LIMIT)}\n```"
            if len(body) > node_service.INLINE_LIMIT:
                return {"type": "files", "content": preview,
                        "files": [{"filename": f"node-{name}-nostr.txt", "data": body.encode("utf-8", "replace")}]}
            return {"type": "text", "content": preview}

        # --- management subcommands ---
        if sub in ("", "list", "ls", "help"):
            usage = (
                "**Agentic node management**\n\n"
                "- `node <name> <command>` — run a command (long ones run in the background)\n"
                "- `node all <command>` — run a command on every node\n"
                "- `node agent <name> <goal>` — let the AI run commands toward a goal\n"
                "- `node jobs` — list your recent jobs\n"
                "- `node log <id>` — show a job's output\n"
                "- `node kill <id>` — stop a running job\n\n"
                f"{_fmt_nodes()}"
            )
            return {"type": "text", "content": usage}

        if sub == "jobs":
            jobs = node_service.list_jobs(user_id=self.user.id if self.user else None)
            if not jobs:
                return {"type": "text", "content": "No jobs yet."}
            icon = {"running": "⏳", "done": "✅", "failed": "❌", "killed": "🛑"}
            lines = ["**Your node jobs:**"]
            for j in jobs:
                lines.append(f"- {icon.get(j.status, '•')} #{j.id} `{j.node}`: `{j.command[:60]}` — {j.status}")
            lines.append("\nUse `node log <id>` for output.")
            return {"type": "text", "content": "\n".join(lines)}

        if sub == "log":
            if len(parts) < 2 or not parts[1].isdigit():
                return {"type": "text", "content": "Usage: `node log <id>`"}
            job = node_service.get_job(int(parts[1]), user_id=self.user.id if self.user else None)
            if not job:
                return {"type": "text", "content": f"Job #{parts[1]} not found."}
            return _result_for(job, f"**Job #{job.id}** `{job.node}` — {job.status} (exit {job.exit_code})\n`{job.command}`")

        if sub == "kill":
            if len(parts) < 2 or not parts[1].isdigit():
                return {"type": "text", "content": "Usage: `node kill <id>`"}
            _uid = self.user.id if self.user else None
            job = node_service.get_job(int(parts[1]), user_id=_uid)
            if not job:
                return {"type": "text", "content": f"Job #{parts[1]} not found."}
            ok = node_service.kill_job(int(parts[1]), user_id=_uid)
            return {"type": "text", "content": f"{'🛑 Killed' if ok else 'Could not kill (already finished?)'} job #{parts[1]}."}

        # --- fan-out: run the same command on every node (local jobs + Nostr workers) ---
        if sub == "all":
            import asyncio
            command = arg.strip()[len(parts[0]):].strip()
            if not command:
                return {"type": "text", "content": "Usage: `node all <command>`"}
            if not _reg:
                return {"type": "text", "content": _fmt_nodes()}
            icon = {"done": "✅", "failed": "❌", "killed": "🛑"}
            lines = [f"## `{command}` on {len(_reg)} node(s)"]
            # Local nodes → background jobs (await briefly); Nostr workers → encrypted dispatch (await result).
            jobs = {name: node_service.start_job(self.db, name, nodes[name], command,
                                                 user_id=self.user.id if self.user else None)
                    for name in nodes}
            _nostr_results = await asyncio.gather(*(
                _dispatch_nostr(name, pk, "shell", command) for name, pk in _npub_nodes.items()
            )) if _npub_nodes else []
            await asyncio.gather(*(node_service.await_job(j, wait=10.0) for j in jobs.values()))
            for name, j in jobs.items():
                if j.done:
                    out = (j.output or "(no output)").strip()
                    lines.append(f"\n**{icon.get(j.status, 'ℹ️')} {name}** (exit {j.exit_code})\n```\n{node_service.tail(out, 1200)}\n```")
                else:
                    node_service.notify_on_done(j, notify)   # still running — deliver when it finishes
                    lines.append(f"\n**⏳ {name}** — still running (job #{j.id}, `node log {j.id}`)")
            for name, res in zip(_npub_nodes.keys(), _nostr_results):
                lines.append(f"\n**🛰️ {name}**\n{(res.get('content') or '').strip()}")
            return {"type": "text", "content": "\n".join(lines)}

        # --- agentic mode ---
        if sub == "agent":
            if len(parts) < 3:
                return {"type": "text", "content": "Usage: `node agent <name> <goal>` (or `node agent all <goal>`)"}
            name, goal = parts[1], parts[2]

            # Resolve target(s) from the Nostr-only registry. A "nostr:<pk>" target runs the agent loop
            # ON the worker; "local" runs it here. `all` fans out over every node.
            if name == "all":
                targets = list(_reg.items())
                if not targets:
                    return {"type": "text", "content": _fmt_nodes()}
            elif name in _reg:
                targets = [(name, _reg[name])]
            else:
                return {"type": "text", "content": f"Unknown node `{name}`.\n\n{_fmt_nodes()}"}

            # With a live channel (web UI), run the agent in the BACKGROUND and post the result back when it's
            # done — so the user can close the chat instead of babysitting a multi-minute run (this holds for
            # Nostr workers too, so a remote agent no longer blocks the socket). Without a channel, run inline.
            if notify:
                _spawn_agent_bg(targets, goal, self.user.id if self.user else None, self.chat_service, notify)
                where = "all nodes" if name == "all" else f"`{name}`"
                return {"type": "text", "content": f"🤖 Agent started on {where} — working on: {goal}\n\nI'll post the result here when it's done. You can close this and come back to it."}

            sections = []
            for _n, _t in targets:
                try:
                    if _t.startswith("nostr:"):
                        body = await node_service.run_agent_over_nostr(_t[len("nostr:"):], goal, mode="agent")
                        sections.append(f"## Agent on `{_n}` — goal: {goal}\n\n{body}")
                    else:
                        sections.append(await node_service.run_agent(self.db, self.user, _n, _t, goal, self.chat_service, notify=None))
                except Exception as e:
                    logger.error(f"[node] agent error on {_n}: {e}", exc_info=True)
                    sections.append(f"## Agent on `{_n}` — goal: {goal}\n\n**⚠️ Error:** {e}")
                finally:
                    if _t.startswith("sandbox:"):   # tear the container down when the run finishes
                        try:
                            from app.services import sandbox_service
                            await sandbox_service.reap(_t.split(":", 1)[1])
                        except Exception:
                            pass
            return {"type": "text", "content": "\n\n---\n\n".join(sections)}

        # --- direct command: node <name> <command...> ---
        name = sub
        command = arg.strip()[len(parts[0]):].strip()
        # Nostr-addressed worker → run the shell command over Nostr (the worker runs it locally).
        if name in _npub_nodes:
            if not command:
                return {"type": "text", "content": f"Usage: `node {name} <command>`"}
            return await _dispatch_nostr(name, _npub_nodes[name], "shell", command)
        if name not in nodes:
            return {"type": "text", "content": f"Unknown node `{name}`.\n\n{_fmt_nodes()}"}
        # Everything after the node name is the command (preserve original spacing/casing).
        if not command:
            return {"type": "text", "content": f"Usage: `node {name} <command>`"}

        job = node_service.start_job(
            self.db, name, nodes[name], command,
            user_id=self.user.id if self.user else None,
        )
        await node_service.await_job(job, wait=8.0)
        if job.done:
            icon = {"done": "✅", "failed": "❌", "killed": "🛑"}.get(job.status, "ℹ️")
            return _result_for(job, f"{icon} `{name}` exit {job.exit_code}")
        # Still running — deliver its output to this channel when it finishes.
        node_service.notify_on_done(job, notify)
        return {"type": "text", "content": f"⏳ Started job #{job.id} on `{name}` (still running).\nI'll post the output here when it's done — or check with `node log {job.id}` / stop with `node kill {job.id}`."}
