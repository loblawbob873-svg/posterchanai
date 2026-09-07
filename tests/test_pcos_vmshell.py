"""The three-hour feedback loop, and the tool that replaces it.

Nearly every defect found in `check_scratch_install_vm.py`'s subject lives in the LAST FIVE MINUTES
of a three-hour build: the installer-version gate, the repos check, three separate false greens in
the @world conflict check, the ABI convergence sweep. Each was tested by rebuilding the whole
operating system to reach the phase that had failed. This tool asks the same questions of the disk
the gate already kept, in about ninety seconds — and these tests exist because a diagnostic nobody
can find is a diagnostic nobody uses, which is how it came to be written twice.
"""
from pathlib import Path
import importlib.util
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts/pcos_vmshell.py"
SRC = TOOL.read_text(encoding="utf-8")


def test_it_is_not_discovered_as_a_check():
    """`./test.sh` runs every scripts/check_*.py it finds. This judges nothing and must never be
    counted as a gate — nor start a VM on an ordinary suite run."""
    assert not TOOL.name.startswith("check_"), "the suite would run a diagnostic as a gate"
    listed = subprocess.run([sys.executable, str(ROOT / "scripts/checkall.py"), "--list"],
                            capture_output=True, text=True, cwd=ROOT)
    assert "pcos_vmshell" not in listed.stdout


def test_it_shares_one_console_implementation_with_the_gate():
    """A second copy of "how to talk to the guest" is a second copy to keep in step — and the bugs
    in that code (bracketed-paste framing, a marker matching the console's echo of its own command)
    would have had to be found twice."""
    assert "check_scratch_install_vm.py" in SRC, "the console protocol was copied, not imported"
    for shared in ("_GATE.Serial", "_GATE.ovmf", "_GATE.iso_label", "_GATE.qemu_args"):
        assert shared in SRC, f"{shared} is reimplemented here instead of imported"


def test_a_command_marker_cannot_be_satisfied_by_its_own_echo():
    """The console echoes everything sent to it, which has produced a false answer twice in this
    codebase already."""
    spec = importlib.util.spec_from_file_location("pcos_vmshell", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.shell_quote("it's") == "'it'\"'\"'s'"
    import re
    echoed = ("root@livecd ~ # chroot /tmp/install /bin/bash -lc 'x' >/tmp/vs0.log 2>&1; "
              "echo VSDONE0=$?\r\n\x1b[?2004l\rVSDONE0=3\r\n")
    assert re.findall(r"VSDONE0=(\d+)", echoed)[-1] == "3"


def test_it_cannot_be_mistaken_for_evidence_of_a_pass():
    """It reports what it was asked and judges nothing. Only the gate decides that a build passed,
    against a machine it built itself."""
    assert "must never be cited as evidence that" in SRC
    assert "WHAT IT IS NOT: a gate" in SRC


def test_it_refuses_rather_than_pretends_when_it_cannot_run():
    got = subprocess.run([sys.executable, str(TOOL)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"})
    assert got.returncode == 2
    assert "nothing to ask" in got.stdout
