"""scripts/pin_server_overlay.sh — how app-misc/posterchan-server follows the code into the overlay.

Run for real against a throwaway git repository and a stubbed `curl`. Three things are pinned: a changed
server gets a NEW version whose PC_COMMIT and Manifest describe the same archive (a changed ebuild under an
old version is invisible to portage, and a Manifest for other bytes is "VERIFY FAILED" on every machine);
an UNCHANGED server keeps the last published ebuild byte for byte (a new version on every publish would make
every PosterChanOS machine re-download ~90 MB for a feature most of them have switched off); and a failed
fetch never fails the publish.
"""
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "pin_server_overlay.sh"
PKG = "app-misc/posterchan-server"
EBUILD = next((ROOT / "os/overlay" / PKG).glob("posterchan-server-*.ebuild"))


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}).stdout.strip()


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "app/main.py").write_text("v1\n")
    (repo / "tests/test_x.py").write_text("t1\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "one")
    stage = tmp_path / "stage" / PKG
    stage.mkdir(parents=True)
    shutil.copy2(EBUILD, stage / EBUILD.name)
    (stage / "Manifest").write_text("DIST committed\n")
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "curl").write_text('#!/bin/bash\necho "curl $*" >> "$CURL_LOG"\n'
                               '[ -n "${CURL_FAIL:-}" ] && exit 22\n'
                               'while [ $# -gt 0 ]; do [ "$1" = -o ] && { printf "archive-of-%s" "${@: -1}" > "$2"; }; '
                               'shift; done\n')
    (stub / "curl").chmod(0o755)
    return {"tmp": tmp_path, "repo": repo, "stage": tmp_path / "stage", "stub": stub}


def pin(w, sha, prev="", **env):
    log = w["tmp"] / "curl.log"
    log.write_text("")
    e = {"PATH": f"{w['stub']}:/usr/bin:/bin", "PC_REPO": str(w["repo"]), "CURL_LOG": str(log), **env}
    r = subprocess.run(["bash", str(SCRIPT), str(w["stage"]), str(prev), sha], env=e, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout, log.read_text().splitlines()


def ebuilds(stage):
    return sorted((stage / PKG).glob("posterchan-server-*.ebuild"))


def test_a_changed_server_gets_a_new_version_a_new_pin_and_a_matching_manifest(world):
    sha = git(world["repo"], "rev-parse", "HEAD")
    out, calls = pin(world, sha, PC_SERVER_VERSION="1.0.20260921000000")
    [eb] = ebuilds(world["stage"])
    assert eb.name == "posterchan-server-1.0.20260921000000.ebuild"
    assert f'PC_COMMIT="{sha}"' in eb.read_text()
    assert any(f"/archive/{sha}.tar.gz" in c for c in calls)
    body = f"archive-of-https://github.com/loblawbob873-svg/posterchanai/archive/{sha}.tar.gz".encode()
    manifest = (world["stage"] / PKG / "Manifest").read_text().split()
    assert manifest[:3] == ["DIST", "posterchan-server-1.0.20260921000000.tar.gz", str(len(body))]
    assert manifest[manifest.index("SHA512") + 1] == hashlib.sha512(body).hexdigest()
    assert manifest[manifest.index("BLAKE2B") + 1] == hashlib.blake2b(body).hexdigest()


def _publish_once(world):
    """A previously published overlay pinned to the first commit."""
    sha1 = git(world["repo"], "rev-parse", "HEAD")
    pin(world, sha1, PC_SERVER_VERSION="1.0.1")
    prev = world["tmp"] / "prev"
    shutil.copytree(world["stage"], prev)
    # A fresh staging tree, as publish_overlay.sh builds one from os/overlay every time.
    shutil.rmtree(world["stage"] / PKG)
    (world["stage"] / PKG).mkdir()
    shutil.copy2(EBUILD, world["stage"] / PKG / EBUILD.name)
    (world["stage"] / PKG / "Manifest").write_text("DIST committed\n")
    return sha1, prev


def test_an_unchanged_server_keeps_the_published_ebuild_and_downloads_nothing(world):
    sha1, prev = _publish_once(world)
    (world["repo"] / "tests/test_x.py").write_text("t2\n")   # not something the package installs
    git(world["repo"], "commit", "-qam", "tests only")
    sha2 = git(world["repo"], "rev-parse", "HEAD")
    out, calls = pin(world, sha2, prev, PC_SERVER_VERSION="1.0.2")
    assert calls == [], "an unchanged server must not be re-fetched"
    [eb] = ebuilds(world["stage"])
    assert eb.name == "posterchan-server-1.0.1.ebuild"
    assert eb.read_bytes() == (prev / PKG / "posterchan-server-1.0.1.ebuild").read_bytes()
    assert (world["stage"] / PKG / "Manifest").read_bytes() == (prev / PKG / "Manifest").read_bytes()


def test_a_server_code_change_is_pinned_even_with_a_previous_publish(world):
    sha1, prev = _publish_once(world)
    (world["repo"] / "app/main.py").write_text("v2\n")
    git(world["repo"], "commit", "-qam", "server change")
    sha2 = git(world["repo"], "rev-parse", "HEAD")
    pin(world, sha2, prev, PC_SERVER_VERSION="1.0.2")
    [eb] = ebuilds(world["stage"])
    assert eb.name == "posterchan-server-1.0.2.ebuild" and f'PC_COMMIT="{sha2}"' in eb.read_text()


def test_a_failed_fetch_keeps_the_published_pin_and_does_not_fail_the_publish(world):
    sha1, prev = _publish_once(world)
    (world["repo"] / "app/main.py").write_text("v3\n")
    git(world["repo"], "commit", "-qam", "server change")
    out, _ = pin(world, git(world["repo"], "rev-parse", "HEAD"), prev, CURL_FAIL="1")
    assert "WARNING" in out
    [eb] = ebuilds(world["stage"])
    assert eb.name == "posterchan-server-1.0.1.ebuild" and f'PC_COMMIT="{sha1}"' in eb.read_text()


def test_a_failed_fetch_with_nothing_published_ships_the_committed_pin(world):
    out, _ = pin(world, git(world["repo"], "rev-parse", "HEAD"), "", CURL_FAIL="1")
    [eb] = ebuilds(world["stage"])
    assert eb.read_bytes() == EBUILD.read_bytes()
    assert (world["stage"] / PKG / "Manifest").read_text() == "DIST committed\n"


def test_publish_overlay_runs_the_pin_and_ships_the_one_helper():
    src = (ROOT / "scripts/publish_overlay.sh").read_text()
    assert "pin_server_overlay.sh" in src
    assert 'bin/pc-server" "$TMP/app-misc/posterchan-server/files/pc-server"' in src
    # The previous pin must be saved BEFORE the staging tree is emptied, or there is never one to keep.
    assert src.index('cp -a "$STAGE/app-misc/posterchan-server"') < src.index('find "$STAGE" -mindepth 1')


def test_the_committed_pin_has_a_manifest_for_exactly_its_archive():
    manifest = (ROOT / "os/overlay" / PKG / "Manifest").read_text().split()
    version = EBUILD.name.removeprefix("posterchan-server-").removesuffix(".ebuild")
    assert manifest[0] == "DIST" and manifest[1] == f"posterchan-server-{version}.tar.gz"
