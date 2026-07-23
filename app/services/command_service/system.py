"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
import asyncio
from ._common import Callable, Optional, logger


# Background node-agent runs. run_agent() is a long multi-step loop; awaiting it inline pins it to the
# chat request, so closing the app would cancel it and lose the result. Instead we run it as a DETACHED
# task with its OWN DB session and deliver the final summary through the same `notify` channel a finished
# job uses (persisted to the conversation) — so the user can close the chat and the result lands when it's
# done. We hold a reference to each task so it isn't garbage-collected mid-run.
_AGENT_BG_TASKS: set = set()


async def _agent_bg(targets, goal, uid, chat_service, notify, stop=None):
    """Run the agent over one-or-more (name, target) nodes on a FRESH session, then deliver the combined
    summary via notify({'type':'agent_result', ...}) — chat.py persists + pushes it (queued if offline).
    Each target is "local" (LLM loop runs HERE) or "nostr:<pkhex>" (the worker runs its own loop and
    returns an encrypted summary) — SSH is gone. `stop` is an asyncio.Event a delete/kill sets to end the
    run cooperatively (reliable even when task.cancel() is swallowed by run_to_completion's shield, §3)."""
    from app.database import SessionLocal
    from app.models import User as _User
    from app.services import node_service
    db = SessionLocal()
    sections = []
    multi = len(targets) > 1
    _stopped = (lambda: bool(stop and stop.is_set()))
    try:
        u = db.query(_User).filter(_User.id == uid).first() if uid else None
        for name, target in targets:
            if _stopped():
                break
            # Prefix live step-progress with the node name only when fanning out (matches the old `all` path).
            # ONLY prefix STRING progress — the agent_progress DICT must pass through untouched, else the
            # fan-out f-string turns it into a stringified dict that node_notify persists as junk (bug #3).
            nfy = notify
            if notify and multi:
                def nfy(payload, _p=name):
                    return notify(f"[{_p}] {payload}" if isinstance(payload, str) else payload)
            try:
                if target.startswith("nostr:"):
                    if nfy:
                        await nfy(f"🛰️ dispatching to `{name}` over Nostr…")
                    body = await node_service.run_agent_over_nostr(target[len("nostr:"):], goal, mode="agent")
                    sections.append(f"## Agent on `{name}` — goal: {goal}\n\n{body}")
                elif target.startswith("sandboxnostr:"):
                    _, _pk, _u = target.split(":", 2)   # placed sandbox → the worker runs container + agent
                    if nfy:
                        await nfy("🛰️ running your sandbox on its placed node…")
                    body = await node_service.run_agent_over_nostr(_pk, goal, mode="agent", sandbox_uid=_u)
                    sections.append(f"## Agent on `{name}` — goal: {goal}\n\n{body}")
                else:
                    sections.append(await node_service.run_agent(db, u, name, target, goal, chat_service,
                                                                 notify=nfy, should_stop=_stopped))
            except Exception as e:
                logger.error(f"[node] background agent error on {name}: {e}", exc_info=True)
                sections.append(f"## Agent on `{name}` — goal: {goal}\n\n**⚠️ Error:** {e}")
            finally:
                # "Delete the container when the job's done": an agent run in a sandbox tears its
                # container down afterward (the idle reaper is only a backstop for bare `node sandbox` use).
                if target.startswith("sandbox:"):
                    try:
                        from app.services import sandbox_service
                        await sandbox_service.reap(target.split(":", 1)[1], force=False)  # polite: skip if another run holds it
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


def _sandbox_capable(db):
    """Sandbox-CAPABLE hosts keyed by their STABLE host PUBKEY, so every controller computes the IDENTICAL
    set and a user's placement is consistent no matter which controller took the request (bug #1: keying on
    the controller-relative label "local" made placement diverge across nodes). Self is keyed by THIS node's
    own pubkey (target "local"); FULL npub nodes — Docker + LLM, i.e. those with their own relay — by theirs.
    A relay-less standalone agent (router.lan) has no Docker/LLM, so it's excluded. Returns {pkhex: target}."""
    from app.services import nostr_dvm
    me = (nostr_dvm.node_pubkey() or "").lower()
    cap = {}
    if me:
        cap[me] = "local"
    for _name, pk in nostr_dvm.agent_node_map().items():
        pk = (pk or "").lower()
        if not pk or pk == me:
            continue
        if nostr_dvm.agent_node_relay(pk):    # a full node (has its own relay) can host a container + LLM
            cap[pk] = f"nostr:{pk}"
    return cap


