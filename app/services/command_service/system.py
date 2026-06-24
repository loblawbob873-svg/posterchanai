"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
from ._common import Callable, Optional, logger


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
        """Run OS commands on configured nodes (SSH or local) as background jobs, with an
        optional agentic mode. Gated by admin Settings (enabled + user allowlist).

        `notify`, when given, is an async callback the caller supplies to deliver a
        finished job's output back to the channel the command came from (web UI
        conversation or Telegram chat). It must not rely on the request's DB session,
        which is closed by the time a long-running job finishes."""
        from app.services import node_service

        if not node_service.user_allowed(self.db, self.user):
            return {"type": "text", "content": "⛔ Agentic node management is disabled or you are not authorized. An admin can enable it in Admin → Services → Agentic Node Management."}

        parts = arg.strip().split(maxsplit=2)
        sub = parts[0].lower() if parts else ""
        nodes = node_service.get_nodes(self.db)

        def _fmt_nodes() -> str:
            if not nodes:
                return "No nodes configured. Add them in Admin → Services → Agentic Node Management (one per line: `name|user@host`)."
            lines = ["**Configured nodes:**"]
            for name, target in nodes.items():
                where = "this host" if target == "local" else target
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

        # --- fan-out: run the same command on every node ---
        if sub == "all":
            import asyncio
            command = arg.strip()[len(parts[0]):].strip()
            if not command:
                return {"type": "text", "content": "Usage: `node all <command>`"}
            if not nodes:
                return {"type": "text", "content": _fmt_nodes()}
            jobs = {
                name: node_service.start_job(
                    self.db, name, target, command,
                    user_id=self.user.id if self.user else None,
                )
                for name, target in nodes.items()
            }
            await asyncio.gather(*(node_service.await_job(j, wait=10.0) for j in jobs.values()))
            icon = {"done": "✅", "failed": "❌", "killed": "🛑"}
            lines = [f"## `{command}` on {len(jobs)} node(s)"]
            for name, j in jobs.items():
                if j.done:
                    out = (j.output or "(no output)").strip()
                    lines.append(f"\n**{icon.get(j.status, 'ℹ️')} {name}** (exit {j.exit_code})\n```\n{node_service.tail(out, 1200)}\n```")
                else:
                    # Still running — deliver its output to this channel when it finishes.
                    node_service.notify_on_done(j, notify)
                    lines.append(f"\n**⏳ {name}** — still running (job #{j.id}, `node log {j.id}`)")
            return {"type": "text", "content": "\n".join(lines)}

        # --- agentic mode ---
        if sub == "agent":
            if len(parts) < 3:
                return {"type": "text", "content": "Usage: `node agent <name> <goal>` (or `node agent all <goal>`)"}
            name, goal = parts[1], parts[2]

            # `node agent all <goal>` — run the agent on every node toward the same goal.
            # Sequential (not parallel): they share one DB session, and LLM inference serializes
            # on the GPU lock anyway, so parallelism wouldn't help and risks interleaved state.
            if name == "all":
                sections = []
                for _n, _t in nodes.items():
                    _nfy = (lambda txt, _p=_n: notify(f"[{_p}] {txt}")) if notify else None
                    try:
                        sections.append(await node_service.run_agent(
                            self.db, self.user, _n, _t, goal, self.chat_service, notify=_nfy))
                    except Exception as e:
                        logger.error(f"[node] agent error on {_n}: {e}", exc_info=True)
                        sections.append(f"## Agent on `{_n}` — goal: {goal}\n\n**⚠️ Error:** {e}")
                return {"type": "text", "content": "\n\n---\n\n".join(sections)}

            if name not in nodes:
                return {"type": "text", "content": f"Unknown node `{name}`.\n\n{_fmt_nodes()}"}
            try:
                summary = await node_service.run_agent(self.db, self.user, name, nodes[name], goal, self.chat_service, notify=notify)
                return {"type": "text", "content": summary}
            except Exception as e:
                logger.error(f"[node] agent error: {e}", exc_info=True)
                return {"type": "text", "content": f"Agent error: {e}"}

        # --- direct command: node <name> <command...> ---
        name = sub
        if name not in nodes:
            return {"type": "text", "content": f"Unknown node `{name}`.\n\n{_fmt_nodes()}"}
        # Everything after the node name is the command (preserve original spacing/casing).
        command = arg.strip()[len(parts[0]):].strip()
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
