"""THE FROM-SCRATCH BUILD HAD NO ENTRY POINT AND THEREFORE NO GATE.

`install-live` copies an already-built live image onto a disk; `install` assumes somebody has
already wiped and LUKS-formatted the target by hand through menu option 5. So the path that actually
BUILDS PosterChanOS — stage3, portage, @world, the kernel, the package set, the bootloader — could
only be driven by a person typing at a menu, and a thing only a person can run is a thing that gets
tested when somebody happens to reinstall.

These run the installer's own shell functions with the destructive halves replaced, and the gate's
own logic. The end-to-end build is the gate itself, run against a real VM.
"""
from pathlib import Path
import ast
import importlib.util
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "os/gentoo.sh"
SH = SCRIPT.read_text(encoding="utf-8")
GATE = ROOT / "scripts/check_scratch_install_vm.py"
SRC = GATE.read_text(encoding="utf-8")
SPEC = importlib.util.spec_from_file_location("scratch_install_vm", GATE)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)
BASH = shutil.which("bash") or "/bin/bash"


def run_sh(body, env=None, stdin=""):
    """Source the real installer (`help` so the dispatcher does not open the menu), then run `body`.

    Sourcing rather than grepping: every rule below is about what the script DOES, and the script is
    4000 lines of shell whose behaviour a regular expression can only guess at.
    """
    script = f'. "{SCRIPT}" help >/dev/null 2>&1\n{body}\n'
    # bash by ABSOLUTE path: some checks below hand the script a deliberately narrow PATH, and
    # looking the interpreter up in that PATH would fail before the script ever ran.
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                          input=stdin, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", **(env or {})})


def test_an_unset_repo_choice_means_our_mirror_not_upstream():
    """`local` is gentoo.poster.place: the binhost that makes a from-scratch build finish at all, and
    a webrsync snapshot that works off this LAN. An unset variable meaning `remote` would be a
    multi-hour compile against upstream that nobody asked for."""
    assert run_sh('echo "CHOICE=$REPO_CHOICE"').stdout.strip().endswith("CHOICE=local")
    got = run_sh('echo "CHOICE=$REPO_CHOICE"', env={"PC_REPO_CHOICE": "remote"})
    assert "CHOICE=remote" in got.stdout


def test_portage_is_pointed_at_our_mirror_for_repo_binhost_and_distfiles():
    """All three, because they fail differently: no repo is a broken --sync, no binhost is a source
    build, no GENTOO_MIRRORS is every distfile pulled from upstream."""
    got = run_sh('TARGET=$(mktemp -d); mkdir -p "$TARGET/etc/portage"; '
                 ': >"$TARGET/etc/portage/make.conf"; gentooRepo >/dev/null; '
                 'cat "$TARGET"/etc/portage/repos.conf/* "$TARGET"/etc/portage/binrepos.conf/* '
                 '"$TARGET"/etc/portage/make.conf; rm -rf "$TARGET"')
    assert "sync-uri = https://gentoo.poster.place" in got.stdout
    assert "binpackages/23.0/x86-64/" in got.stdout
    assert 'GENTOO_MIRRORS="https://gentoo.poster.place"' in got.stdout


def test_the_wipe_question_can_actually_be_answered_yes():
    """The old prompt offered "local" as the default answer to a yes/no question, and `-i` is ignored
    without `-e`, so enter returned an empty string and the function fell out having said nothing."""
    body = ('prepareInstallDisk() { echo PREPARED; }; rm() { echo "RM $*"; }; '
            'HARD_DISK=zzz; initializeDisk >/dev/null; initializeDisk')
    assert "PREPARED" in run_sh(body, stdin="y\n" * 4).stdout


def test_an_unattended_wipe_needs_pc_assume_yes_and_nothing_else():
    """Wiping a disk is not something an argument alone may do, and an unanswered prompt must be a
    refusal rather than a default."""
    body = ('prepareInstallDisk() { echo PREPARED; }; rm() { :; }; HARD_DISK=zzz; '
            'initializeDisk; echo "RC=$?"')
    silent = run_sh(body, stdin="")
    assert "PREPARED" not in silent.stdout and "RC=1" in silent.stdout
    assert "PREPARED" in run_sh(body, env={"PC_ASSUME_YES": "1"}, stdin="").stdout