_AGENT_BG_BY_CONV: dict = {}   # conversation_id -> (task, sandbox_uids, stop_event), so deleting the chat stops the run
_REAP_TASKS: set = set()       # keep cancel-triggered reap tasks referenced so they aren't GC'd mid-run


async def _reap_after_cancel(uid) -> None:
    """Reap a cancelled run's sandbox container refcount-aware, with a short grace period. The cancelled
    task's exec releases its container hold asynchronously (as CancelledError unwinds), so we retry a
    polite (force=False) reap for a few seconds: for a single run the hold drops and it's removed
    promptly; if ANOTHER concurrent same-user run still holds it, we leave it (that run reaps on finish) —
    so deleting one chat never pulls the shared container out from under another (B1)."""
    from app.services import sandbox_service
    for _ in range(8):                       # ~4s of grace
        if await sandbox_service.reap(uid, force=False):
            return
        await asyncio.sleep(0.5)


def _spawn_agent_bg(targets, goal, uid, chat_service, notify):
    stop = asyncio.Event()   # a delete of the launch chat sets this → the run ends cooperatively (§3)
    t = asyncio.create_task(_agent_bg(targets, goal, uid, chat_service, notify, stop=stop))
    _AGENT_BG_TASKS.add(t)
    # The chat.py notify closure carries the launch conversation id (set as an attribute), so a delete
    # of that chat can find + stop THIS run. Store the stop Event + sandbox uids alongside the task so the
    # cancel can end the loop and reap the container without relying on the cancelled task's own finally.
    _conv = getattr(notify, "conv_id", None)
    _sbx_uids = [tg.split(":", 1)[1] for _n, tg in targets if str(tg).startswith("sandbox:")]
    if _conv is not None:
        _AGENT_BG_BY_CONV[_conv] = (t, _sbx_uids, stop)

    def _done(_t):
        _AGENT_BG_TASKS.discard(_t)
        if _conv is not None:
            cur = _AGENT_BG_BY_CONV.get(_conv)
            if cur and cur[0] is _t:
                _AGENT_BG_BY_CONV.pop(_conv, None)
    t.add_done_callback(_done)


