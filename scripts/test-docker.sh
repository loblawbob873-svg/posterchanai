#!/bin/bash
# Run the whole check suite in a container. Called by ./test.sh --docker.
#
#   ./test.sh --docker                              everything self-contained
#   ./test.sh --docker --live https://poster.place  …plus the live-instance checks
#   ./test.sh --docker --group ui --jobs 2          any checkall.py flag passes straight through
#
# NOTHING IS PUBLISHED TO THE HOST. Every listener the checks open — the throwaway static servers,
# Chrome's debugging port — binds inside the container, so this is safe to run on a node that is
# already serving PosterChanAI on 3051 and already has something on 9473. There is no `-p` here and
# there must never be one.
#
# The repo is BIND-MOUNTED, not copied: it checks the tree you are about to deploy, uncommitted work
# included. The image only carries Chrome, node and the Python dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE=${PC_TEST_IMAGE:-posterchanai-test}

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not installed. Run ./test.sh without --docker to use this machine's venv." >&2
  exit 2
fi

# Rebuild when the image is missing or either requirements file is newer than it. Cheap to check,
# and the alternative — a stale image quietly testing against last month's dependencies — is the
# kind of wrong answer this suite exists to prevent.
rebuild=0
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  rebuild=1
else
  built=$(docker image inspect -f '{{.Created}}' "$IMAGE")
  built_s=$(date -d "$built" +%s 2>/dev/null || echo 0)
  for f in Dockerfile.test requirements-test.txt requirements-nostr.txt; do
    [ -f "$f" ] || continue
    [ "$(stat -c %Y "$f")" -gt "$built_s" ] && rebuild=1
  done
fi

if [ "$rebuild" = 1 ]; then
  echo "building $IMAGE (first run, or a requirements file changed)…"
  # BUILT FROM A THREE-FILE CONTEXT, not from the repo root. Two reasons, and the first one is not
  # an optimisation: docker reads the ENTIRE context before it runs a single instruction, and the
  # repo root contains runtime state this user cannot read (searxng/.secret is 0600 root) — so a
  # plain `docker build .` dies at "checking context" having done nothing. The second is that the
  # image deliberately carries no source: the repo is bind-mounted at run time, so shipping 2GB of
  # context to bake in a copy that gets shadowed anyway would be pure waste.
  ctx=$(mktemp -d)
  trap 'rm -rf "$ctx"' EXIT
  cp Dockerfile.test requirements-nostr.txt requirements-test.txt "$ctx/"
  docker build -f "$ctx/Dockerfile.test" -t "$IMAGE" "$ctx"
fi

# --shm-size: Chrome's default 64MB /dev/shm makes tabs crash under load, which reads as a flaky
#             check rather than as the resource problem it is.
# --init:     so a wedged Chrome is reaped instead of becoming a zombie holding the run open.
# The repo is mounted read-write because some checks build fixtures (the desktop bundle, meme
# renders) next to the code they check.
exec docker run --rm --init \
  --shm-size=1g \
  -v "$PWD:/app" \
  -w /app \
  -e NO_COLOR="${NO_COLOR:-}" \
  "$IMAGE" "$@"
