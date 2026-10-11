"""ptop -- PosterChan top. The numbers it draws are measured, and every PosterChan install ships it.

The measurement is checked against a fake /proc + /sys with two known samples, so every figure on screen (per-core
CPU, per-interface bytes/s, iostat's r/s w/s rkB/s wkB/s r_await w_await %util) is compared with a value computed by
hand from the counters. The screen is then rendered from THIS machine's real /proc, and the four ways a node gets
its software (the PosterChanOS overlay ebuild, gentoo.sh, install.sh, the Docker image) must all install it.
"""
import importlib.machinery
import importlib.util
import os
import re
import stat
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PTOP = os.path.join(ROOT, "os", "overlay", "app-misc", "posterchanos-shell", "files", "ptop")


def _load(proc=None, sysfs=None):
    if proc:
        os.environ["PTOP_PROC"] = proc
    if sysfs:
        os.environ["PTOP_SYS"] = sysfs
    try:
        loader = importlib.machinery.SourceFileLoader("ptop_under_test", PTOP)
        spec = importlib.util.spec_from_loader("ptop_under_test", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod
    finally:
        os.environ.pop("PTOP_PROC", None)
        os.environ.pop("PTOP_SYS", None)


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _fake(root, *, cpu, net, disk):
    proc, sysfs = os.path.join(root, "proc"), os.path.join(root, "sys")
    # user nice system idle iowait irq softirq steal
    _w(proc + "/stat", "".join("%s %s\n" % (n, " ".join(str(v) for v in vals)) for n, vals in cpu))
    _w(proc + "/meminfo", "MemTotal: 1000 kB\nMemFree: 100 kB\nMemAvailable: 400 kB\nBuffers: 50 kB\n"
                          "Cached: 150 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    _w(proc + "/net/dev", "Inter-|   Receive\n face |bytes\n" + "".join(
        "%6s: %d 0 0 0 0 0 0 0 %d 0 0 0 0 0 0 0\n" % (n, rx, tx) for n, rx, tx in net))
    # major minor name reads merged sectors_read ms_read writes merged sectors_written ms_write inflight io_ms wio_ms
    _w(proc + "/diskstats", "".join("   8 0 %s %d 0 %d %d %d 0 %d %d 0 %d 0\n" % d for d in disk))
    _w(proc + "/loadavg", "0.50 0.40 0.30 1/100 1\n")
    _w(proc + "/uptime", "3700.0 1.0\n")
    for n, *_ in disk:
        os.makedirs(os.path.join(sysfs, "block", n), exist_ok=True)
    os.makedirs(os.path.join(sysfs, "block", "loop0"), exist_ok=True)
    for n, *_ in net:
        _w(os.path.join(sysfs, "class", "net", n, "operstate"), "up\n")
    return proc, sysfs


