"""The REAL VirshBackend's cold-migration primitives, through the runner seam.

Every migration test drives FakeBackend, which carries its own copies of these methods — so the shipped
backend could lose them entirely and every migration test would stay green. That happened: a merge left
the phase-3 block indented INSIDE `parse_snapshot_list`, after its `return`, where it defined nothing.
These tests call the methods on `VirshBackend` itself and pin the argv each one runs.
"""
import asyncio

import pytest

from app.services.vmhost.backend import BackendError, VirshBackend

U = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


def run(coro):
    return asyncio.run(coro)


def backend(answers=None):
    seen = []

    async def runner(argv, timeout, stdin=None):
        seen.append(argv)
        for key, ans in (answers or {}).items():
            if key in argv:
                return ans
        return 0, "", ""
    return VirshBackend("qemu:///system", runner=runner), seen


@pytest.mark.parametrize("name", ["dumpxml_inactive", "snapshot_names", "snapshot_dumpxml", "snapshot_redefine",
                                  "undefine_for_migration", "img_info"])
def test_the_real_backend_has_every_migration_primitive(name):
    assert callable(getattr(VirshBackend, name, None)), f"VirshBackend.{name} is missing"


def test_migration_primitive_argv(tmp_path):
    be, seen = backend({"snapshot-list": (0, "clean\nafter apt\n", ""), "snapshot-current": (0, "after apt\n", "")})
    run(be.dumpxml_inactive(U))
    assert seen[-1][3:] == ["dumpxml", U, "--inactive", "--migratable"]
    assert run(be.snapshot_names(U)) == (["clean", "after apt"], "after apt")
    run(be.snapshot_redefine(U, "<domainsnapshot/>", str(tmp_path), current=True))
    assert seen[-1][3:6] == ["snapshot-create", U, str(tmp_path / ".snapshot-redefine.xml")]
    assert seen[-1][6:] == ["--redefine", "--current"]
    assert not (tmp_path / ".snapshot-redefine.xml").exists()
    run(be.undefine_for_migration(U, keep_nvram=True))
    assert seen[-1][3:] == ["undefine", U, "--snapshots-metadata", "--keep-nvram"]
    with pytest.raises(BackendError):
        run(be.snapshot_dumpxml(U, "--all"))