def test_a_scripted_install_keeps_the_disk_hook_the_menu_throws_away():
    """`rm -f /tmp/disk` is right for the menu, which re-detects the new partitions on its next
    pass, and fatal for a scripted install: the next phase reads that file and would otherwise stop
    at a prompt with nobody there."""
    body = ('prepareInstallDisk() { :; }; rm() { echo "RM $*"; }; HARD_DISK=zzz; '
            'PC_ASSUME_YES=1 initializeDisk')
    assert "RM -f /tmp/disk" in run_sh(body).stdout
    assert "RM -f /tmp/disk" not in run_sh(
        body, env={"PC_KEEP_DISK_HOOK": "1", "PC_ASSUME_YES": "1"}).stdout


def test_scratch_is_the_three_menu_phases_in_one_process():
    """One process on purpose: the LUKS password, the mapper name derived from the UUID luksFormat
    has just minted, and the /tmp/disk hook are all state that only survives inside one run."""
    body = ('setDevices() { echo PHASE-devices; }; initializeDisk() { echo "PHASE-init $PC_KEEP_DISK_HOOK"; }; '
            'download-setup() { echo PHASE-download; }; buildGentoo() { echo PHASE-build; }; '
            'scratchInstall')
    out = run_sh(body).stdout
    order = [line.split()[0] for line in out.splitlines() if line.startswith("PHASE-")]
    assert order == ["PHASE-devices", "PHASE-init", "PHASE-download", "PHASE-build"]
    assert "PHASE-init 1" in out


def test_a_failed_phase_stops_the_build_instead_of_installing_onto_nothing():
    """A refused wipe used to be invisible: the next phase mounted whatever was already on the disk
    and the build carried on against it."""
    body = ('setDevices() { :; }; initializeDisk() { return 1; }; '
            'download-setup() { echo PHASE-download; }; buildGentoo() { echo PHASE-build; }; '
            'scratchInstall; echo "RC=$?"')
    out = run_sh(body).stdout
    assert "PHASE-download" not in out and "PHASE-build" not in out and "RC=1" in out


def test_the_installer_copied_into_the_target_does_not_come_from_the_caller_cwd():
    """`cp -f gentoo.sh` copied whatever was named that where the operator stood — nothing at all
    from $HOME, a stale file from an old checkout. It is the copy the target repairs itself with."""
    assert "cp -f gentoo.sh $TARGET/usr/bin/" not in SH
    assert SH.count('cp -f "$INSTALLER_SRC" "$TARGET/usr/bin/gentoo.sh"') >= 3


def test_the_portage_tmpfs_is_sized_from_the_ram_that_is_there():
    """tmpfs accepts a size larger than memory without complaint, so a flat 32G was a fast build
    directory on the desktop and a promise a VM could not keep: the mount succeeds, emerge fills it,
    the kernel kills the compiler."""
    # The SHIPPED awk program, lifted out of the script and fed a made-up /proc/meminfo. A second
    # copy of the arithmetic in the test would agree with itself while the installer drifted.
    program = SH[SH.index('TMPFS_SIZE="$(awk \'') + len('TMPFS_SIZE="$(awk \''):]
    program = program[:program.index("}' /proc/meminfo") + 1]

    def size_for(kb):
        got = subprocess.run(["awk", program], input=f"MemTotal:       {kb} kB\n",
                             capture_output=True, text=True)
        return got.stdout.strip()

    assert size_for(8 * 1048576) == ""           # 8G VM: no tmpfs at all, build on the disk
    assert size_for(32 * 1048576) == "16G"       # half
    assert size_for(128 * 1048576) == "32G"      # capped at the historical value
    assert 'if [ -n "$TMPFS_SIZE" ]; then' in SH


def test_both_new_commands_are_reachable_from_the_command_line():
    """A phase you can name is not a nicety for an installer that takes hours; the menu is not a
    scripting interface."""
    dispatch = SH[SH.rindex('if [ "$1" = "services" ]'):]
    assert 'elif [ "$1" = "scratch" ]; then\n\tscratchInstall' in dispatch
    assert 'elif [ "$1" = "init-disk" ]; then' in dispatch
    assert "gentoo.sh scratch" in SH[SH.index("show-help() {"):SH.index("\ntweaks() {")]


