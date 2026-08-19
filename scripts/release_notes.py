#!/usr/bin/env python3
"""Release notes for the Zapstore listing, written from the commits that are actually in the build.

Zapstore publishing has been automated since it was set up — every APK build publishes — but the
release went out with NO NOTES, so the store showed a version number and nothing about what changed.
`zsp` takes them from a `release_notes:` path in zapstore.yaml, so this writes that file in CI.

THE HARD PART IS NOT THE GIT LOG, IT IS DECIDING WHAT A USER CARES ABOUT. This repo's commit
subjects are written as sentences about behaviour ("The checksum was wrong, not the file"), which
makes them unusually good release-note material — but a build also contains work nobody outside
should read: test-only changes, CI, docs, the OS installer. Those are dropped by looking at what the
commit TOUCHED, not by matching words in it, because a subject line is prose and the paths are fact.

A release with nothing user-facing in it is a real outcome and gets a short honest line rather than
an invented one.
"""
import argparse
import os
import re
import subprocess
import sys

# What ships INSIDE the APK. A commit that touched none of these changed nothing a phone will run.
SHIPPED = (
    "static/js/", "static/css/", "static/i18n/", "static/fonts/", "static/img/",
    "templates/client.html", "mobile/",
)
# ...and these are inside those paths but are not the product.
NOT_SHIPPED = ("mobile/android/app/src/test", "mobile/www/",)


def run(args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120).stdout


def commits(since, root):
    rng = f"{since}..HEAD" if since else "-40"
    out = run(["git", "log", "--no-merges", "--format=%H%x1f%s", rng], cwd=root)
    rows = []
    for line in out.splitlines():
        if "\x1f" not in line:
            continue
        sha, subject = line.split("\x1f", 1)
        rows.append((sha.strip(), subject.strip()))
    return rows


def touches_shipped(sha, root):
    files = run(["git", "show", "--name-only", "--format=", sha], cwd=root).split()
    for f in files:
        if any(f.startswith(p) for p in NOT_SHIPPED):
            continue
        if any(f.startswith(p) for p in SHIPPED):
            return True
    return False


def clean(subject):
    """One line, as a user would read it. Kept as written — these subjects are already about
    behaviour — with only the mechanical noise taken off."""
    s = subject.strip()
    s = re.sub(r"^(fix|feat|chore|refactor|docs|test)(\([^)]*\))?:\s*", "", s, flags=re.I)
    # An em-dash subject is "headline — why"; the headline is the part that belongs in a store, and
    # the threshold only exists to stop a two-word fragment becoming the whole line.
    head = s.split(" — ")[0].strip()
    if len(head) >= 14:
        s = head
    # A store listing is read at a glance. Cut at a clause boundary rather than mid-word, and only
    # when there is a boundary late enough to be worth cutting at.
    if len(s) > 88:
        cut = max(s.rfind(", ", 0, 88), s.rfind("; ", 0, 88), s.rfind(": ", 0, 88))
        s = (s[:cut] if cut >= 40 else s[:88].rsplit(" ", 1)[0]).rstrip(",;: ")
    return s[:1].upper() + s[1:] if s else s


def build(since, version, root):
    picked, seen = [], set()
    for sha, subject in commits(since, root):
        if not touches_shipped(sha, root):
            continue
        line = clean(subject)
        key = line.lower()
        if not line or key in seen:
            continue
        seen.add(key)
        picked.append(line)
        if len(picked) >= 12:
            break

    head = run(["git", "rev-parse", "--short", "HEAD"], cwd=root).strip()
    out = []
    if version:
        out.append(f"## {version}")
        out.append("")
    if picked:
        out += [f"- {p}" for p in picked]
    else:
        # Honest, and it happens: a build whose only changes were tests, CI or the server.
        out.append("- Maintenance build — no user-facing changes since the last release.")
    out += ["", f"_Built from `{head}`._"]
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=os.environ.get("NOTES_SINCE", ""),
                    help="commit to start after; empty = the last 40 commits")
    ap.add_argument("--version", default=os.environ.get("NOTES_VERSION", ""))
    ap.add_argument("--out", default="RELEASE_NOTES.md")
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    a = ap.parse_args()

    since = a.since.strip()
    if since:
        # A stale or rewritten sha must not fail the build — it falls back to the recent window.
        ok = subprocess.run(["git", "cat-file", "-e", since + "^{commit}"], cwd=a.root,
                            capture_output=True).returncode == 0
        if not ok:
            print(f"release_notes: {since!r} is not a commit here — using the recent window",
                  file=sys.stderr)
            since = ""

    text = build(since, a.version.strip(), a.root)
    with open(os.path.join(a.root, a.out), "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)


if __name__ == "__main__":
    main()
