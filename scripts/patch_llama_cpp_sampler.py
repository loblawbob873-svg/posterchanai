#!/usr/bin/env python3
"""
One-time patch for llama-cpp-python AttributeError: 'LlamaModel' object has no attribute 'sampler'.

Some versions of llama-cpp-python have a bug in LlamaModel.close() and __del__ that reference
self.sampler, which may not exist. This script patches the installed _internals.py to use
getattr(self, 'sampler', None) so teardown does not raise.

Run once from the project root (or with the same venv active):
  python scripts/patch_llama_cpp_sampler.py

Requires: llama-cpp-python installed in the current environment.
"""

import sys
from pathlib import Path


def main() -> int:
    try:
        import llama_cpp
    except ImportError:
        print("llama_cpp not installed in this environment. Skip patch.", file=sys.stderr)
        return 0

    # Find _internals.py next to the package
    pkg_dir = Path(llama_cpp.__file__).resolve().parent
    internals = pkg_dir / "_internals.py"
    if not internals.exists():
        print(f"_internals.py not found at {internals}", file=sys.stderr)
        return 1

    text = internals.read_text(encoding="utf-8")
    old = "if self.sampler is not None:"
    new = "if getattr(self, 'sampler', None) is not None:"

    if new in text:
        print("Patch already applied.")
        return 0
    if old not in text:
        print(f"Pattern {old!r} not found in {internals}. Library may have changed.", file=sys.stderr)
        return 1

    text = text.replace(old, new)
    internals.write_text(text, encoding="utf-8")
    print(f"Patched {internals}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