def test_no_iso_and_no_mirror_is_a_skip_not_a_pass():
    """Exit 2 is "could not run" and the suite reports it as a SKIP. A gate that exits 0 having
    built nothing would be a green tick for an installer nobody ran."""
    got = subprocess.run([sys.executable, str(GATE), "--iso", "/nonexistent.iso"],
                         capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert got.returncode == 2
    assert "SKIP" in got.stdout


def test_the_ordinary_suite_does_not_start_an_eight_hour_build():
    """`./test.sh` DISCOVERS check_*.py. A gate that fetched its own ISO and began a source build
    would turn a ten-minute suite into an eight-hour one on any machine with /dev/kvm, the first
    time somebody ran it after this landed, with nothing on screen to say why. And the refusal must
    not depend on the host — it is asked before the tool and firmware checks, so every node in the
    fleet gives the same answer."""
    got = subprocess.run([sys.executable, str(GATE)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"})
    assert got.returncode == 2
    assert "not part of the ordinary suite" in got.stdout
    assert "--iso auto" in got.stdout, "a skip must say what would make it run"

    registry = (ROOT / "scripts/checkall.py").read_text(encoding="utf-8")
    assert '"check_scratch_install_vm"' in registry, \
        "an unregistered check still runs, with the default timeout — hours short of this one"
    entry = registry[registry.index('"check_scratch_install_vm"'):]
    assert "serial=True" in entry[:400], "it owns a KVM guest; it cannot share the box"


def test_the_guest_is_uefi_and_boots_the_medium_unmodified():
    """The installer writes an ESP and a systemd-boot entry, so a SeaBIOS guest would prove nothing
    about the bootloader; and the kernel comes out of the stock ISO rather than a repacked one, so
    what is tested is the medium a person would actually download."""
    args = MOD.qemu_args("/tmp/d.qcow2", "/tmp/x.iso", "/k", "/i", "root=live:CDLABEL=X",
                         "/tmp/s.sock", "/c.fd", "/v.fd", 4096, 4, 1234)
    assert "if=pflash,format=raw,unit=0,readonly=on,file=/c.fd" in args
    assert "-kernel" in args and "/k" in args
    assert "file=/tmp/x.iso,media=cdrom,readonly=on" in args


def test_the_console_carries_a_heartbeat_and_not_the_build_output():
    """115200 baud is 11 KB/s and a Gentoo build prints hundreds of megabytes; sending it down the
    serial line makes the LINE the bottleneck and a four-hour build a multi-day one."""
    # The launch line is an f-string, so it is read as source rather than as a constant: what
    # matters is that the redirection is IN it, and an f-string node has no single value to read.
    sends = [ast.get_source_segment(SRC, node.args[0])
             for node in ast.walk(ast.parse(SRC))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "send" and node.args]
    launch = next(v for v in sends if "gentoo.sh scratch" in v)
    assert ">/tmp/scratch.log 2>&1" in launch
    assert any("PCPROGRESS" in v for v in sends)


def test_a_run_that_never_finishes_still_yields_its_log(tmp_path):
    """The build log is the only evidence a multi-hour failure leaves. It comes back by PUT because
    the console cannot carry it — and it must be fetched on the timeout path too."""
    body = SRC[SRC.index("def install("):SRC.index("def upload_log(")]
    assert body.count("upload_log(con, port)") >= 2
    assert "did not finish within" in body


def test_the_iso_label_is_read_from_the_medium_not_its_filename(tmp_path):
    """`root=live:CDLABEL=...` must name the medium exactly or dracut drops to an emergency shell
    with no root filesystem. Deriving it from the file name worked only for releases whose name
    happened to encode the date."""
    iso = tmp_path / "renamed-by-somebody.iso"
    blob = bytearray(40000)
    blob[32768 + 40:32768 + 40 + 32] = b"Gentoo-amd64-20260830".ljust(32)
    iso.write_bytes(bytes(blob))
    assert MOD.iso_label(iso) == "Gentoo-amd64-20260830"


def test_a_full_build_without_kvm_is_a_skip_rather_than_a_week():
    assert "no /dev/kvm on this host" in SRC


def test_the_whole_os_tree_travels_not_just_the_script(tmp_path):
    """The script copies pc-* helpers out of bin/ and the boot theme out of plymouth/, and
    finalization REFUSES to report success without the theme — hours in."""
    import tarfile

    MOD.installer_tarball(tmp_path)
    with tarfile.open(tmp_path / "pcos.tar.gz") as tf:
        names = tf.getnames()
    assert "gentoo.sh" in names
    assert any(n.startswith("bin/") for n in names)
    assert any(n.startswith("plymouth/") for n in names)


def test_the_tool_check_cannot_read_its_own_echo_as_an_answer():
    """The console echoes back everything sent to it. A per-tool `echo NEED-$t` put the literal
    string "NEED-$t" on the transcript before a single tool had been looked at, and the first
    version read that as a missing tool named "$t;" — reporting a perfectly good medium as unusable
    about two minutes into an eight-hour run."""
    sends = [ast.get_source_segment(SRC, node.args[0])
             for node in ast.walk(ast.parse(SRC))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "send" and node.args]
    # The SHIPPED expression, evaluated with the module's own tool list: a hand-retyped copy would
    # be a different command from the one the gate sends.
    expr = next(v for v in sends if "PCTOOLS" in v)
    command = eval("(" + expr + ")",  # noqa: S307 - our own source, wrapped so it spans lines
                   {"LIVE_TOOLS": MOD.LIVE_TOOLS})
    answer = subprocess.run(["bash", "-c", command], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin"}).stdout
    transcript = "root@livecd ~ # " + command + "\r\n" + answer
    found = re.findall(r"PCTOOLS:\[([^\]]*)\]", transcript)
    assert len(found) == 2, f"the echoed command and its answer both carry the marker: {found}"
    assert found[-1].split() == [], f"read the echo instead of the answer: {found}"
    # And the gate waits for the ANSWER, never satisfied by the echo of its own question.
    assert re.search(r"PCTOOLS:\[[^\]]*\]\s*\r?\n", 'x echo "PCTOOLS:[$m]"\r\n') is None


def test_the_binary_host_is_not_thrown_away_by_a_global_32_bit_abi():
    """`ABI_X86="64 32"` in make.conf makes every multilib-capable package need a 32-bit build, and
    the binary host's packages are built without one — so portage refuses them (measured in a VM
    install: glib, gtk+ and libsecret all `ignored due to non matching USE`). getbinpkg is the only
    reason a from-scratch build finishes at all.

    It is not merely slow: forcing glib, gtk+ and pillow to be BUILT closes a cycle portage cannot
    break (docutils → pillow → harfbuzz → glib → docutils) and `emerge @world` aborts outright.
    Measured on one guest, same disk, one variable — `64 32` exits 1; `64` resolves to 188 binary
    packages and 16 source builds and exits 0."""
    # Comments stripped: this file explains the old value at length, and a plain substring search
    # would be satisfied by the explanation of the bug rather than by its absence.
    code = "\n".join(l for l in SH.splitlines() if not l.strip().startswith("#"))
    assert 'ABI_X86="64 32"' not in code
    assert "echo 'ABI_X86=\"64\"' >>$TARGET/etc/portage/make.conf" in code
    # Steam still gets 32-bit libraries, by USE dependency and autounmask — which only works if the
    # package phase still answers autounmask at all.
    assert "emerge -uDN $PACKAGES --autounmask-write" in SH
    # And the steam command must not put the line back on an installed machine.
    steam = code[code.index("installSteam() {"):code.index("\nlocale() {")]
    assert "ABI_X86" not in steam, "the steam command must not put the line back"


def test_git_exists_before_the_git_overlay_is_synced():
    """A stage3 has no git, so `emerge --sync` over a git-backed overlay answered `!!! Command not
    found: git` and left /var/db/repos/posterchan EMPTY. Nothing failed — and hours later the
    package set could not resolve `gui-apps/wlr-randr`, which lives in that overlay, and took every
    other package in the set down with it."""
    body = SH[SH.index("configurePortage() {"):SH.index("buildGentoo() {")]
    first_sync = body.index("emerge --sync gentoo")
    install_git = body.index("emerge --oneshot --noreplace dev-vcs/git")
    full_sync = body.index("/usr/bin/emerge --sync\n")
    assert first_sync < install_git < full_sync, \
        "sync the gentoo tree, install git out of it, then sync everything"
    # The overlay it is for actually declares itself, or portage warns and ignores it.
    assert "masters = gentoo" in (ROOT / "os/overlay/metadata/layout.conf").read_text()
    assert (ROOT / "os/overlay/profiles/repo_name").read_text().strip() == "posterchan"


def test_opening_an_existing_volume_asks_for_its_passphrase():
    """The placeholder at the top of this file is not the key any real disk was formatted with, so
    a repair command that reached `partitions` with it reported "No key available with this
    passphrase" about a perfectly good disk. `open` takes the answer once, with no confirmation —
    this unlocks an existing volume rather than creating one."""
    got = run_sh('readInstallPassword open && echo "PW=[$DISK_PASSWORD]"',
                 env={"PC_INSTALL_PASSWORD": "hunter2"})
    assert "PW=[hunter2]" in got.stdout
    # Interactively it asks once, not twice: a second prompt for an existing disk is a confirmation
    # of a secret the disk already knows.
    got = run_sh('readInstallPassword open && echo "PW=[$DISK_PASSWORD]"', stdin="hunter2\n")
    assert "PW=[hunter2]" in got.stdout
    mount = SH[SH.index('elif [ "$1" = "mount" ]') + 10:]
    mount = mount[:mount.index("elif [")]
    assert mount.index("setDevices") < mount.index("readInstallPassword") < mount.index("systemMounts")


def test_autounmask_gets_more_than_one_question():
    """It was asked exactly once — write, apply, emerge — which is enough for a keyword and not for a
    USE DEPENDENCY CHAIN. `steam-launcher` wants `mesa[abi_x86_32]`; accepting that reveals
    `libudev[abi_x86_32]`; accepting THAT reveals `systemd[abi_x86_32]`. Each layer is invisible
    until the one above it is answered, so one pass stops partway and the whole set fails."""
    body = SH[SH.index("installPackages() {"):SH.index("installFlatpaks() {")]
    assert "for pass in 1 2 3" in body, "autounmask still gets a single pass"
    # PRETEND passes. A real merge per pass, with its output silenced so three resolutions of a
    # 200-package set do not bury the log, makes the install SILENT for its longest phase —
    # measured, 15 minutes of a frozen log with qemu at 458% CPU, which reads as a hang.
    loop = body[body.index("for pass in 1 2 3"):body.index("# Visible, and the one that")]
    assert "emerge -p -uDN $PACKAGES" in loop, "a convergence pass must not merge anything"
    assert body.index("for pass in 1 2 3") < body.index("if /usr/bin/emerge -uDN $PACKAGES; then"), \
        "the passes must run before the emerge whose success is being judged"
    # And a pass that changed nothing ends it: three unconditional resolutions of a 200-package set
    # is minutes of nothing on every install that never needed them.
    assert "break" in body[body.index("for pass in 1 2 3"):]


def test_the_version_gate_is_not_defeated_by_its_own_missing_tool(tmp_path):
    """`cmp` is in sys-apps/diffutils, which Gentoo's minimal installcd trims — the medium a
    from-scratch install runs from. The shell printed `cmp: command not found`, `if !` read that
    non-zero status as "the files differ", and finalizeInstall refused a complete install with "The
    target did not receive this PosterChanOS installer version". A gate defeated by its own
    dependency fails good builds and names the wrong cause."""
    import os
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    a.write_text("same"); b.write_text("same"); c.write_text("different")

    def verdict(x, y, path):
        # PATH is narrowed INSIDE the script: gentoo.sh assigns its own PATH at the top, so an
        # environment set by the caller is gone by the time any of its functions run.
        got = run_sh(f'PATH="{path}"; _pc_same_file "{x}" "{y}"; echo "RC=$?"')
        return got.stdout.strip().splitlines()[-1]

    full = "/usr/bin:/bin"
    assert verdict(a, b, full) == "RC=0"
    assert verdict(a, c, full) == "RC=1"

    # The same answers with NO cmp reachable — a directory holding only the fallback hasher.
    nocmp = tmp_path / "bin"
    nocmp.mkdir()
    for tool in ("sha256sum", "cut"):
        src = shutil.which(tool)
        if src:
            os.symlink(src, nocmp / tool)
    assert shutil.which("cmp", path=str(nocmp)) is None, "the fixture still has cmp"
    assert verdict(a, b, nocmp) == "RC=0", "identical files read as different without cmp"
    assert verdict(a, c, nocmp) == "RC=1"

    # And with NOTHING to compare with, "could not tell" is its own answer — never "they differ".
    empty = tmp_path / "empty"
    empty.mkdir()
    assert verdict(a, b, empty) == "RC=2"
    gate = SH[SH.index('_pc_same_file "$INSTALLER_SRC"'):]
    assert "Could not compare" in gate[:900], "a gate that cannot check must say so, not accuse"
