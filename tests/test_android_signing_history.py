"""Fixture coverage for the history-aware Android signing-secret gate."""

from pathlib import Path
import subprocess

from scripts.check_no_android_signing_history import (
    HistoryUnavailable,
    main,
    reachable_private_signing_paths,
)


def git(repo, *args, input=None):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input,
        text=True,
        check=True,
        capture_output=True,
    ).stdout.strip()


def repository(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Signing Guard Test")
    git(tmp_path, "config", "user.email", "signing-guard@example.invalid")
    return tmp_path


def commit_file(repo, relative, contents):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    git(repo, "add", "--", relative)
    git(repo, "commit", "-qm", f"add {relative}")
    return git(repo, "rev-parse", f"HEAD:{relative}")


def test_deleted_keystore_remains_a_reachable_failure(tmp_path):
    repo = repository(tmp_path)
    object_id = commit_file(
        repo, "mobile/android/posterchan release.keystore", b"fixture-key-bytes"
    )
    (repo / "mobile/android/posterchan release.keystore").unlink()
    git(repo, "add", "-u")
    git(repo, "commit", "-qm", "delete signing key")

    assert reachable_private_signing_paths(repo) == [
        (object_id, "mobile/android/posterchan release.keystore")
    ]


def test_failure_diagnostic_discloses_object_and_path_but_not_key_bytes(
    tmp_path, capsys
):
    repo = repository(tmp_path)
    object_id = commit_file(repo, "release.jks", b"do-not-copy-this-key")

    assert main(["--repo", str(repo)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Android signing-history guard failed:\n"
        f"  - reachable private signing blob: {object_id} release.jks\n"
    )
    assert "do-not-copy-this-key" not in captured.err


def test_clean_complete_history_passes(tmp_path):
    repo = repository(tmp_path)
    commit_file(repo, "mobile/android/README.md", b"no private key material\n")

    assert reachable_private_signing_paths(repo) == []


def test_shallow_history_is_not_treated_as_clean(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    commit_file(repo, "README.md", b"fixture\n")

    import scripts.check_no_android_signing_history as guard

    real_git = guard._git

    def shallow(repo_path, *args):
        if args == ("rev-parse", "--is-shallow-repository"):
            return "true\n"
        return real_git(repo_path, *args)

    monkeypatch.setattr(guard, "_git", shallow)
    try:
        reachable_private_signing_paths(repo)
    except HistoryUnavailable as exc:
        assert "shallow or incomplete" in str(exc)
    else:
        raise AssertionError("shallow history was incorrectly accepted as clean")
