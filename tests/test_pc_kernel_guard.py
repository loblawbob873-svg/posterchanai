"""os/bin/pc-kernel-guard, RUN under bash against a fake root with emerge/lsinitrd/modinfo stubbed.

"the kernel regression is a constant issue with you, you need to make sure it's fixed!" — the chain
measured on the laptop and the ISO build VM (2026-09-21): a REGISTERED gentoo-kernel-bin-6.18.48 with
5 of its 5,974 module files, whose initramfs therefore held ZERO modules and dropped to emergency
mode; a loader.conf default naming a machine-id the system no longer had, so it kept booting a
kernel no package owned; an NVIDIA module built for the other kernel. Each state is built here.
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "os" / "bin" / "pc-kernel-guard"
OLD, NEW = "6.18.43-gentoo-dist-bin", "6.18.48-gentoo-dist-bin"


def _exe(path, body):
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Machine:
    def __init__(self, tmp: Path):
        self.r = tmp / "root"
        self.bin = tmp / "bin"
        self.log = tmp / "calls.log"
        self.bin.mkdir(parents=True)
        self.log.write_text("")
        # lsinitrd: an initrd file's content decides how many modules it "holds".
        _exe(self.bin / "lsinitrd", 'n=$(cat "$1" 2>/dev/null); for i in $(seq 1 ${n:-0}); do echo "usr/lib/modules/x/kernel/m$i.ko"; done')
        # modinfo: the version is written next to the .ko.
        _exe(self.bin / "modinfo", 'cat "${@: -1}.ver" 2>/dev/null')
        self.emerge = self.bin / "emerge"
        _exe(self.emerge, f'echo "emerge $*" >> "{self.log}"; for a in "$@"; do case "$a" in =sys-kernel/*) '
                          f'. "{tmp}/restore.sh";; @module-rebuild) . "{tmp}/rebuild.sh";; esac; done')
        (tmp / "restore.sh").write_text("")
        (tmp / "rebuild.sh").write_text("")
        self.tmp = tmp

    def kernel(self, rel, *, present=True, n=3):
        pkg = f"gentoo-kernel-bin-{rel.split('-')[0]}"
        d = self.r / "var/db/pkg/sys-kernel" / pkg
        d.mkdir(parents=True, exist_ok=True)
        lines = [f"dir /lib/modules/{rel}"]
        for i in range(n):
            p = f"/lib/modules/{rel}/kernel/drivers/m{i}.ko"
            lines.append(f"obj {p} deadbeef 1")
            if present:
                (self.r / p.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
                (self.r / p.lstrip("/")).write_text("ko")
        (d / "CONTENTS").write_text("\n".join(lines) + "\n")
        (self.r / "lib/modules" / rel).mkdir(parents=True, exist_ok=True)
        return pkg

    def entry(self, mid, rel, modules=550):
        e = self.r / "boot/loader/entries"; e.mkdir(parents=True, exist_ok=True)
        k = self.r / "boot" / mid / rel; k.mkdir(parents=True, exist_ok=True)
        (k / "linux").write_text("k"); (k / "initrd").write_text(str(modules))
        (e / f"{mid}-{rel}.conf").write_text(
            f"title Gentoo\nversion {rel}\nlinux /{mid}/{rel}/linux\ninitrd /{mid}/{rel}/initrd\n")

    def loader(self, default):
        (self.r / "boot/loader").mkdir(parents=True, exist_ok=True)
        (self.r / "boot/loader/loader.conf").write_text(f"default {default}\ntimeout 1\n")

    def default(self):
        for line in (self.r / "boot/loader/loader.conf").read_text().splitlines():
            if line.startswith("default "):
                return line.split(None, 1)[1]

    def nvidia(self, rel, mod, user):
        (self.r / "var/db/pkg/x11-drivers/nvidia-drivers-" + user if False else self.r / f"var/db/pkg/x11-drivers/nvidia-drivers-{user}").mkdir(parents=True, exist_ok=True)
        v = self.r / "lib/modules" / rel / "video"; v.mkdir(parents=True, exist_ok=True)
        (v / "nvidia.ko").write_text("ko"); (v / "nvidia.ko.ver").write_text(mod)
        lib = self.r / "usr/lib64"; lib.mkdir(parents=True, exist_ok=True)
        (lib / f"libnvidia-ml.so.{user}").write_text("so")

    def run(self, mode="--repair", running=OLD):
        env = dict(os.environ, PC_ROOT=str(self.r), PC_UNAME=running, PC_EMERGE=str(self.emerge),
                   PATH=f"{self.bin}:{os.environ['PATH']}")
        p = subprocess.run([shutil.which("bash"), str(GUARD), mode], env=env, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr, self.log.read_text()


def measured_state(m):
    """The laptop / build VM on 2026-09-21."""
    m.kernel(NEW, present=False)                 # registered, module files gone
    (m.r / "lib/modules" / OLD / "kernel").mkdir(parents=True, exist_ok=True)   # running, unowned
    m.entry("da63be93", OLD)                     # the old machine-id's entry
    m.entry("6ed1a9cb", NEW, modules=0)          # installkernel's entry: a 0-module initrd
    m.loader("da63be93-*")


def test_check_names_the_empty_kernel_package_and_fails(tmp_path):
    m = Machine(tmp_path); measured_state(m)
    rc, out, calls = m.run("--check")
    assert rc == 1, out
    assert "gentoo-kernel-bin-6.18.48 is installed but 3 of its 3 kernel modules are missing" in out
    assert "not owned by any package" in out
    assert calls == "", "--check must not change anything"


def test_repair_reinstalls_it_and_points_the_loader_at_it_only_once_its_initrd_is_real(tmp_path):
    m = Machine(tmp_path); measured_state(m)
    # What reinstalling does: the module files come back and installkernel rebuilds a real initrd.
    (tmp_path / "restore.sh").write_text(
        f'for i in 0 1 2; do mkdir -p "{m.r}/lib/modules/{NEW}/kernel/drivers"; echo ko > "{m.r}/lib/modules/{NEW}/kernel/drivers/m$i.ko"; done\n'
        f'echo 600 > "{m.r}/boot/6ed1a9cb/{NEW}/initrd"\n')
    rc, out, calls = m.run("--repair")
    assert rc == 0, out
    assert "emerge -1 --usepkg --quiet-build=y =sys-kernel/gentoo-kernel-bin-6.18.48" in calls
    assert m.default() == f"6ed1a9cb-{NEW}.conf"
    assert (m.r / "boot/loader/loader.conf.pre-kernel-guard").exists()
    assert "next boot: 6.18.48" in out


def test_a_zero_module_initramfs_is_never_made_the_default(tmp_path):
    """The mistake made by hand on the build VM today: pointing the loader at 6.18.48's entry while
    its initrd was empty, which dropped the machine to emergency mode."""
    m = Machine(tmp_path); measured_state(m)
    rc, out, _ = m.run("--repair")          # restore.sh does nothing: the reinstall "fails" to fix it
    assert rc == 1
    assert m.default() == "da63be93-*", "a machine that cannot be repaired keeps the loader it had"
    assert "restored sys-kernel" not in out, "an emerge that restored nothing must not be reported as a repair"


def test_an_intact_kernel_with_an_empty_initramfs_is_never_chosen(tmp_path):
    m = Machine(tmp_path)
    m.kernel(OLD); m.kernel(NEW)
    m.entry("da63be93", OLD); m.entry("6ed1a9cb", NEW, modules=0)
    m.loader(f"da63be93-{OLD}.conf")
    rc, out, _ = m.run("--repair", running=OLD)
    assert rc == 0, out
    assert m.default() == f"da63be93-{OLD}.conf"
    assert "holds no kernel modules — never chosen" in out


def test_a_stale_machine_id_default_is_moved_to_the_newest_intact_kernel(tmp_path):
    m = Machine(tmp_path)
    m.kernel(OLD); m.kernel(NEW)
    m.entry("da63be93", OLD); m.entry("6ed1a9cb", NEW)
    m.loader("da63be93-*")
    rc, out, _ = m.run("--repair", running=OLD)
    assert rc == 0, out
    assert m.default() == f"6ed1a9cb-{NEW}.conf"


def test_nvidia_is_rebuilt_for_the_kernel_that_will_boot(tmp_path):
    m = Machine(tmp_path)
    m.kernel(NEW); m.entry("6ed1a9cb", NEW); m.loader(f"6ed1a9cb-{NEW}.conf")
    m.nvidia(NEW, "580.173.02", "580.178.04")
    (tmp_path / "rebuild.sh").write_text(f'echo 580.178.04 > "{m.r}/lib/modules/{NEW}/video/nvidia.ko.ver"\n')
    rc, out, calls = m.run("--repair", running=NEW)
    assert rc == 0, out
    assert "@module-rebuild" in calls
    assert "NVIDIA module rebuilt for 6.18.48" in out


def test_nvidia_mismatch_fails_the_check(tmp_path):
    m = Machine(tmp_path)
    m.kernel(NEW); m.entry("6ed1a9cb", NEW); m.loader(f"6ed1a9cb-{NEW}.conf")
    m.nvidia(NEW, "580.173.02", "580.178.04")
    rc, out, calls = m.run("--check", running=NEW)
    assert rc == 1 and "does not match the NVIDIA userspace" in out
    assert calls == ""


def test_a_healthy_machine_is_left_exactly_as_it_is(tmp_path):
    m = Machine(tmp_path)
    m.kernel(NEW); m.entry("6ed1a9cb", NEW); m.loader(f"6ed1a9cb-{NEW}.conf")
    before = (m.r / "boot/loader/loader.conf").read_text()
    rc, out, calls = m.run("--repair", running=NEW)
    assert rc == 0, out
    assert calls == ""
    assert (m.r / "boot/loader/loader.conf").read_text() == before
    assert "kernel, modules and boot entry agree" in out


def test_the_updater_runs_it_before_it_can_say_up_to_date():
    for f in (ROOT / "os/bin/update-posterchan",
              ROOT / "os/overlay/app-misc/posterchanos-shell/files/update-posterchan"):
        s = f.read_text()
        assert s.index("pc-kernel-guard --repair") < s.index('echo "${GRN}Already up to date.'), f
    assert (ROOT / "os/bin/pc-kernel-guard").read_bytes() == \
        (ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-kernel-guard").read_bytes()
    eb = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0-r5.ebuild").read_text()
    assert " pc-kernel-guard " in eb


def test_the_iso_refuses_a_broken_kernel_and_keeps_package_owned_opt_trees():
    g = (ROOT / "os/gentoo.sh").read_text()
    assert '"$_kg" --check' in g
    assert g.index('"$_kg" --check') < g.index("THE MODULES MUST BE BUILT FOR THE KERNEL THIS IMAGE WILL SHIP")
    assert 'grep -qsxF "dir $F" /var/db/pkg/*/*/CONTENTS && continue' in g
