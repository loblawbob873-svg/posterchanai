"""A VRAM swap and a model load must happen INSIDE the shared GPU lock.

One GPU, several models, and only one may be resident: `prepare_for_llm`/`_image`/`_music`/`_video`/
`_voice` free the others, and `_ensure_model_loaded` brings one in. Run outside `GPUResourceLock`,
either of those reaches into a generation that is already running and takes its model away.

That is not hypothetical and it is not a near miss. On 2026-08-14 09:13 a chat message sent while a
`geni` was rendering ran `prepare_vram_for_llm` from the web-UI path — which holds no lock — and
unloaded the image model out from under a diffusers run that HELD the lock. The image job died with
`UR_RESULT_ERROR_OUT_OF_HOST_MEMORY` and llama.cpp aborted on the half-torn-down SYCL context,
core-dumping the whole service: one lost chat, one lost geni, and every other user's request on that
node gone with them. `GPUResourceLockSync`'s docstring records the SAME bug on the model-download
path. Image, music, video and voice all had it right; chat had it wrong on both of its local paths.

This matters most for the thing that is hardest to test: **the load balancer sends chat to OTHER
nodes.** A node quietly rendering an image locally can be handed a chat request over HTTP at any
moment, so the ordering has to hold on every node independently — there is no global scheduler that
could paper over it.

The check is static because the failure needs a GPU, two concurrent jobs and precise timing to
reproduce, and because the property is purely lexical: is this call inside a `with GPUResourceLock`?
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Calls that must not run unprotected: a swap frees other models, a load brings one in.
GUARDED = {
    "prepare_for_llm", "prepare_for_image", "prepare_for_music", "prepare_for_video",
    "prepare_for_voice", "prepare_vram_for_llm", "prepare_vram_for_image",
    "_ensure_model_loaded",
}
LOCKS = {"GPUResourceLock", "GPUResourceLockSync"}

# Where the GPU is actually driven. Deliberately a list rather than the whole tree: `vram_manager`
# defines these functions and calls them from one another, and the routers/tests mention them in
# prose and in stubs.
FILES = [
    "app/services/chat_service.py",
    "app/services/llama_service.py",
    "app/services/image_factory.py",
    "app/services/music_factory.py",
    "app/services/video_factory.py",
    "app/services/voice_factory.py",
    "app/services/model_download_service.py",
]

# A lexical check cannot see a lock held by a CALLER, and the load path is deliberately factored so
# that one function does the work for three entry points. So each such function is named here with
# the caller that owns its lock — which is the point of the list: it is short, every line is a claim
# somebody can check, and adding to it is a deliberate act rather than a silent exemption. If a
# fourth entry point ever calls one of these WITHOUT the lock, this list is the thing that turns out
# to have been wrong, and that is a better failure than no check at all.
ALLOWED_UNLOCKED = {
    # Runs in the executor inside `chat_completion`'s `async with GPUResourceLock`.
    ("app/services/llama_service.py", "_sync_chat_completion_no_unload"),
    # Runs in `_stream_executor` inside the lock `chat_service.chat_stream` holds around it.
    ("app/services/llama_service.py", "stream_chat_content"),
    # THE load path itself. Reached only from the three entry points above/below, all of which hold
    # the lock across it — which is exactly why the swap was moved in here on 2026-08-14.
    ("app/services/llama_service.py", "_ensure_model_loaded"),
    # Reloads the model on an explicit admin action; the caller owns the GPU at that point.
    ("app/services/llama_service.py", "reload_model"),
}


def _enclosing_functions(tree):
    """Map every node to the name of the function it is defined in (innermost wins)."""
    owner = {}

    def walk(node, name):
        for child in ast.iter_child_nodes(node):
            here = child.name if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)) else name
            owner[child] = here
            walk(child, here)

    walk(tree, "<module>")
    return owner


def _lock_protected(tree):
    """Every node lexically inside a `with`/`async with` that opens a GPU lock."""
    protected = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        opens_lock = any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(getattr(item.context_expr, "func", None), ast.Name)
            and item.context_expr.func.id in LOCKS
            for item in node.items
        )
        if not opens_lock:
            continue
        for body_stmt in node.body:
            for inner in ast.walk(body_stmt):
                protected.add(inner)
    return protected


def _unprotected_calls(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    protected = _lock_protected(tree)
    owner = _enclosing_functions(tree)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in GUARDED or node in protected:
            continue
        # A `def` whose body IS the guarded call is fine — it is a one-line indirection whose
        # callers are checked in their own right (`prepare_vram_for_llm` wraps `prepare_for_llm`).
        fn = owner.get(node, "<module>")
        if fn in ("prepare_vram_for_llm", "prepare_vram_for_image", "_ensure_llm_loaded"):
            continue
        if (path, fn) in ALLOWED_UNLOCKED:
            continue
        out.append((node.lineno, name, fn))
    return out


def test_every_vram_swap_and_model_load_is_inside_the_gpu_lock():
    problems = [(path, line, call, fn)
                for path in FILES
                for line, call, fn in _unprotected_calls(path)]
    assert not problems, (
        "these run outside GPUResourceLock and can pull a model out from under a running "
        "generation:\n" + "\n".join(
            f"  {p}:{ln}  {call}()  in {fn}()" for p, ln, call, fn in problems))


def test_the_check_can_fail(tmp_path):
    """A static check that matches nothing looks exactly like a correct tree.

    So run it over the shape that actually crashed production, and over the fix that replaced it.
    """
    crashed = tmp_path / "crashed.py"
    crashed.write_text(
        "async def chat_stream(self):\n"
        "    prepare_vram_for_llm(self.db)\n"
        "    async with GPUResourceLock('LLM', rid):\n"
        "        yield 1\n"
    )
    fixed = tmp_path / "fixed.py"
    fixed.write_text(
        "async def chat_stream(self):\n"
        "    async with GPUResourceLock('LLM', rid):\n"
        "        prepare_vram_for_llm(self.db)\n"
        "        yield 1\n"
    )

    def scan(p):
        tree = ast.parse(p.read_text())
        protected = _lock_protected(tree)
        return [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and getattr(n.func, "id", getattr(n.func, "attr", None)) in GUARDED
                and n not in protected]

    assert scan(crashed), "the pre-fix shape is no longer detected — this check is inert"
    assert not scan(fixed), "the fixed shape is being reported as a problem"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
