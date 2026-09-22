"""`./install.sh --vmhost` (scripts/install/vmhost.sh), RUN under bash with every system command stubbed.

The stubs keep STATE (groups joined, NOCOW flags, the default network, enabled units) in files, so a second run
sees what the first one did — which is what "idempotent" means, and what a grep of the script cannot show. The
two host facts that did not "just work" on the first live host are driven both ways: btrfs vs not (and an
already-populated btrfs directory), and libvirt with vs without polkit.
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REAL_TOOLS = ["bash", "sh", "ls", "cat", "grep", "sed", "awk", "tr", "mkdir", "chmod", "dirname", "tee", "rm", "env",
              "sort", "head", "touch", "true", "false", "cp"]

STUBS = {
    "sudo": 'if [ "$1" = "-u" ]; then echo "sudo-u $2 ${*:3}" >> "$STUB_LOG"; shift 2; fi; exec "$@"',
    "emerge": 'echo "emerge $*" >> "$STUB_LOG"; install_tools',
    "apt-get": 'echo "apt-get $*" >> "$STUB_LOG"; case " $* " in *" install "*) install_tools;; esac',
    "pacman": 'echo "pacman $*" >> "$STUB_LOG"; install_tools',
    "dnf": 'echo "dnf $*" >> "$STUB_LOG"; install_tools',
    "zypper": 'echo "zypper $*" >> "$STUB_LOG"; install_tools',
    "systemctl": '''echo "systemctl $*" >> "$STUB_LOG"
case "$1" in
  is-enabled) [ -f "$STUB_STATE/enabled-${@: -1}" ];;
  enable) touch "$STUB_STATE/enabled-${@: -1}";;
esac''',
    "getent": 'case " $STUB_GROUPS " in *" $2 "*) echo "$2:x:1:";; *) exit 2;; esac',
    "id": 'cat "$STUB_STATE/groups" 2>/dev/null | tr "\\n" " "; echo',
    "usermod": 'echo "usermod $*" >> "$STUB_LOG"; echo "$2" >> "$STUB_STATE/groups"',
    "chown": 'echo "chown $*" >> "$STUB_LOG"',
    "chattr": 'echo "chattr $*" >> "$STUB_LOG"; echo "$2" >> "$STUB_STATE/nocow"',
    "lsattr": ('if grep -qxF "$2" "$STUB_STATE/nocow" 2>/dev/null; then echo "---------------C------ $2"; '
               'else echo "---------------------- $2"; fi'),
    "stat": 'if [ "$1" = "-f" ]; then echo "$STUB_FSTYPE"; else exec "$REAL_STAT" "$@"; fi',
}
TOOL_STUBS = {
    "virsh": '''echo "virsh $*" >> "$STUB_LOG"
[ "$1" = "--version" ] && { echo 12.0.0; exit 0; }
shift 2
case "$1" in
  net-info) [ -f "$STUB_STATE/net" ] || exit 1
            echo "Name:           default"
            [ -f "$STUB_STATE/net-active" ] && echo "Active:         yes" || echo "Active:         no"
            [ -f "$STUB_STATE/net-auto" ] && echo "Autostart:      yes" || echo "Autostart:      no";;
  net-define) touch "$STUB_STATE/net";;
  net-autostart) touch "$STUB_STATE/net-auto";;
  net-start) touch "$STUB_STATE/net-active";;
  list) echo " Id   Name   State"; echo "--------------------"; exit "${STUB_LIST_RC:-0}";;
esac''',
    "qemu-img": 'exit 0',
}


class Host:
    def __init__(self, tmp: Path, *, distro="gentoo", fstype="btrfs", polkit=False, kvm=True, tools=False):
        self.tmp = tmp
        self.bin, self.tools, self.real, self.state = (tmp / d for d in ("bin", "tools", "real", "state"))
        for d in (self.bin, self.tools, self.real, self.state):
            d.mkdir(parents=True, exist_ok=True)
        for t in REAL_TOOLS:
            p = shutil.which(t)
            if p and not (self.real / t).exists():
                (self.real / t).symlink_to(p)
        self.templates = tmp / "templates"
        self.templates.mkdir(exist_ok=True)
        for name, body in TOOL_STUBS.items():
            (self.templates / name).write_text("#!/bin/bash\n" + body + "\n")
            (self.templates / name).chmod(0o755)
        install_tools = 'install_tools(){ cp -p "$STUB_TEMPLATES"/* "$STUB_TOOLS"/; }'
        for name, body in STUBS.items():
            p = self.bin / name
            p.write_text("#!/bin/bash\n" + install_tools + "\n" + body + "\n")
            p.chmod(0o755)
        if tools:
            for name in TOOL_STUBS:
                shutil.copy2(self.templates / name, self.tools / name)
        self.etc = tmp / "etc"
        (self.etc / "libvirt").mkdir(parents=True, exist_ok=True)
        conf = self.etc / "libvirt" / "libvirtd.conf"
        if not conf.exists():
            conf.write_text('# This is restricted to root by default.\n#unix_sock_group = "libvirt"\n'
                            '#unix_sock_ro_perms = "0777"\n#unix_sock_rw_perms = "0770"\n#auth_unix_rw = "polkit"\n')
        self.polkit = tmp / "polkit"
        self.polkit.mkdir(exist_ok=True)
        if polkit:
            (self.polkit / "org.libvirt.unix.policy").write_text("<policyconfig/>")
        self.kvm = tmp / "kvm"
        if kvm:
            self.kvm.write_text("")
        self.netxml = tmp / "default.xml"
        self.netxml.write_text("<network><name>default</name></network>")
        self.storage = tmp / "var" / "lib" / "posterchan" / "vms"
        self.images = tmp / "var" / "lib" / "libvirt" / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        self.log = tmp / "calls.log"
        self.distro, self.fstype = distro, fstype

    def run(self, **extra):
        self.log.write_text("")
        env = {
            "PATH": f"{self.bin}:{self.tools}:{self.real}", "HOME": str(self.tmp), "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state), "STUB_TOOLS": str(self.tools), "STUB_TEMPLATES": str(self.templates), "STUB_FSTYPE": self.fstype,
            "STUB_GROUPS": "libvirt kvm qemu", "REAL_STAT": shutil.which("stat"),
            "VMHOST_USER": "svc", "VMHOST_STORAGE": str(self.storage), "VMHOST_LIBVIRT_IMAGES": str(self.images),
            "VMHOST_ETC": str(self.etc), "VMHOST_POLKIT_ACTIONS": str(self.polkit), "VMHOST_KVM_DEV": str(self.kvm),
            "VMHOST_NETWORK_XML": str(self.netxml), **extra,
        }
        script = (f'set -e; source "{ROOT}/scripts/install/utils.sh"; source "{ROOT}/scripts/install/detect.sh"; '
                  f'source "{ROOT}/scripts/install/vmhost.sh"; DISTRO={self.distro}; setup_vmhost')
        p = subprocess.run([shutil.which("bash"), "-c", script], env=env, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr, self.log.read_text().splitlines()

    @property
    def dropin(self):
        return self.etc / "systemd" / "system" / "libvirtd.socket.d" / "posterchan-group.conf"

    def nocow(self):
        f = self.state / "nocow"
        return f.read_text().split() if f.exists() else []


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_a_fresh_gentoo_btrfs_host_without_polkit(tmp_path):
    h = Host(tmp_path)
    rc, out, calls = h.run()
    assert rc == 0, out
    emerge = [c for c in calls if c.startswith("emerge")]
    assert emerge == ["emerge --noreplace app-emulation/libvirt app-emulation/qemu"], emerge
    assert "edk2" not in " ".join(calls), "qemu pins its own edk2 — requesting it separately conflicts"
    assert "usermod -aG libvirt svc" in calls and "usermod -aG kvm svc" in calls
    # storage: created, NOCOW set while EMPTY (before isos/ existed), owned svc:qemu, 0751 / isos 0755
    assert h.storage.is_dir() and (h.storage / "isos").is_dir()
    assert mode(h.storage) == 0o751 and mode(h.storage / "isos") == 0o755
    assert f"chown svc:qemu {h.storage} {h.storage / 'isos'}" in calls
    assert h.nocow() == [str(h.storage), str(h.storage / "isos"), str(h.images)]
    assert calls.index(f"chattr +C {h.storage}") < calls.index(f"chattr +C {h.storage / 'isos'}")
    # the group socket
    assert h.dropin.read_text() == "[Socket]\nSocketGroup=libvirt\nSocketMode=0660\n"
    conf = (h.etc / "libvirt" / "libvirtd.conf").read_text()
    assert 'unix_sock_group = "libvirt"\n' in conf and 'unix_sock_rw_perms = "0770"\n' in conf
    assert '#unix_sock_ro_perms = "0777"' in conf and '#auth_unix_rw = "polkit"' in conf, "nothing else touched"
    assert conf.count("unix_sock_group") == 1
    assert "systemctl daemon-reload" in calls and "systemctl restart libvirtd.socket" in calls
    assert "systemctl enable --now libvirtd.socket" in calls
    # network defined, autostarted, started; KVM present; the final check ran AS the service user
    assert any("net-define" in c for c in calls) and any("net-autostart default" in c for c in calls) \
        and any("net-start default" in c for c in calls)
    assert "sudo-u svc virsh -c qemu:///system list --all" in calls
    assert "can drive qemu:///system" in out


def test_a_second_run_changes_nothing(tmp_path):
    h = Host(tmp_path)
    assert h.run()[0] == 0
    conf_before = (h.etc / "libvirt" / "libvirtd.conf").read_text()
    rc, out, calls = h.run()
    assert rc == 0, out
    changing = [c for c in calls if c.split()[0] in ("emerge", "usermod", "chattr")
                or any(v in c for v in ("net-define", "net-autostart", "net-start", "daemon-reload", "restart",
                                        "enable --now"))]
    assert changing == [], changing
    assert (h.etc / "libvirt" / "libvirtd.conf").read_text() == conf_before
    assert "already has NOCOW" in out and "already in the libvirt group" in out and "drop-in already in place" in out


def test_not_btrfs_means_no_chattr(tmp_path):
    h = Host(tmp_path, fstype="ext2/ext3", tools=True)
    rc, out, calls = h.run()
    assert rc == 0, out
    assert not any(c.startswith("chattr") for c in calls) and "no NOCOW needed" in out
    assert not any(c.startswith("emerge") for c in calls), "libvirt/QEMU already installed"


def test_a_populated_btrfs_directory_is_warned_about_not_chattred(tmp_path):
    h = Host(tmp_path, tools=True)
    h.storage.mkdir(parents=True)
    (h.storage / "old-disk.qcow2").write_bytes(b"x")
    (h.images / "legacy.qcow2").write_bytes(b"x")
    rc, out, calls = h.run()
    assert rc == 0, out
    assert f"chattr +C {h.storage}" not in calls and f"chattr +C {h.images}" not in calls
    assert out.count("already holds files") == 2
    assert f"chattr +C {h.storage / 'isos'}" in calls, "the new, empty isos/ still gets it"


def test_with_polkit_the_socket_and_config_are_left_alone(tmp_path):
    h = Host(tmp_path, polkit=True, tools=True)
    before = (h.etc / "libvirt" / "libvirtd.conf").read_text()
    rc, out, calls = h.run()
    assert rc == 0, out
    assert not h.dropin.exists() and (h.etc / "libvirt" / "libvirtd.conf").read_text() == before
    assert "systemctl daemon-reload" not in calls and "uses polkit" in out


def test_debian_packages_and_the_libvirt_qemu_group(tmp_path):
    h = Host(tmp_path, distro="debian", fstype="ext2/ext3", polkit=True)
    rc, out, calls = h.run(STUB_GROUPS="libvirt kvm libvirt-qemu")
    assert rc == 0, out
    install = [c for c in calls if c.startswith("apt-get") and " install " in f" {c} "]
    assert install and all(p in install[0] for p in ("libvirt-daemon-system", "qemu-system-x86", "qemu-utils", "ovmf"))
    assert f"chown svc:libvirt-qemu {h.storage} {h.storage / 'isos'}" in calls


def test_no_kvm_and_a_user_who_cannot_connect_yet_are_said_out_loud(tmp_path):
    h = Host(tmp_path, kvm=False, tools=True)
    rc, out, calls = h.run(STUB_LIST_RC="1")
    assert rc == 0, out
    assert "is missing: enable VT-x/AMD-V" in out
    assert "cannot reach qemu:///system yet" in out and "restart" in out


@pytest.mark.parametrize("flag", ["--vmhost"])
def test_install_sh_wires_the_flag_and_the_help(flag):
    text = (ROOT / "install.sh").read_text()
    assert 'source "$INSTALL_DIR/vmhost.sh"' in text
    assert f'if [ "$1" = "{flag}" ]; then\n    setup_vmhost\n    exit $?' in text
    assert flag in (ROOT / "scripts" / "install" / "utils.sh").read_text()


def test_with_docker_vm_traffic_is_let_through_its_forward_drop(tmp_path):
    """Measured on nas.lan: Docker's iptables FORWARD policy is DROP, so a VM on libvirt's NAT network
    reached its gateway and nothing else. The installer must leave a unit that re-applies the
    DOCKER-USER accepts on every boot — for libvirt's bridges and br0-3, NEVER br+ (Docker's br-<id>)."""
    h = Host(tmp_path)
    units = tmp_path / "units"; units.mkdir()
    docker = h.bin / "docker"; docker.write_text("#!/bin/sh\nexit 0\n"); docker.chmod(0o755)
    rc, out, calls = h.run(VMHOST_UNIT_DIR=str(units))
    assert rc == 0, out
    unit = (units / "posterchan-vm-forward.service").read_text()
    assert "DOCKER-USER -i \"$i\" -j ACCEPT" in unit and "--ctstate RELATED,ESTABLISHED" in unit
    assert "virbr+" in unit and "br0" in unit
    assert " br+ " not in unit and "br+;" not in unit, "br+ would also open Docker's own networks"
    assert "iptables -C" in unit, "re-running must not stack duplicate rules"
    assert any("systemctl enable --now posterchan-vm-forward.service" in c for c in calls)


def test_without_docker_no_forward_unit(tmp_path):
    h = Host(tmp_path)
    units = tmp_path / "units"; units.mkdir()
    rc, out, calls = h.run(VMHOST_UNIT_DIR=str(units))
    assert rc == 0, out
    assert not (units / "posterchan-vm-forward.service").exists()
    assert not any("posterchan-vm-forward" in c for c in calls)
