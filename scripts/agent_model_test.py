#!/usr/bin/env python3
"""Can a model build a small working web app BY ITSELF, on this node?

    scripts/agent_model_test.py --model models/Qwen3.6-27B-IQ4_XS.gguf

WHY THIS EXISTS, AND WHY IT IS NOT A PROMPT IN A CHAT WINDOW. "Can we run model X" is two questions
that get answered as one and should not be: whether the weights LOAD on this node's GPU, and whether
what comes out is worth loading. The first is easy to check and is the one everybody checks. The
second is the one that decides whether a model replaces Qwen3-Coder-30B, and eyeballing a snippet
answers it badly — a model that writes plausible Flask and never actually serves a request reads,
in a chat window, exactly like one that works.

So the task is graded by RUNNING it. The model gets tools (write a file, read one, list the
directory, run a command), a spec, and a step budget; then this script starts what it wrote as a
subprocess and drives real HTTP at it — create a document, list it, edit it, read the edit back,
delete it, confirm it is gone. Nothing about the grade is a judgement call, and nothing in it asks
another model's opinion.

IT RUNS THROUGH THE PRODUCTION PATH ON PURPOSE. The loop calls
``app.services.tool_calling.generate_message`` and sizes the load with
``llama_service._compute_autofit_gpu_layers`` — the same prompt rendering, the same tool-call
parsing, the same GPU/CPU split the app itself would use. A bespoke harness would measure a
configuration nobody ships: this repo has already been bitten by a model whose native tool format
(``<function=NAME><parameter=KEY>``) the app parses and a generic client does not, and testing
against the generic one would have called that model broken.

IT TAKES THE NODE'S GPU LOCK. Both GPU nodes serve live traffic on one card each, and the app frees
VRAM between requests rather than warm-keeping the LLM. Loading 15 GB beside it without the lock is
how you OOM a stranger's image generation, so this waits its turn on the same
``/tmp/posterchanai_locks/gpu.lock`` every other GPU task uses, and holds it for the run.

Exit code is 0 when the app it wrote passed every CRUD step, 1 when it did not, and 2 when the test
could not be RUN at all (no model file, no llama_cpp) — the same three-way answer the ``check_*``
scripts give, so a node that simply does not have the weights is never reported as a pass.

DELIBERATELY NOT NAMED ``check_*``: ``test.sh`` discovers those and runs them all, and this one wants
a 15 GB download and around twenty minutes of a GPU that is also serving traffic. It is run by hand,
per node, when a new model is being considered — not on every deploy.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

GPU_LOCK = "/tmp/posterchanai_locks/gpu.lock"

# The brief. It names an HTTP contract because the grade is machine-checked and a grader cannot
# guess someone's route names — but everything ABOVE the contract (storage, HTML, JS, error
# handling, how the editor actually behaves) is left to the model, which is the part being measured.
TASK = """Build a web-based text editor with a Python backend, in the current directory.

Users must be able to create, edit and delete documents.

Requirements:
  * One file: `app.py`. It must run as `python app.py` with NO pip install — Python standard
    library only (http.server / json / sqlite3 are all fine). Nothing may be fetched from the
    internet at runtime.
  * It must listen on 127.0.0.1 on the port in the environment variable PORT (default 8000).
  * `GET /` must return an HTML page: the editor UI. A document list, a text area to edit the
    document, and controls to create, save and delete. It has to work with no external CDN.
  * A JSON API, exactly these routes:
      GET    /api/documents         -> [{"id": ..., "title": ...}, ...]
      POST   /api/documents         <- {"title": ..., "content": ...}   -> {"id": ...}
      GET    /api/documents/<id>    -> {"id": ..., "title": ..., "content": ...}
      PUT    /api/documents/<id>    <- {"title": ..., "content": ...}
      DELETE /api/documents/<id>
  * Documents must persist while the server runs.

