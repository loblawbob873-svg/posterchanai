# Node/Agent over Nostr — design spec

Replace SSH-based remote node execution with a **Nostr transport**: a command is an encrypted NIP-90
event addressed to a worker node's `npub`. The worker runs it **locally** and returns an encrypted
result. No SSH keys, no open ports, NAT-friendly. Extends the existing DVM (`app/services/nostr_dvm.py`),
which already does "listen for events from a trusted npub, process, return" for GPU offload.

Status: **Phase 1 (config) DONE** — settings + Admin UI (`node_exec_nostr_*`, `node_exec_node_npubs`,
`node_exec_trusted_npubs`) committed (`2cc473f2`), NOT deployed. Runtime below is TODO.

## Trust / security model
- **Dedicated allowlist** `node_exec_trusted_npubs` (NOT the DVM peer trust — command exec ≫ GPU offload).
  A worker executes a command ONLY if the event is signed by an npub on ITS list (Nostr sig = auth,
  replaces SSH keys).
- The controller side is still gated by `node_exec_users` (who can issue commands from the app).
- **Whitelisted npubs ONLY** (no open/anon requests) — a request from an npub not on `node_exec_trusted_npubs`
  is dropped silently.
- **Serialized execution: ONE request at a time, QUEUED per worker.** The worker pushes accepted jobs onto
  a FIFO queue and runs them one-by-one (a single-slot `asyncio.Lock`/`Queue`, like the DVM's
  `GPUResourceLock`) so a burst of requests can't overload the box. Optionally send an interim "queued (N
  ahead)" result event so the controller can show status. Extra jobs wait, not run in parallel.

## Wire protocol (both the app-worker AND the standalone agent MUST match this)
- **Request** kind `5300`, **Result** kind `5300+1000 = 6300` (NIP-90 5xxx range; slot after chat/image/
  music/video in `_REQ_KIND`). Add `_REQ_KIND["agent"] = 5300`.
- Request event: authored by the CONTROLLER's key, tags `["p", <worker_pubkey_hex>]`, content =
  **NIP-44 `encrypt_to(controller_sk, worker_pk, json)`** where json =
  `{"id": <jobid>, "task": "agent", "mode": "shell"|"agent", "command"|"goal": "...", "ts": <unix>}`.
- Result event: authored by the WORKER's key, tags `["p", <controller_pubkey_hex>]`, `["e", <req_id>]`,
  content = NIP-44 `encrypt_to(worker_sk, controller_pk, json)` where json =
  `{"id": <jobid>, "status": "done"|"error", "summary": "...", "output": "...", "exit": <int|null>}`.
  Live step progress = interim result events with `{"id", "step": "⚙️ `cmd`"}` (optional).
- Reuse `nostr/event.build_event`, `nostr/nip44.encrypt_to/decrypt_from`, `nostr_dvm`'s relay + expiry.

## Components
1. **Config + Admin UI** — DONE (Phase 1).
2. **App-worker** (full PosterChanAI nodes: nas, server1): in `nostr_dvm._run_local`, add
   `if task == "agent": ...` → run `node_service.run_command`/`run_agent` LOCALLY (target `local`).
   Subscribe path already exists (`_spawn_job` verifies trust → extend its trust check to
   also accept `node_exec_trusted_npubs`).
3. **Controller** — `_node_command`: when `node_exec_nostr_enabled` and the node name resolves to an npub
   (`node_exec_node_npubs`), dispatch via `nostr_dvm.run_remote("agent", …)` instead of SSH; stream steps
   and deliver the summary through the existing `notify`/`agent_result` path (web + Telegram).
4. **Standalone lightweight agent** (`agent/pcnode_agent.py` + vendored nostr core) — for router.lan / other
   people's machines that DON'T run the full app: generates a keypair on first run, **prints its npub**
   (to paste into `node_exec_node_npubs` + as a worker), connects to the relay, subscribes for kind-5300
   `p`-tagged to itself from `--trust <npub>`, decrypts, executes (shell/agent), publishes 6300.
   Minimal deps (websocket + the pure-python core). `install.sh --agent`: interactive setup, writes
   `pcnode-agent.service` (systemd), prints the npub at the end.
5. **logs_scheduler (syslogs)** — reuses `run_agent`, so it inherits the transport automatically once (3)
   routes `local`/remote through Nostr; verify the health report still resolves nodes by npub.
7. **Live-node config** — after the runtime works+tests: set nas.lan + server1.lan node npubs + mutual
   trust in Admin → Nodes.

## Build order (each independently testable)
Protocol (this doc) → standalone agent (test: it prints an npub, connects, echoes a signed shell cmd) →
app-worker handler → controller dispatch → logs_scheduler check → configure live nodes.
SSH path stays as fallback during cutover.
