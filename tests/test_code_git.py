import asyncio
import os
import subprocess
import tempfile
from unittest import mock

import pytest
from fastapi import HTTPException

from app.routers import code as C


class User:
    id = 7
    is_admin = True


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as root:
        subprocess.run(["git", "init", "-q", root], check=True)
        subprocess.run(["git", "-C", root, "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", root, "config", "user.email", "test@example.invalid"], check=True)
        with open(os.path.join(root, "a.txt"), "w") as fh: fh.write("one\n")
        subprocess.run(["git", "-C", root, "add", "a.txt"], check=True)
        subprocess.run(["git", "-C", root, "commit", "-qm", "initial"], check=True)
        with mock.patch.object(C, "_root", lambda: root), \
             mock.patch.object(C.node_service, "user_allowed", lambda db, user: True):
            yield root


def test_status_diff_stage_unstage_and_commit(repo):
    with open(os.path.join(repo, "a.txt"), "a") as fh: fh.write("two\n")
    st = run(C.git_status(db=None, current_user=User()))
    assert st["files"] == [{"xy": " M", "path": "a.txt"}]
    assert "+two" in run(C.git_diff(path="a.txt", staged=False, db=None, current_user=User()))["diff"]
    run(C.git_action(C.GitBody(action="stage", paths=["a.txt"]), None, User()))
    assert run(C.git_status(db=None, current_user=User()))["files"][0]["xy"] == "M "
    run(C.git_action(C.GitBody(action="unstage", paths=["a.txt"]), None, User()))
    run(C.git_action(C.GitBody(action="stage", paths=["a.txt"]), None, User()))
    run(C.git_action(C.GitBody(action="commit", message="second"), None, User()))
    assert not run(C.git_status(db=None, current_user=User()))["files"]


def test_restore_discards_one_tracked_or_untracked_file(repo):
    with open(os.path.join(repo, "a.txt"), "a") as fh: fh.write("changed\n")
    with open(os.path.join(repo, "new.txt"), "w") as fh: fh.write("untracked\n")
    run(C.git_action(C.GitBody(action="restore", paths=["a.txt"]), None, User()))
    assert open(os.path.join(repo, "a.txt")).read() == "one\n"
    assert os.path.exists(os.path.join(repo, "new.txt"))
    run(C.git_action(C.GitBody(action="restore", paths=["new.txt"]), None, User()))
    assert not os.path.exists(os.path.join(repo, "new.txt"))


def test_git_paths_cannot_escape_or_become_options(repo):
    with pytest.raises(HTTPException):
        run(C.git_action(C.GitBody(action="stage", paths=["../outside"]), None, User()))
    with open(os.path.join(repo, "--help"), "w") as fh: fh.write("file, not an option")
    run(C.git_action(C.GitBody(action="stage", paths=["--help"]), None, User()))
    assert "--help" in subprocess.check_output(["git", "-C", repo, "diff", "--cached", "--name-only"], text=True)


def test_shell_commands_and_arbitrary_actions_are_not_accepted(repo):
    with pytest.raises(HTTPException):
        run(C.git_action(C.GitBody(action="status; touch owned"), None, User()))
    assert not os.path.exists(os.path.join(repo, "owned"))


def test_nostr_remote_is_recognized_without_special_user_setup(repo):
    subprocess.run(["git", "-C", repo, "remote", "add", "origin", "nostr://npub1example/repo"], check=True)
    st = run(C.git_status(db=None, current_user=User()))
    assert st["nostr"] is True
