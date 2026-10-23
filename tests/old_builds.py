"""OLDER BUILDS OF THIS REPOSITORY, FROM ITS OWN HISTORY -- for the upgrade tests (a device or a node holding data an
older build wrote must open it on today's build).

Commits are found relative to HEAD's commit date, never hard-coded, so the tests keep testing "last month's data" as
the tree moves. Files come out of `git archive` into a temporary directory: no checkout, no worktree to clean up.

A shallow checkout (the desktop CI's actions/checkout, fetch-depth 1) holds no older build: `unavailable()` says so
and the tests skip with that reason.
"""
import datetime
import io
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args, binary=False):
    return subprocess.run(['git', *args], cwd=str(ROOT), capture_output=True, text=not binary, timeout=120)


def unavailable():
    """Why this checkout cannot provide older builds ('' when it can)."""
    try:
        shallow = git('rev-parse', '--is-shallow-repository')
    except (OSError, subprocess.SubprocessError) as e:
        return 'no git here: %s' % e
    if shallow.returncode != 0:
        return 'not a git checkout: %s' % shallow.stderr.strip()
    if shallow.stdout.strip() != 'false':
        return 'a shallow checkout (the desktop CI) holds no older build'
    return ''


def head_time():
    return datetime.datetime.fromisoformat(git('log', '-1', '--format=%cI', 'HEAD').stdout.strip())


def before(when, needs='static/js/client/app.js'):
    """The newest commit on HEAD's history dated before `when` that has `needs`, or ''."""
    sha = git('rev-list', '-1', '--before=' + when.isoformat(), 'HEAD').stdout.strip()
    if not sha or git('cat-file', '-e', sha + ':' + needs).returncode != 0:
        return ''
    return sha


def client_builds():
    """{label: sha}: the last client build before HEAD's day, 14 and 30 days before HEAD. {'': why} if unavailable."""
    why = unavailable()
    if why:
        return {'': why}
    at = head_time()
    wanted = {'yesterday': at.replace(hour=0, minute=0, second=0, microsecond=0),
              '2_weeks': at - datetime.timedelta(days=14),
              '1_month': at - datetime.timedelta(days=30)}
    out = {}
    for label, when in wanted.items():
        sha = before(when)
        if not sha:
            return {'': 'the history has no client build before %s' % when.isoformat()}
        out[label] = sha
    return out


def first_commit_with(path):
    """The commit that ADDED `path` (the oldest build that has it), or ''."""
    out = git('log', '--diff-filter=A', '--format=%H', '--', path).stdout.split()
    return out[-1] if out else ''


def extract(sha, into, paths=('static', 'templates')):
    """`paths` of `sha` into the directory `into`, from git's own archive."""
    tar = git('archive', '--format=tar', sha, *paths, binary=True)
    assert tar.returncode == 0, tar.stderr
    with tarfile.open(fileobj=io.BytesIO(tar.stdout)) as t:
        t.extractall(into, filter='data')
    return Path(into)
