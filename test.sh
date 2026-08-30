#!/bin/bash
# PosterChanAI — run every check, and say what passed and what did not.
#
#   ./test.sh                              everything this machine can check on its own (~10 min)
#   ./test.sh --live https://poster.place  …plus the checks that need a running instance
#   ./test.sh --docker                     all of it in a container, nothing published to the host
#   ./test.sh --group ui                   just the browser checks
#   ./test.sh --only os_desktop,notes      one or two, by name
#   ./test.sh --list                       what would run
#
# Full docs: docs/TESTING.md
set -euo pipefail
cd "$(dirname "$0")"

# --docker anywhere in the arguments hands the whole run to the container script, minus that flag.
for a in "$@"; do
  if [ "$a" = "--docker" ]; then
    args=()
    for b in "$@"; do [ "$b" = "--docker" ] || args+=("$b"); done
    exec ./scripts/test-docker.sh ${args[@]+"${args[@]}"}
  fi
done

# The venv, found from a GIT WORKTREE too. A worktree has no venv-unified/ of its own — it is not a
# tracked file — so this used to fall through to a bare `python3` with no websockets, and 45 of the
# 85 checks then reported "websockets not installed" or a red ModuleNotFoundError. The whole board
# printed in eight seconds and looked like a suite that had run. `git rev-parse --git-common-dir`
# resolves to the MAIN checkout's .git from inside any worktree, so its parent holds the venv.
PY=venv-unified/bin/python
if [ ! -x "$PY" ]; then
  COMMON=$(git rev-parse --git-common-dir 2>/dev/null || true)
  if [ -n "$COMMON" ] && [ -x "$(dirname "$COMMON")/venv-unified/bin/python" ]; then
    PY="$(dirname "$COMMON")/venv-unified/bin/python"
  else
    PY=python3   # Docker and a bare pip install both run with the active interpreter; checkall.py
  fi             # says so out loud if that interpreter cannot actually run a check.
fi
exec "$PY" scripts/checkall.py "$@"
