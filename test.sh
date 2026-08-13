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

PY=venv-unified/bin/python
[ -x "$PY" ] || PY=python3
exec "$PY" scripts/checkall.py "$@"
