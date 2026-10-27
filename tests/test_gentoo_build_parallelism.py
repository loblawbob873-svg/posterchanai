"""The installer's build parallelism is sized from RAM as well as CPUs, so a big C++ compile is not OOM-killed.

Measured 2026-10-27 in the from-scratch VM (8 GB, 16 threads): MAKEOPTS was -j16 with `--jobs 5`, and both
dev-libs/boost and media-video/obs-studio died "g++: fatal error: Killed signal terminated program cc1plus" -- the
package set did not complete. Runs the SHIPPED buildParallelism and the lines that write make.conf.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SH = (ROOT / "os/gentoo.sh").read_text()


def _fn():
    start = SH.index("buildParallelism() {")
    return SH[start:SH.index("\n}\n", start) + 3]


def _jobs(mem_kb, cpus):
    out = subprocess.run(["bash", "-c", _fn() + f'\nbuildParallelism {mem_kb} {cpus}'], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    make, emerge = map(int, out.stdout.split())
    return make, emerge


GB = 1024 * 1024   # kB


def test_the_vm_that_was_oom_killed_now_builds_within_its_memory():
    make, emerge = _jobs(8 * GB, 16)
    assert make * emerge * 2 <= 8, ("compile jobs x parallel packages x 2 GB exceeds 8 GB", make, emerge)
    assert (make, emerge) == (4, 1)


def test_big_machines_keep_their_parallelism_and_tiny_ones_still_build():
    assert _jobs(64 * GB, 16) == (16, 5)           # what every machine got before
    assert _jobs(128 * GB, 32) == (32, 5)
    assert _jobs(16 * GB, 16) == (8, 2)
    assert _jobs(1 * GB, 8) == (1, 1)               # never zero
    assert _jobs(64 * GB, 4) == (4, 5)              # never more compile jobs than CPUs
    assert _jobs(0, 0)[0] == 1                      # unreadable meminfo: still a working build


def test_make_conf_gets_the_computed_values():
    head = SH[SH.index("buildParallelism() {"):SH.index("ROOT_PARTITION_SIZE=")]
    assert 'MAKEOPTS="-j${_PC_MAKE_JOBS:-1}"' in head
    tail = SH[SH.index("ROOT_PARTITION_SIZE="):][:400]
    assert 'EMERGE_DEFAULT_OPTS="--jobs ${_PC_EMERGE_JOBS:-1} --getbinpkg "' in tail
    assert "--jobs 5 --getbinpkg" not in SH, "a fixed --jobs 5 is back"
    # The two values are what the installed system's make.conf is written from.
    assert 'echo "MAKEOPTS=\\"$MAKEOPTS\\"" >>$TARGET/etc/portage/make.conf' in SH
    assert 'echo "EMERGE_DEFAULT_OPTS=\\"$EMERGE_DEFAULT_OPTS\\"" >>$TARGET/etc/portage/make.conf' in SH
