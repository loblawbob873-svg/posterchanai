"""The end of an install converges the 32-bit (Steam) ABI from BOTH things portage says -- found by the from-scratch VM.

2026-10-27, `check_scratch_install_vm.py` run 2: the build completed and the installed machine's `emerge -uDNp @world`
FAILED -- "The following USE changes are necessary to proceed: >=media-libs/libpng-1.6.59-r1 abi_x86_32,
>=x11-libs/libX11-1.8.14 abi_x86_32, >=x11-libs/libXrender-0.9.13 abi_x86_32" (Steam's chain had left libXfixes
installed with abi_x86_32). The install's sweep only read the OTHER shape (an upgrade SKIPPED due to a conflict); the
failure shape was read on updates only. That run's real output is the fixture. installABI32Sweep is run under bash
with `chroot` stubbed: the first resolution answers with the fixture, the next one clean.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
G = (ROOT / "os/gentoo.sh").read_text()
FIX = ROOT / "tests/fixtures/portage_world_scratch_run2_20261027.txt"


def _fn(name):
    start = G.index(name + "() {")
    end = G.index("\n}\n", start) + 3
    return G[start:end]


def _run(tmp_path, first_answer):
    target = tmp_path / "t"
    (target / "etc/portage").mkdir(parents=True)
    first = tmp_path / "first.txt"
    first.write_text(first_answer)
    script = "\n".join([_fn("skippedABI32Packages"), _fn("proposedABI32Changes"), _fn("installABI32Sweep"), f"""
TARGET={target}
n=0
chroot(){{ shift; case "$*" in
  *"emerge -uDNp @world"*) n=$((n+1)); [ -f {tmp_path}/asked ] && cat {tmp_path}/asked >/dev/null; echo x >>{tmp_path}/asked
     if [ "$(wc -l <{tmp_path}/asked)" -eq 1 ]; then cat {first}; return 1; fi; echo 'Total: 0 packages'; return 0;;
  *) return 0;; esac; }}
installABI32Sweep
"""])
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    use = target / "etc/portage/package.use/posterchan-abi"
    return r, (use.read_text().split("\n") if use.exists() else [])


def test_the_failure_shape_from_the_vm_is_converged(tmp_path):
    r, use = _run(tmp_path, FIX.read_text())
    assert r.returncode == 0, r.stderr
    got = sorted(x for x in use if x)
    assert got == ["media-libs/libpng abi_x86_32", "x11-libs/libX11 abi_x86_32", "x11-libs/libXrender abi_x86_32"], got
    assert "edk2-bin" not in " ".join(use), "an exact-version pin must never gain an ABI flag"


def test_a_proposal_with_any_other_flag_is_refused(tmp_path):
    other = FIX.read_text().replace(">=x11-libs/libX11-1.8.14 abi_x86_32", ">=x11-libs/libX11-1.8.14 abi_x86_32 doc")
    r, use = _run(tmp_path, other)
    assert r.returncode == 0, r.stderr
    assert not [x for x in use if x], ("a proposal that is not only abi_x86_32 was written", use)