def cancel_agent_for_conv(conv_id) -> bool:
    """Cancel a background agent tied to this conversation (called when the chat is deleted):
      * task.cancel() stops the agent loop and ABANDONS the in-flight LLM request — the inference
        backend aborts that generation when the client disconnects (GPU stop is best-effort, backend-dependent);
      * we ALSO reap any sandbox container DIRECTLY here (a fresh task), so the container is removed even
        if the cancelled task's own finally can't finish its await;
      * delivery is skipped, so a deliberately-deleted chat is NOT resurrected.
    Returns True if a live run was cancelled."""
    ent = _AGENT_BG_BY_CONV.pop(conv_id, None)
    if not ent:
        return False
    t, sbx_uids, stop = ent
    live = t is not None and not t.done()
    if stop is not None:
        stop.set()          # cooperative stop — reliable even if the cancel below is swallowed by a shield (§3)
    if live:
        t.cancel()
    for _u in (sbx_uids or []):
        try:
            # Refcount-aware reap with grace (B1) — don't rely on the cancelled task's own await-during-
            # cancel-fragile finally. Referenced in _REAP_TASKS so it isn't garbage-collected mid-run (L3).
            _rt = asyncio.create_task(_reap_after_cancel(_u))
            _REAP_TASKS.add(_rt)
            _rt.add_done_callback(_REAP_TASKS.discard)
        except Exception:
            pass
    return live


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
        # Sandbox load-balancing (off by default): place a user's container on a sandbox-capable node by
        # DETERMINISTIC hash — sticky (same user → same node → same container) and spread across users. If
        # the placement isn't THIS host, the sandbox target becomes "sandboxnostr:<pk>:<uid>" and the whole
        # run (container + agent loop) is dispatched to that node's worker over Nostr.
        if _sbx and self.user:
            from app.services import sandbox_service as _sbxsvc
            if _sbxsvc.lb_enabled():
                _cap = _sandbox_capable(self.db)                       # {host_pk: target}
                _placed_pk = _sbxsvc.placement_node(self.user.id, list(_cap.keys()))   # hash over STABLE pubkeys
                _pt = _cap.get(_placed_pk)
                if _pt and _pt.startswith("nostr:"):
                    # Namespace the WORKER's container by THIS controller's pubkey (bug #2): each node has its
                    # own Postgres/id space, so a bare uid could collide two different controllers' users on the
                    # same worker. `<ctrl_pk8>-<uid>` keeps tenants isolated (pcai-sbx-<ctrl_pk8>-<uid>).
                    _myid = (nostr_dvm.node_pubkey() or "anon")[:8]
                    _sbx_target = f"sandboxnostr:{_pt[len('nostr:'):]}:{_myid}-{self.user.id}"
        if _full:
            _reg = node_service.all_nodes(self.db)
            if _sbx:                                # sandbox enabled → offer it as a node to admins too
                _reg["sandbox"] = _sbx_target
        else:
            _reg = {"sandbox": _sbx_target}         # sandbox-only user: their container is the only target
        # "sandboxnostr:…" is a placed-sandbox target: it rides the local job path's shape (in `nodes`) but
        # the agent/shell branches route it over Nostr with the sandbox uid (never a bare local job).
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
                         else "🐳 your Debian sandbox" if target.startswith("sandbox:")
                         else "🐳 your Debian sandbox (placed on another node)" if target.startswith("sandboxnostr:")
                         else target)
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

        async def _dispatch_nostr(name: str, worker_pk: str, mode: str, text: str, dangerous: bool = False,
                                  sandbox_uid: str = None) -> dict:
            """Send a node/agent command to an npub-addressed worker over Nostr and render its result.
            No SSH — the worker runs it locally and returns an encrypted result (see docs/NODE_AGENT_NOSTR.md).
            `sandbox_uid` → the worker runs it inside that user's container (placed-sandbox load balancing)."""
            if notify:
                await notify(f"🛰️ dispatching to `{name}` over Nostr…" if not sandbox_uid
                             else "🛰️ running your sandbox on its placed node…")
            params = {"mode": mode, "dangerous": bool(dangerous)}
            params["command" if mode == "shell" else "goal"] = text
            if sandbox_uid:
                params["sandbox_uid"] = str(sandbox_uid)
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
            # Local nodes → background jobs; a placed sandbox → its worker over Nostr; npub workers → dispatch.
            _local_names = [n for n in nodes if not nodes[n].startswith("sandboxnostr:")]
            _placed = [(n, nodes[n].split(":", 2)) for n in nodes if nodes[n].startswith("sandboxnostr:")]
            jobs = {name: node_service.start_job(self.db, name, nodes[name], command,
                                                 user_id=self.user.id if self.user else None)
                    for name in _local_names}
            _nostr_results = await asyncio.gather(*(
                [_dispatch_nostr(name, pk, "shell", command) for name, pk in _npub_nodes.items()] +
                [_dispatch_nostr(n, p[1], "shell", command, sandbox_uid=p[2]) for n, p in _placed]
            )) if (_npub_nodes or _placed) else []
            await asyncio.gather(*(node_service.await_job(j, wait=10.0) for j in jobs.values()))
            for name, j in jobs.items():
                if j.done:
                    out = (j.output or "(no output)").strip()
                    lines.append(f"\n**{icon.get(j.status, 'ℹ️')} {name}** (exit {j.exit_code})\n```\n{node_service.tail(out, 1200)}\n```")
                else:
                    node_service.notify_on_done(j, notify)   # still running — deliver when it finishes
                    lines.append(f"\n**⏳ {name}** — still running (job #{j.id}, `node log {j.id}`)")
            for name, res in zip(list(_npub_nodes.keys()) + [n for n, _ in _placed], _nostr_results):
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
                    elif _t.startswith("sandboxnostr:"):
                        _, _pk, _u = _t.split(":", 2)
                        body = await node_service.run_agent_over_nostr(_pk, goal, mode="agent", sandbox_uid=_u)
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
                            await sandbox_service.reap(_t.split(":", 1)[1], force=False)  # polite: skip if another run holds it
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

        # Placed sandbox → run the shell command inside the user's container on its placed node over Nostr.
        if nodes[name].startswith("sandboxnostr:"):
            _, _pk, _u = nodes[name].split(":", 2)
            return await _dispatch_nostr(name, _pk, "shell", command, sandbox_uid=_u)

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