Work on your own: write the file with your tools, then RUN it and check it before you finish.
When it is finished and you have verified it, reply with the single word DONE."""

TOOLS = [
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a file with the given text, relative to the working directory.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path relative to the working directory."},
            "content": {"type": "string", "description": "The complete new contents of the file."}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file back, relative to the working directory.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List the files in the working directory.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "run",
        "description": ("Run a shell command in the working directory and get its output. Use this to "
                        "test what you wrote. Commands are killed after 25 seconds, so start a server "
                        "in the background if you need it running."),
        "parameters": {"type": "object", "properties": {
            "cmd": {"type": "string", "description": "The shell command."}}, "required": ["cmd"]}}},
]


def _say(msg: str) -> None:
    print(msg, flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---- the sandbox the model works in ------------------------------------------------------------

class Sandbox:
    """The four tools, confined to one directory.

    `_safe` is not a security boundary — a `run` tool is a shell and this is a test on a node that
    already offers deliberate RCE — it is there so a model that writes to `/etc/...` by accident
    fails its own test instead of the machine's.
    """

    def __init__(self, workdir: str, python: str):
        self.dir = workdir
        self.python = python
        self.writes = 0

    def _safe(self, path: str) -> str:
        full = os.path.realpath(os.path.join(self.dir, path))
        if not full.startswith(os.path.realpath(self.dir) + os.sep) and full != os.path.realpath(self.dir):
            raise ValueError("path is outside the working directory")
        return full

    def write_file(self, path: str = "", content: str = "", **_) -> str:
        full = self._safe(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content or "")
        self.writes += 1
        return f"wrote {path} ({len(content or '')} bytes)"

    def read_file(self, path: str = "", **_) -> str:
        with open(self._safe(path), encoding="utf-8", errors="replace") as fh:
            return fh.read()[:8000]

    def list_dir(self, **_) -> str:
        out = []
        for name in sorted(os.listdir(self.dir)):
            p = os.path.join(self.dir, name)
            out.append(f"{name}  {os.path.getsize(p)} bytes" if os.path.isfile(p) else f"{name}/")
        return "\n".join(out) or "(empty)"

    def run(self, cmd: str = "", **_) -> str:
        try:
            r = subprocess.run(cmd, shell=True, cwd=self.dir, capture_output=True, text=True,
                               timeout=25, env={**os.environ, "PYTHON": self.python})
        except subprocess.TimeoutExpired:
            return "(timed out after 25s — if this was a server, start it with & so it runs in the background)"
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        return (f"exit {r.returncode}\n" + out.strip())[:4000] or f"exit {r.returncode} (no output)"

    def call(self, name: str, args: dict) -> str:
        fn = {"write_file": self.write_file, "read_file": self.read_file,
              "list_dir": self.list_dir, "run": self.run}.get(name)
        if not fn:
            return f"no such tool: {name}"
        try:
            return fn(**(args if isinstance(args, dict) else {}))
        except Exception as exc:                      # a tool error is INFORMATION, not a crash:
            return f"error: {exc}"                    # the model gets it back and can correct itself


# ---- grading: run what it wrote and drive real HTTP at it ---------------------------------------

def _req(url: str, method: str = "GET", body=None, timeout: float = 10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return r.status, raw


def _json(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        return None


def verify(workdir: str, python: str) -> dict:
    """Start `app.py` and put it through create → list → read → edit → delete.

    Every step is recorded whether it passed or not, because "it failed" is not a useful report: the
    interesting result is a model that gets four of six steps right, and a bare False cannot say so.
    """
    steps: list[tuple[str, bool, str]] = []

    def step(name: str, ok: bool, detail: str = "") -> bool:
        steps.append((name, bool(ok), detail))
        return bool(ok)

    app = os.path.join(workdir, "app.py")
    if not os.path.isfile(app):
        step("app.py exists", False, "the model never wrote app.py")
        return {"passed": False, "steps": steps}
    step("app.py exists", True, f"{os.path.getsize(app)} bytes")

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([python, "app.py"], cwd=workdir, env={**os.environ, "PORT": str(port)},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        up = False
        for _ in range(60):                                   # 30s: a stdlib server binds instantly,
            if proc.poll() is not None:                       # so this is mostly about a crash loop
                break
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                up = True
                break
            except OSError:
                time.sleep(0.5)
        if not up:
            out = ""
            try:
                proc.kill()
                out = (proc.stdout.read() or "")[-600:]
            except Exception:
                pass
            step("server listens on PORT", False, out.strip() or "never opened the port")
            return {"passed": False, "steps": steps}
        step("server listens on PORT", True, f"port {port}")

        try:
            status, html = _req(base + "/")
            ok = status == 200 and re.search(r"<textarea|contenteditable", html, re.I) is not None
            step("GET / serves an editor UI", ok,
                 f"status {status}, {len(html)} bytes" + ("" if ok else ", no textarea/contenteditable"))
        except Exception as exc:
            step("GET / serves an editor UI", False, str(exc))

        doc_id = None
        try:
            status, raw = _req(base + "/api/documents", "POST",
                               {"title": "notes", "content": "first version"})
            body = _json(raw)
            doc_id = (body or {}).get("id") if isinstance(body, dict) else None
            step("POST creates a document", doc_id is not None, f"status {status}, id={doc_id!r}")
        except Exception as exc:
            step("POST creates a document", False, str(exc))

        try:
            status, raw = _req(base + "/api/documents")
            body = _json(raw)
            found = isinstance(body, list) and any(
                str((d or {}).get("id")) == str(doc_id) for d in body if isinstance(d, dict))
            step("GET lists it", found, f"status {status}, {len(body) if isinstance(body, list) else '?'} item(s)")
        except Exception as exc:
            step("GET lists it", False, str(exc))

        if doc_id is not None:
            try:
                _req(f"{base}/api/documents/{doc_id}", "PUT",
                     {"title": "notes", "content": "second version"})
                status, raw = _req(f"{base}/api/documents/{doc_id}")
                body = _json(raw) or {}
                ok = body.get("content") == "second version"
                step("PUT edits it and the edit reads back", ok, f"content={body.get('content')!r}")
            except Exception as exc:
                step("PUT edits it and the edit reads back", False, str(exc))

            try:
                _req(f"{base}/api/documents/{doc_id}", "DELETE")
                gone = False
                try:
                    status, raw = _req(f"{base}/api/documents/{doc_id}")
                    gone = status in (404, 410)
                except urllib.error.HTTPError as e:
                    gone = e.code in (404, 410)
                # A list that no longer carries it is just as good an answer as a 404.
                if not gone:
                    _, raw = _req(base + "/api/documents")
                    body = _json(raw)
                    gone = isinstance(body, list) and not any(
                        str((d or {}).get("id")) == str(doc_id) for d in body if isinstance(d, dict))
                step("DELETE removes it", gone, "" if gone else "still present after delete")
            except Exception as exc:
                step("DELETE removes it", False, str(exc))
    finally:
        try:
            proc.kill()
        except Exception:
            pass

    return {"passed": all(ok for _, ok, _ in steps), "steps": steps}


# ---- the agent loop ------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--ctx", type=int, default=32768)
    ap.add_argument("--steps", type=int, default=40)
    ap.add_argument("--workdir", default="")
    ap.add_argument("--report", default="")
    ap.add_argument("--flash-attn", default="")   # "true"/"false"; per-node (Arc OFF, CUDA on)
    args = ap.parse_args()

    model_path = args.model if os.path.isabs(args.model) else os.path.join(ROOT, args.model)
    if not os.path.isfile(model_path):
        _say(f"SKIP: no model at {model_path}")
        return 2
    try:
        from llama_cpp import Llama
        from app.services.tool_calling import generate_message
        from app.services.llama_service import _compute_autofit_gpu_layers
    except Exception as exc:
        _say(f"SKIP: cannot import the inference stack here ({exc})")
        return 2

    workdir = args.workdir or os.path.join("/tmp", f"agent-test-{os.getpid()}")
    os.makedirs(workdir, exist_ok=True)
    node = os.uname().nodename
    flash = str(args.flash_attn).lower() == "true"

    os.makedirs(os.path.dirname(GPU_LOCK), exist_ok=True)
    lock = os.open(GPU_LOCK, os.O_CREAT | os.O_RDWR)
    _say(f"[{node}] waiting for this node's GPU lock…")
    fcntl.flock(lock, fcntl.LOCK_EX)                  # blocking on purpose: we are not the priority
    _say(f"[{node}] got the GPU lock")

    size = os.path.getsize(model_path)
    layers, why = _compute_autofit_gpu_layers(model_path, size, args.ctx, flash_attn=flash)
    _say(f"[{node}] model {os.path.basename(model_path)} ({size/2**30:.2f} GiB), ctx {args.ctx}")
    _say(f"[{node}] autofit: {why}")

    t0 = time.time()
    model = Llama(model_path=model_path, n_ctx=args.ctx, n_gpu_layers=layers,
                  n_batch=512, flash_attn=flash, verbose=False)
    load_s = time.time() - t0
    _say(f"[{node}] loaded in {load_s:.1f}s")

    box = Sandbox(workdir, sys.executable)
    messages = [
        {"role": "system", "content": "You are a careful software engineer. You have tools and you "
                                      "use them: you write files with write_file and you check your "
                                      "work by running it. Never claim something works without "
                                      "running it."},
        {"role": "user", "content": TASK},
    ]
    params = {"temperature": 0.3, "top_p": 0.9, "max_tokens": 4096}

    calls = 0
    finished = ""
    gen_s = 0.0
    for step_i in range(args.steps):
        t = time.time()
        try:
            msg, reason = generate_message(model, messages, TOOLS, params, disable_thinking=True)
        except Exception as exc:
            finished = f"generation failed: {exc}"
            break
        gen_s += time.time() - t
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            finished = (msg.get("content") or "").strip()
            _say(f"[{node}] step {step_i+1}: finished — {finished[:120]!r}")
            break
        messages.append(msg)
        for tc in tool_calls:
            fn = (tc.get("function") or {})
            name = fn.get("name") or "?"
            raw = fn.get("arguments")
            a = raw if isinstance(raw, dict) else (_json(raw or "{}") or {})
            result = box.call(name, a)
            calls += 1
            _say(f"[{node}] step {step_i+1}: {name}({', '.join(sorted(a))}) -> {result.splitlines()[0][:90] if result else ''}")
            messages.append({"role": "tool", "tool_call_id": tc.get("id") or "", "content": result})
    else:
        finished = f"hit the {args.steps}-step budget"

    del model                                          # free VRAM before the graded app starts
    os.close(lock)                                     # closing the fd releases the flock

    _say(f"[{node}] --- grading what it wrote ---")
    result = verify(workdir, sys.executable)
    for name, ok, detail in result["steps"]:
        _say(f"[{node}]   {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))

    verdict = "PASS" if result["passed"] else "FAIL"
    _say(f"[{node}] {verdict} — {calls} tool calls, {box.writes} writes, "
         f"{gen_s:.0f}s generating, loaded in {load_s:.0f}s, workdir {workdir}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump({"node": node, "model": os.path.basename(model_path), "passed": result["passed"],
                       "steps": [{"name": n, "ok": o, "detail": d} for n, o, d in result["steps"]],
                       "tool_calls": calls, "writes": box.writes, "gen_seconds": round(gen_s, 1),
                       "load_seconds": round(load_s, 1), "gpu_layers": layers, "autofit": why,
                       "ctx": args.ctx, "final_message": finished[:500], "workdir": workdir}, fh, indent=2)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