def test_every_number_is_the_difference_of_two_samples(tmp_path, monkeypatch):
    root = str(tmp_path)
    proc, sysfs = _fake(root,
                        cpu=[("cpu", [100, 0, 100, 800, 0, 0, 0, 0]), ("cpu0", [50, 0, 50, 400, 0, 0, 0, 0]),
                             ("cpu1", [50, 0, 50, 400, 0, 0, 0, 0])],
                        net=[("eth0", 1000, 2000), ("lo", 10, 10)],
                        disk=[("sda", 100, 1000, 50, 50, 4000, 500, 1000)])
    m = _load(proc, sysfs)
    clock = [1000.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    s = m.Sampler()
    assert s.sample() is None, "one reading is not a measurement"
    # two seconds later: eth0 +4000/+1000 bytes;
    # sda +20 reads taking 100 ms, +10 writes taking 300 ms, +2048/+4096 sectors, +1500 ms busy
    _fake(root,
          cpu=[("cpu", [200, 0, 150, 1050, 0, 0, 0, 0]), ("cpu0", [125, 0, 75, 450, 0, 0, 0, 0]),
               ("cpu1", [75, 0, 75, 600, 0, 0, 0, 0])],
          net=[("eth0", 5000, 3000), ("lo", 10, 10)],
          disk=[("sda", 120, 3048, 150, 60, 8096, 800, 2500)])
    clock[0] += 2.0
    snap = s.sample()
    assert snap["cpu"]["cpu0"] == pytest.approx(100.0 * 100 / 150)      # busy 100->200 of total 500->650
    assert snap["cpu"]["cpu1"] == pytest.approx(100.0 * 50 / 250)       # busy 100->150 of total 500->750
    assert snap["ncpu"] == 2
    assert snap["net"]["eth0"]["rx_Bps"] == pytest.approx(2000.0)
    assert snap["net"]["eth0"]["tx_Bps"] == pytest.approx(500.0)
    d = snap["disks"]["sda"]
    assert "loop0" not in snap["disks"]
    assert d["r_s"] == pytest.approx(10.0) and d["w_s"] == pytest.approx(5.0)
    assert d["rkB_s"] == pytest.approx(2048 * 512 / 1024 / 2)
    assert d["wkB_s"] == pytest.approx(4096 * 512 / 1024 / 2)
    assert d["r_await_ms"] == pytest.approx(100 / 20)                    # iostat's r_await: ms per read
    assert d["w_await_ms"] == pytest.approx(300 / 10)
    assert d["util_pct"] == pytest.approx(1500 / 2000 * 100)
    assert snap["mem"]["total"] == 1000 * 1024 and snap["mem"]["available"] == 400 * 1024


def test_the_screen_shows_every_core_interface_and_disk_of_this_machine():
    env = dict(os.environ, COLORTERM="truecolor", LANG="C.UTF-8")
    out = subprocess.run([sys.executable, PTOP, "--once", "--interval", "0.3", "--size", "160x60"],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out.stdout)
    for title in ("CPU", "MEMORY", "NETWORK", "DISK I/O", "PROCESSES", "r_await", "w_await"):
        assert title in plain, title
    ncpu = sum(1 for ln in open("/proc/stat") if re.match(r"cpu\d+ ", ln))
    shown = set(re.findall(r"\bC(\d+)\b", plain))
    assert shown == {str(i) for i in range(ncpu)}, "cores shown %s of %d" % (sorted(shown, key=int), ncpu)
    assert "\x1b[38;2;" in out.stdout, "no truecolor"
    assert "▀" in out.stdout or "▄" in out.stdout, "the logo is missing"
    import socket
    assert socket.gethostname() in plain


def test_ascii_mode_draws_no_unicode_and_needs_no_colour():
    out = subprocess.run([sys.executable, PTOP, "--once", "--ascii", "--colors", "none", "--interval", "0.3",
                          "--size", "100x40"], capture_output=True, text=True, timeout=60,
                         env=dict(os.environ, LANG="C"))
    assert out.returncode == 0, out.stderr[-2000:]
    assert all(ord(c) < 128 for c in out.stdout), "non-ASCII output in --ascii mode"
    assert "\x1b[" not in out.stdout


def test_json_is_the_measurement_the_screen_is_drawn_from():
    out = subprocess.run([sys.executable, PTOP, "--json", "--interval", "0.3"], capture_output=True, text=True,
                         timeout=60)
    import json
    snap = json.loads(out.stdout)
    for k in ("cpu", "mem", "net", "disks", "procs", "load", "uptime_s"):
        assert k in snap, k


def test_every_install_path_ships_it():
    assert os.stat(PTOP).st_mode & stat.S_IXUSR, "ptop is not executable"
    mode = subprocess.run(["git", "ls-files", "-s", PTOP], cwd=ROOT, capture_output=True, text=True).stdout
    assert mode.startswith("100755"), "ptop is not executable IN GIT (a clone would get 0644): %r" % mode
    eb = [f for f in os.listdir(os.path.join(ROOT, "os/overlay/app-misc/posterchanos-shell")) if f.endswith(".ebuild")]
    assert len(eb) == 1
    ebuild = open(os.path.join(ROOT, "os/overlay/app-misc/posterchanos-shell", eb[0])).read()
    loop = re.search(r"for helper in ([^;]*); do\s*\n\s*doexe", ebuild).group(1).split()
    assert "ptop" in loop, "the overlay package does not install ptop"
    g = open(os.path.join(ROOT, "os/gentoo.sh")).read()
    assert any("ptop" in m.group(1).split() for m in re.finditer(r"for helper in ([^;]*); do", g)), \
        "gentoo.sh does not install ptop on a new PosterChanOS"
    inst = open(os.path.join(ROOT, "install.sh")).read()
    main = inst[inst.index("main() {"):]
    assert main.index("install_ptop") < main.index("install_nostr_only"), "nostr-only installs skip ptop"
    assert 'source "$INSTALL_DIR/ptop.sh"' in inst
    df = open(os.path.join(ROOT, "Dockerfile")).read()
    assert re.search(r"ln -sfn /app/os/overlay/app-misc/posterchanos-shell/files/ptop /usr/local/bin/ptop", df)


def test_the_installer_function_links_the_checkout(tmp_path):
    """Run install_ptop for real (sudo and the target path stubbed) -- a symlink to the checkout, so deploys update it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = """
set -e
SCRIPT_DIR=%r
print_success(){ echo "OK $*"; }; print_warning(){ echo "WARN $*"; }
source %r
sed_dst=%r
install_ptop_dst() { :; }
eval "$(declare -f install_ptop | sed "s#/usr/local/bin/ptop#$sed_dst#")"
id() { echo 0; }
install_ptop
readlink "$sed_dst"
""" % (ROOT, os.path.join(ROOT, "scripts/install/ptop.sh"), str(bindir / "ptop"))
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == PTOP


def test_filesystems_are_the_real_ones_once_each(tmp_path, monkeypatch):
    proc = str(tmp_path / "proc")
    _w(proc + "/mounts", "\n".join([
        "/dev/mapper/luks-a / btrfs rw,subvol=/@ 0 0",
        "/dev/mapper/luks-a /home btrfs rw,subvol=/@home 0 0",          # same filesystem: one row, named '/'
        "/dev/mapper/luks-a /.snapshots btrfs rw 0 0",
        "/dev/nvme0n1p1 /boot vfat rw 0 0",
        "/dev/md0 /mnt/my\\040drive ext4 rw 0 0",                        # octal-escaped space
        "/dev/md0 /srv/bind ext4 rw 0 0",                                # a bind mount of it: still one row
        "nas:/export /mnt/nas nfs4 rw 0 0",
        "tmpfs /tmp tmpfs rw 0 0", "proc /proc proc rw 0 0", "sysfs /sys sysfs rw 0 0",
        "cgroup2 /sys/fs/cgroup cgroup2 rw 0 0", "/dev/loop0 /snap/core/1 squashfs ro 0 0",
        "devpts /dev/pts devpts rw 0 0", "overlay /var/lib/docker/x overlay rw 0 0",
        "/dev/sdz1 /empty ext4 rw 0 0",                                  # zero-size: skipped
    ]) + "\n")
    m = _load(proc, str(tmp_path / "sys"))

    class V:
        def __init__(self, blocks, bfree, bavail):
            self.f_blocks, self.f_bfree, self.f_bavail, self.f_frsize = blocks, bfree, bavail, 4096
    sizes = {"/empty": V(0, 0, 0)}
    monkeypatch.setattr(m.os, "statvfs", lambda p: sizes.get(p, V(1000, 400, 300)))
    fs = m.read_filesystems()
    assert [f["mount"] for f in fs] == ["/", "/boot", "/mnt/my drive", "/mnt/nas"], [f["mount"] for f in fs]
    root = fs[0]
    assert root["size"] == 1000 * 4096 and root["used"] == 600 * 4096 and root["avail"] == 300 * 4096
    # a container's root overlay IS its filesystem
    _w(proc + "/mounts", "overlay / overlay rw 0 0\nproc /proc proc rw 0 0\n")
    assert [f["mount"] for f in m.read_filesystems()] == ["/"]


def test_the_screen_lists_this_machines_filesystems_and_no_tmpfs():
    env = dict(os.environ, COLORTERM="truecolor", LANG="C.UTF-8")
    out = subprocess.run([sys.executable, PTOP, "--once", "--interval", "0.3", "--size", "160x70"],
                         capture_output=True, text=True, env=env, timeout=60)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out.stdout)
    assert "FILESYSTEMS" in plain
    real = []
    for line in open("/proc/mounts"):
        src, mnt, typ = line.split()[:3]
        if typ in ("ext4", "btrfs", "xfs", "vfat") and mnt in ("/", "/boot"):
            real.append(mnt)
    for mnt in real:
        assert re.search(r"[│|]%s\s" % re.escape(mnt), plain), "%s is missing from the screen" % mnt
    assert not re.search(r"\btmpfs\b", plain), "a tmpfs is listed"
    panel = plain[plain.index("FILESYSTEMS"):]
    rows = [ln for ln in panel.splitlines()[1:] if ln.strip().startswith(("│/", "|/"))]
    assert rows and all(re.search(r"\d+\.\d%\s*[│|]\s*$", r) for r in rows), "a usage figure is cut off: %r" % rows
