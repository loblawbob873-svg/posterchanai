"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
import asyncio
from ._common import Callable, Optional, logger


# Background node-agent runs. run_agent() is a long multi-step loop; awaiting it inline pins it to the
# chat request, so closing the app would cancel it and lose the result. Instead we run it as a DETACHED
# task with its OWN DB session and deliver the final summary through the same `notify` channel a finished
# job uses (persisted to the conversation) — so the user can close the chat and the result lands when it's
# done. We hold a reference to each task so it isn't garbage-collected mid-run.
_AGENT_BG_TASKS: set = set()


def _archive_file_count(data: bytes) -> int:
    """How many FILES a workspace tarball holds. `.` (the archived directory itself) is not one.

    -1 means "could not tell" — an unreadable archive is still delivered, because refusing to hand
    over bytes we merely failed to parse would lose the very thing the backup exists to keep."""
    import io
    import tarfile
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            return sum(1 for m in tf.getmembers() if m.isfile())
    except Exception as e:
        logger.info("[node] could not read a workspace archive to count it: %s", e)
        return -1


async def _agent_done_dm(npub: str, goal: str, banner: str) -> None:
    """Tell the user their background agent finished, as a NIP-17 DM from the instance's operator key.

    The in-app `agent_done` push only reaches a socket that is still OPEN — which is precisely the case a
    detached run exists to handle ("close the app, get the result later"). So a user who walked away
    learned nothing until they went looking. A DM badges the client on every device they read from and
    survives being offline. Mirrors uptime_service._alert_nostr / access_notify_service: operator key →
    LOCAL relay, which federates outward. Best-effort — a failed ping must never sink a finished run."""
    # SAY what happened, always. A notification that is silently skipped is indistinguishable from one
    # that is broken — "I didn't get an agent DM" was unanswerable from the logs because the success
    # path logged nothing at all. (Same reasoning as uptime_service's "alert not sent (channel off)".)
    if not npub:
        logger.info("[node] agent-done DM skipped — the launching user has no linked npub")
        return
    try:
        from app.services import system_dm

        # Short by design: the DM is the PING, not the delivery. The full transcript already landed in
        # the conversation that launched the run — a multi-page agent log in a DM is unreadable.
        one_line = " ".join((goal or "").split())
        if len(one_line) > 160:
            one_line = one_line[:159] + "…"
        text = (f"🤖 {banner.replace('**', '')}\n\n"
                + (f"Goal: {one_line}\n\n" if one_line else "")
                + "The full transcript is in the chat you started it from (PosterChan AI).")
        # system_dm, NOT the operator key: on a single-admin node the operator key IS the admin's own
        # key, and a DM from you to you is a self-DM the client files under note-to-self — no unread
        # count, no toast. It published fine and notified nobody.
        if await system_dm.send(npub, text):
            logger.info(f"[node] agent-done DM sent to {npub[:16]}…")
    except Exception as e:
        logger.warning(f"[node] agent-done DM failed: {e}")


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
    backups = []        # (name, tar.gz bytes) — a sandbox's /workspace, auto-archived so the user gets what it built
    multi = len(targets) > 1
    npub = ""           # the launcher's npub, captured below for the completion DM
    _stopped = (lambda: bool(stop and stop.is_set()))
    try:
        u = db.query(_User).filter(_User.id == uid).first() if uid else None
        # Capture the npub NOW, while the session is alive: the completion DM fires after this session is
        # closed (a multi-minute run outlives it), so it can't go back to the ORM for it.
        npub = (getattr(u, "nostr_npub", "") or "").strip() if u else ""
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
                    # notify=nfy: stream the worker's per-step progress into this chat, exactly like the
                    # local branch below. Without it a remote/placed run shows NOTHING for its whole
                    # multi-minute life — the panel looks hung even though the node is working fine.
                    body = await node_service.run_agent_over_nostr(target[len("nostr:"):], goal, mode="agent",
                                                                   notify=nfy)
                    sections.append(f"## Agent on `{name}` — goal: {goal}\n\n{body}")
                elif target.startswith("sandboxnostr:"):
                    _, _pk, _u = target.split(":", 2)   # placed sandbox → the worker runs container + agent
                    if nfy:
                        await nfy("🛰️ running your sandbox on its placed node…")
                    body = await node_service.run_agent_over_nostr(_pk, goal, mode="agent", sandbox_uid=_u,
                                                                   notify=nfy)
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
        # Auto-backup: the agent builds things in the sandbox's persistent /workspace volume — archive it
        # so the user gets what it made without a second command. The volume outlives the reap above, so
        # this re-creates a throwaway container on it (then reaps that), or on a placed sandbox drives its
        # worker over Nostr. Best-effort: a backup failure must never sink the run's actual result.
        for name, target in targets:
            if _stopped():
                break
            if not (target.startswith("sandbox:") or target.startswith("sandboxnostr:")):
                continue   # only a sandbox has a /workspace worth keeping; a full host node doesn't
            try:
                _bdata, _berr = await node_service.archive_dir(db, u, name, target, "/workspace")
                if _bdata:
                    backups.append((name, _bdata))
                elif _berr:
                    logger.info(f"[node] workspace backup skipped for {name}: {_berr}")
                if target.startswith("sandbox:"):   # reap the container archive_dir just re-created
                    try:
                        from app.services import sandbox_service
                        await sandbox_service.reap(target.split(":", 1)[1], force=False)
                    except Exception:
                        pass
            except Exception as _be:
                logger.warning(f"[node] workspace backup failed for {name}: {_be}")
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
        # A clear TERMINAL banner so the user can always tell the run ended (and how). A dispatched
        # (nas) run is silent until this single delivery, and a long transcript buries the outcome —
        # so end with an unmistakable done/stopped/error line ("can't tell if it finished/died").
        content = "\n\n---\n\n".join(sections)
        # Derive the banner from the run's actual terminal marker (run_agent emits exactly one):
        #   ✅ Done  = the model CALLED finish → the goal was completed
        #   ⏹️ Stopped = reached step limit / stuck / cancelled → did NOT finish
        #   ⚠️ Error/Stopped = exception / inference error
        # "complete" must mean ✅ Done, NOT merely "no ⚠️" — else a step-limit stop (different emoji)
        # is mislabelled as complete, which is exactly what happened.
        if _stopped():
            banner = "⏹️ **Agent run cancelled.**"
        elif any("⚠️ Error:" in s or "⚠️ Stopped:" in s for s in sections):
            banner = "⚠️ **Agent run ended with an error** — see the transcript above."
        elif any("✅ Done:" in s for s in sections):
            banner = "✅ **Agent run complete.**"
        elif any("⏹️ Stopped:" in s for s in sections):
            banner = "⏹️ **Agent stopped before finishing** (step limit or stuck) — see the transcript above."
        else:
            banner = "☑️ **Agent run ended.**"
        try:
            await notify({"type": "agent_result", "content": f"{content}\n\n{banner}"})
        except Exception as e:
            logger.warning(f"[node] background agent deliver failed: {e}")
        # Then ping the user on the SOCIAL side, so a run they walked away from actually reaches them
        # (the in-app toast needs a live socket; a DM badges every device and waits). Skipped when THEY
        # cancelled it — telling someone about the thing they just stopped is noise, not news.
        if _stopped():
            logger.info("[node] agent-done DM skipped — the run was cancelled by the user")
        else:
            await _agent_done_dm(npub, goal, banner)
        # Then hand back what the agent BUILT: each sandbox's /workspace as a downloadable .tar.gz.
        # Delivered as its own `agent_files` payload so each interface stores it its own way (web →
        # encrypted Blossom + link, Telegram → a document) — the same split the `type:files` path uses.
        for _bname, _bdata in backups:
            try:
                # AN EMPTY WORKSPACE IS NOT A BACKUP, and it is impossible to tell from the message.
                # An agent that worked in /tmp (a `git clone /tmp/pc` and a test run — a real one)
                # leaves /workspace untouched, and a tar.gz of an empty directory is still ~190
                # bytes: the line read "📦 workspace backup (191 bytes, gzipped)" with a download
                # button beside it, so the archive downloaded to nothing and the DOWNLOAD looked
                # broken. Say which it is instead, and name where the files would have had to be.
                _n = _archive_file_count(_bdata)
                if _n == 0:
                    await notify({"type": "agent_result",
                                  "content": f"📦 `{_bname}`: nothing to back up — `/workspace` is "
                                             "empty. The agent built its files somewhere else (a "
                                             "run that works in `/tmp` is the usual reason); only "
                                             "`/workspace` survives the container and is archived."})
                    continue
                await notify({"type": "agent_files",
                              "content": f"📦 `{_bname}` workspace backup ({len(_bdata):,} bytes, gzipped)",
                              "files": [{"filename": f"{_bname}-workspace.tar.gz", "data": _bdata,
                                         "content_type": "application/gzip"}]})
            except Exception as e:
                logger.warning(f"[node] workspace backup deliver failed for {_bname}: {e}")


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
      * we set the cooperative `stop` Event ONLY — the run exits between steps after its current LLM
        step finishes natively. We must NEVER hard-cancel (`task.cancel()`) a live agent: on the Arc
        SYCL llama.cpp stack, injecting CancelledError abandons the in-flight native generation while
        its executor thread is still inside libggml, and the ensuing teardown corrupts the glibc heap
        ("corrupted double-linked list" → SIGABRT core-dump that took the WHOLE process + live stream
        down, 2026-07-23). Cooperative stop costs at most one extra step (~seconds) and never crashes;
      * we reap any sandbox container DIRECTLY here (a fresh task), so the container is removed even
        if the run's own finally can't finish its await;
      * delivery is skipped for the deleted chat (the `_was_deleted` guard in chat.py), so it is NOT
        resurrected.
    Returns True if a live run was signalled to stop."""
    ent = _AGENT_BG_BY_CONV.pop(conv_id, None)
    if not ent:
        return False
    t, sbx_uids, stop = ent
    live = t is not None and not t.done()
    if stop is not None:
        stop.set()          # cooperative stop — the ONLY safe way to end a GPU-inference agent run
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



def fleet_targets(reg: dict) -> dict:
    """What `all` means: THE FLEET, never the sandbox.

    The sandbox is in the registry so it can be PICKED — it is a legitimate target by name — but it is
    a throwaway Debian container with no relation to any host, so putting it in a fan-out is wrong
    twice over. The answers are meaningless (asking every machine its uptime and getting a fresh
    container's), and it is not free: each run spins a container up, archives its /workspace afterwards
    and reaps it, so every `node all …` paid for a sandbox nobody asked about.

    A sandbox-ONLY user is the exception and keeps it: with no fleet to fan out over, `all` meaning
    "nothing" would be a dead command with a confusing message.
    """
    fleet = {n: t for n, t in (reg or {}).items() if n != "sandbox"}
    return fleet or dict(reg or {})


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
            return {"type": "text", "content": "⛔ Agentic node management is disabled or you are not authorized. An admin can enable it in Admin → Nodes → Agentic Node Management."}

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
        # Global agent node (Admin → Nodes → 'Agent node', empty = this host): pin every sandbox run to
        # ONE node so all agentic GPU work funnels through a single worker, serialized by that worker's
        # existing 1-at-a-time agent lock (the queue). When it names a full peer node, the sandbox target
        # becomes "sandboxnostr:<pk>:<uid>" and the whole run (container + agent loop) is dispatched to that
        # worker over Nostr. (Replaced the old deterministic sha256(uid)%nodes container LB.)
        if _sbx and self.user:
            from app.services import sandbox_service as _sbxsvc
            _an = _sbxsvc.agent_node_name()                            # configured node NAME, or "" = this host
            if _an:
                _me = (nostr_dvm.node_pubkey() or "").lower()
                _an_pk = (nostr_dvm.agent_node_map().get(_an, "") or "").lower()
                if _an_pk and _an_pk != _me and nostr_dvm.agent_node_relay(_an_pk):
                    # Namespace the WORKER's container by THIS controller's pubkey: each node has its own
                    # Postgres/id space, so a bare uid could collide two different controllers' users on the
                    # same worker. `<ctrl_pk8>-<uid>` keeps tenants isolated (pcai-sbx-<ctrl_pk8>-<uid>).
                    _myid = (nostr_dvm.node_pubkey() or "anon")[:8]
                    _sbx_target = f"sandboxnostr:{_an_pk}:{_myid}-{self.user.id}"
                # else: 'Agent node' names THIS host (or an unknown/relay-less node) → local sandbox (default)
        if _full:
            _reg = node_service.all_nodes(self.db)
            if _sbx:                                # sandbox enabled → offer it as a node to admins too
                _reg["sandbox"] = _sbx_target
        else:
            _reg = {"sandbox": _sbx_target}         # sandbox-only user: their container is the only target
        def _fanout() -> dict:
            return fleet_targets(_reg)

        # "sandboxnostr:…" is a placed-sandbox target: it rides the local job path's shape (in `nodes`) but
        # the agent/shell branches route it over Nostr with the sandbox uid (never a bare local job).
        nodes = {n: t for n, t in _reg.items() if not t.startswith("nostr:")}
        _npub_nodes = {n: t[len("nostr:"):] for n, t in _reg.items() if t.startswith("nostr:")}

        def _fmt_nodes() -> str:
            if not nodes and not _npub_nodes:
                return "No nodes configured. Add them in Admin → Nodes → Agentic Node Management (one per line: `name|user@host`, or a Nostr worker `name npub1…`)."
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

        async def _dispatch_nostr(name: str, worker_pk: str, mode: str, text: str,
                                  sandbox_uid: str = None) -> dict:
            """Send a node/agent command to an npub-addressed worker over Nostr and render its result.
            No SSH — the worker runs it locally and returns an encrypted result (see docs/NODE_AGENT_NOSTR.md).
            `sandbox_uid` → the worker runs it inside that user's container (placed-sandbox load balancing)."""
            if notify:
                await notify(f"🛰️ dispatching to `{name}` over Nostr…" if not sandbox_uid
                             else "🛰️ running your sandbox on its placed node…")
            params = {"mode": mode}
            params["command" if mode == "shell" else "goal"] = text
            if sandbox_uid:
                params["sandbox_uid"] = str(sandbox_uid)
            # on_progress=notify → the worker's per-step play-by-play lands in THIS chat live, the same
            # as a local run. Without it a placed run showed nothing at all until it finished.
            out = await nostr_dvm.run_remote("agent", params, worker_pubkey=worker_pk, on_progress=notify)
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
                "- `node get <name> <path>` — download a file from a node/sandbox (→ Blossom)\n"
                "- `node backup <name> [dir]` — archive a working dir (default `/workspace`) → Blossom\n"
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

        # --- pull a file OFF a node/sandbox → an encrypted Blossom artifact + download link ---
        # The agent builds things in its sandbox /workspace (a Docker volume on the worker); this is how
        # a human gets one back out. Reuses the chat's `type:files` path, so the bytes land in Blossom
        # (encrypted) and come back as a download link in the web UI or a document on Telegram.
        if sub == "get":
            if len(parts) < 3 or not parts[2].strip():
                return {"type": "text", "content": "Usage: `node get <name> <path>` — fetch a file from a node/sandbox."}
            name, path = parts[1], parts[2].strip()
            if name not in _reg:
                return {"type": "text", "content": f"Unknown node `{name}`.\n\n{_fmt_nodes()}"}
            raw, err = await node_service.run_shell_on_target(self.db, self.user, name, _reg[name],
                                                              node_service.download_command(path))
            if err:
                return {"type": "text", "content": f"⚠️ `{name}`: {err}"}
            data, derr = node_service.decode_download(raw or "")
            if derr:
                return {"type": "text", "content": f"⚠️ `{path}` on `{name}`: {derr}"}
            if not data:   # 0-byte file: the files/Blossom path drops empty blobs, so say so plainly
                return {"type": "text", "content": f"📭 `{path}` on `{name}` is empty (0 bytes) — nothing to download."}
            fname = path.rstrip("/").rsplit("/", 1)[-1] or "file"
            return {
                "type": "files",
                "content": f"📦 `{path}` from `{name}` ({len(data):,} bytes)",
                "files": [{"filename": fname, "data": data}],
            }

        # --- archive a whole working dir (default the sandbox /workspace) → one .tar.gz in Blossom ---
        if sub == "backup":
            if len(parts) < 2:
                return {"type": "text", "content": "Usage: `node backup <name> [dir]` — archive a directory (default `/workspace`) → Blossom."}
            name = parts[1]
            if name not in _reg:
                return {"type": "text", "content": f"Unknown node `{name}`.\n\n{_fmt_nodes()}"}
            target = _reg[name]
            directory = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else None
            if not directory:
                # /workspace is the sandbox's persistent volume; a host node has no default, so name one.
                if "sandbox" in target:
                    directory = "/workspace"
                else:
                    return {"type": "text", "content": f"Usage: `node backup {name} <dir>` — a host node has no default workspace, name the directory."}
            if notify:
                await notify(f"📦 archiving `{directory}` on `{name}`…")
            data, err = await node_service.archive_dir(self.db, self.user, name, target, directory)
            if err:
                return {"type": "text", "content": f"⚠️ backup of `{directory}` on `{name}`: {err}"}
            base = directory.rstrip("/").rsplit("/", 1)[-1] or "workspace"
            return {
                "type": "files",
                "content": f"📦 `{directory}` from `{name}` ({len(data):,} bytes, gzipped)",
                "files": [{"filename": f"{base}.tar.gz", "data": data, "content_type": "application/gzip"}],
            }

        # --- fan-out: run the same command on every node (local jobs + Nostr workers) ---
        if sub == "all":
            import asyncio
            command = arg.strip()[len(parts[0]):].strip()
            if not command:
                return {"type": "text", "content": "Usage: `node all <command>`"}
            if not _reg:
                return {"type": "text", "content": _fmt_nodes()}
            # The fleet, never the sandbox — see _fanout. `nodes`/`_npub_nodes` are derived from the
            # full registry, so they are narrowed here rather than at the top: the sandbox is still a
            # target you can NAME, it just is not one of "every node".
            _fan = _fanout()
            nodes = {n: t for n, t in _fan.items() if not t.startswith("nostr:")}
            _npub_nodes = {n: t[len("nostr:"):] for n, t in _fan.items() if t.startswith("nostr:")}
            icon = {"done": "✅", "failed": "❌", "killed": "🛑"}
            lines = [f"## `{command}` on {len(_fan)} node(s)"]
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
                targets = list(_fanout().items())     # the fleet, never the sandbox — see _fanout
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
