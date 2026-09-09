"""A test must not write into the LIVE repository store, and for years several did.

`tests/test_git_host_serve.py` carried `ghs.GIT_PROJECT_ROOT = tmp   # module attr read at call
time`. It is not read at call time and there is no such attribute: `git_project_root()` resolves
GRASP_GIT_PROJECT_ROOT, then the `upload_path` setting, then /var/lib/posterchanai/git_repos — lazily,
so that the pre-receive/post-receive subprocesses resolve the SAME root the server served from. Every
one of those assignments was a no-op, so the tests created bare repositories in the live store on
every run: test_git_host_serve, test_git_push_e2e, test_git_proxy, and two GRASP-08 files added in
this branch.

HOW IT WAS FOUND, because the shape is worth knowing: /var/lib/posterchanai/git_repos held exactly one
owner directory, `774ae7f8…`, containing privrepo.git (private, readers `421f5fc9…`) and pubrepo.git.
Those were reported up the chain as "the real hosted repos" and used to reason about our production
privacy model. `774ae7f8…` is the pubkey of the integer secret key **11** and `421f5fc9…` is the
pubkey of key **22** — test_git_host_serve's own `owner_sk` and `reader_sk`. Test residue, read as
production data. (The conclusion drawn from it — that our private repos are private by never being
announced — happens to be correct, but it is `create_repo`'s empty `announcement_addr` and
`app/routers/git.py`'s refusal to announce that say so, not those directories.)

CLAUDE.md already states the rule — "never write into the working tree's live state — a test that
touched `streamserver/mediamtx.pid` passed on a laptop and PermissionError'd on every node that was
actually serving". On a node that serves git, this is somebody's repository directory.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.services import git_host_service as ghs       # noqa: E402

def test_setting_the_module_attribute_does_NOT_redirect_the_repo_store(tmp_path, monkeypatch):
    """A GUARD FOR A BUG THAT WAS LIVE, not a hypothetical.

    `tests/test_git_host_serve.py` said `ghs.GIT_PROJECT_ROOT = tmp   # module attr read at call
    time`. It is not read at call time and there is no such attribute: `git_project_root()` resolves
    GRASP_GIT_PROJECT_ROOT, then the `upload_path` setting, then /var/lib/posterchanai/git_repos. So
    that test — and two others, and two of my own — created bare repositories in the LIVE store on
    every run. Found because `/var/lib/posterchanai/git_repos` held exactly one owner directory,
    `774ae7f8…`, which is the pubkey of the integer secret key 11, with a `readers` entry of
    `421f5fc9…`, the pubkey of key 22: the test's own fixtures, mistaken for production data.

    This is the rule CLAUDE.md already states — "never write into the working tree's live state" —
    and the reason it is stated: on a node that is actually serving, this is somebody's repository
    directory.
    """
    monkeypatch.delenv("GRASP_GIT_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(ghs, "GIT_PROJECT_ROOT", str(tmp_path), raising=False)
    assert ghs.git_project_root() != str(tmp_path), (
        "the attribute idiom appears to work — re-read this test before trusting it")


def test_the_env_var_is_what_actually_redirects_it(tmp_path, monkeypatch):
    monkeypatch.setenv("GRASP_GIT_PROJECT_ROOT", str(tmp_path))
    assert ghs.git_project_root() == str(tmp_path)


def test_no_test_in_this_suite_redirects_the_store_by_attribute():
    """The behavioural test above proves the idiom is a no-op; this one proves nobody is using it.
    Both are needed: the first cannot see a caller, and the second cannot see a rename."""
    import pathlib
    offenders = []
    for f in sorted(pathlib.Path(_ROOT, "tests").glob("test_*.py")):
        text = f.read_text()
        if f.name == pathlib.Path(__file__).name:
            continue                      # this file demonstrates the idiom in order to refute it
        if "ghs.GIT_PROJECT_ROOT =" in text or 'setattr(ghs, "GIT_PROJECT_ROOT"' in text:
            offenders.append(f.name)
    assert not offenders, "these tests write into the live repo store: %s" % offenders
