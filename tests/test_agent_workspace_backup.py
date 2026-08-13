"""An empty workspace must not be handed over as a backup.

Run: venv-unified/bin/python -m pytest tests/test_agent_workspace_backup.py

Every sandbox agent run ends by archiving `/workspace` and delivering it as a download. The archive
is made unconditionally, and a tar.gz of an EMPTY directory is still ~190 bytes — so a run whose
agent worked somewhere else (a `git clone … /tmp/pc` then `./test.sh`, which is a real run that
happened) produced:

    📦 `sandbox` workspace backup (191 bytes, gzipped)   [⬇️ sandbox-workspace.tar.gz]

There is nothing in that line to tell it from a real backup, and downloading it gives you nothing —
which reads as the DOWNLOAD being broken, and was reported that way alongside a genuine download bug
in the APK. So the delivery counts the files first and says which case it is.

  counts-files          `.` (the archived directory itself) is not a file; a real file is
  unreadable-is-kept    a tarball we cannot parse is still delivered — refusing to hand over bytes we
                        merely failed to READ would lose the thing the backup exists to keep
  empty-is-not-a-file   the delivery branches on the count, not on the byte size (an empty archive is
                        not zero bytes, which is what made this invisible)
"""
import io
import re
import tarfile
from pathlib import Path

from app.services.command_service.system import _archive_file_count

SRC = Path(__file__).resolve().parents[1] / "app/services/command_service/system.py"


def _tgz(names):
    """A gzipped tar shaped like the sandbox's own: the directory itself, then its files."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        d = tarfile.TarInfo(".")
        d.type = tarfile.DIRTYPE
        tf.addfile(d)
        for n in names:
            ti = tarfile.TarInfo("./" + n)
            ti.size = 3
            tf.addfile(ti, io.BytesIO(b"abc"))
    return buf.getvalue()


def test_counts_files():
    assert _archive_file_count(_tgz([])) == 0
    assert _archive_file_count(_tgz(["a.txt"])) == 1
    assert _archive_file_count(_tgz(["a.txt", "b/c.txt"])) == 2


def test_an_empty_archive_is_not_empty_bytes():
    """The reason this was invisible: the empty case still weighs ~200 bytes and reads as content."""
    assert len(_tgz([])) > 50


def test_unreadable_is_kept():
    assert _archive_file_count(b"not a tarball at all") == -1
    assert _archive_file_count(b"") == -1


def test_delivery_branches_on_the_count():
    """The `agent_files` payload must be guarded by the count — the one thing a unit test of the
    helper cannot prove on its own, since the helper is useless if nothing calls it."""
    src = SRC.read_text()
    i = src.index('"type": "agent_files"')
    before = src[max(0, i - 1200):i]
    assert "_archive_file_count" in before, \
        "the workspace delivery no longer counts the archive's files before offering it"
    assert re.search(r"_n\s*==\s*0", before), \
        "nothing branches on an EMPTY archive — an empty /workspace would be offered as a download"
