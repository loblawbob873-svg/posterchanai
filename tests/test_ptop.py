"""ptop -- PosterChan top. The numbers it draws are measured, and every PosterChan install ships it.

The measurement is checked against a fake /proc + /sys with two known samples, so every figure on screen (per-core
CPU, per-interface bytes/s, iostat's r/s w/s rkB/s wkB/s r_await w_await %util) is compared with a value computed by
hand from the counters. The screen is then rendered from THIS machine's real /proc, and the four ways a node gets
its software (the PosterChanOS overlay ebuild, gentoo.sh, install.sh, the Docker image) must all install it.
"""
import importlib.machinery
import importlib.util
import os
import random
import re
import stat
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PTOP = os.path.join(ROOT, "os", "overlay", "app-misc", "posterchanos-shell", "files", "ptop")


def _term_env(**extra):
    """The caller's environment with the TERMINAL pinned. ptop rightly drops to 256 colours inside GNU screen
    (STY) or a screen TERM, and a gate run from an SSH session in `screen -xRR` -- how sync.sh is run on
    server1 -- inherited STY and failed "no truecolor" against a correct ptop."""
    env = {k: v for k, v in os.environ.items() if k not in ("STY", "TMUX", "NO_COLOR")}
    env.setdefault("TERM", "xterm-256color")
    if env["TERM"].startswith("screen"):
        env["TERM"] = "xterm-256color"
    env.update(extra)
    return env


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
    env = _term_env(COLORTERM="truecolor", LANG="C.UTF-8")
    out = subprocess.run([sys.executable, PTOP, "--once", "--interval", "0.3", "--size", "160x60"],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out.stdout)
    for title in ("CPU", "MEMORY", "NETWORK", "DISK I/O", "PROCESSES", "await", "aqu-sz", "IOWAIT", "iowait "):
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
                         env=_term_env(LANG="C"))
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
    env = _term_env(COLORTERM="truecolor", LANG="C.UTF-8")
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
    rows = [m.group(0) for m in re.finditer(r"[│|]/\S*\s+(?:btrfs|ext4|xfs|vfat|zfs|f2fs|nfs4?|exfat)\b[^\n]*", panel)]
    assert rows and all(re.search(r"\d+\.\d%\s*[│|]\s*$", r) for r in rows), "a usage figure is cut off: %r" % rows


def test_iowait_await_and_queue_depth_are_iostats(tmp_path, monkeypatch):
    """%iowait (iostat avg-cpu), await (ms per I/O, reads+writes) and aqu-sz (average queue depth), from the counters."""
    root = str(tmp_path)
    proc, sysfs = _fake(root, cpu=[("cpu", [100, 0, 100, 700, 100, 0, 0, 0])], net=[],
                        disk=[("sdd", 100, 1000, 50, 50, 4000, 500, 1000)])
    _w(proc + "/diskstats", "   8 0 sdd 100 0 4000 1000 50 0 500 50 0 1000 3000\n")
    m = _load(proc, sysfs)
    clock = [10.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    s = m.Sampler()
    s.sample()
    # 2 s later: iowait +60 of +300 ticks; sdd +40 reads in 2000 ms, +10 writes in 1000 ms, weighted io +16000 ms
    _w(proc + "/stat", "cpu 150 0 150 840 160 0 0 0\n")
    _w(proc + "/diskstats", "   8 0 sdd 140 0 4400 3000 60 0 600 1050 0 2900 19000\n")
    clock[0] += 2.0
    snap = s.sample()
    assert snap["iowait"]["cpu"] == pytest.approx(100.0 * 60 / 300)
    d = snap["disks"]["sdd"]
    assert d["await_ms"] == pytest.approx(3000 / 50)          # (2000 + 1000) ms over 40 + 10 I/Os
    assert d["r_await_ms"] == pytest.approx(2000 / 40) and d["w_await_ms"] == pytest.approx(1000 / 10)
    assert d["aqu_sz"] == pytest.approx(16000 / 2000)          # weighted ms / interval ms
    assert d["util_pct"] == pytest.approx(1900 / 2000 * 100)


def test_slash_searches_processes_interactively():
    """Press `/`, type, Enter keeps the filter, Esc clears it -- driven through a real pty, like a person at btop."""
    import fcntl
    import pty
    import select
    import struct
    import termios
    import time
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(sys.executable, [sys.executable, PTOP, "--interval", "0.5"],
                   _term_env(TERM="xterm-256color", COLORTERM="truecolor", LANG="C.UTF-8"))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))

    def screen(seconds):
        buf, end = b"", time.time() + seconds
        while time.time() < end:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    buf += os.read(fd, 65536)
                except OSError:
                    break
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", buf.decode("utf-8", "replace"))

    try:
        assert "PROCESSES" in screen(3)
        os.write(fd, b"/")
        os.write(fd, ("python").encode())
        typed = screen(1.5)
        assert "/python_" in typed, "the query being typed is not shown"
        os.write(fd, b"\r")
        kept = screen(1.5)
        assert re.search(r"/python  \(\d+ match", kept), "Enter did not keep the filter"
        os.write(fd, b"\x1b")
        cleared = screen(1.5)
        assert "/python" not in cleared.split("PROCESSES")[-1][:60], "Esc did not clear the filter"
        os.write(fd, b"q")
        # Keep READING while waiting: a terminal always drains, and a test that stops reading leaves ptop blocked
        # writing its next frame into a full pty -- it never gets to the `q` (that hung a run for 15 minutes).
        end = time.time() + 10
        status = None
        while time.time() < end:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    os.read(fd, 65536)
                except OSError:
                    pass
            done, st = os.waitpid(pid, os.WNOHANG)
            if done:
                status = st
                break
        assert status is not None, "q did not quit ptop within 10 s"
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def test_the_filter_matches_name_command_or_pid():
    m = _load()
    p = {"pid": 4005680, "name": "tor", "cmd": "/usr/bin/tor -f /var/lib/posterchanai/tor2/torrc"}
    assert m.proc_matches(p, "TOR") and m.proc_matches(p, "tor2/torrc") and m.proc_matches(p, "4005")
    assert not m.proc_matches(p, "5680") and not m.proc_matches(p, "nginx")
    assert m.proc_matches(p, "") and m.proc_matches(p, "  ")


MDSTAT = """Personalities : [raid1] [raid5] [raid6]
md0 : active raid5 sdd1[3] sdc1[1] sdb1[0]
      23437502720 blocks super 1.2 level 5, 64k chunk, algorithm 2 [3/3] [UUU]
      bitmap: 52/88 pages [208KB], 65536KB chunk

md1 : active raid1 sdf1[2](F) sde1[0] sdg1[3](S)
      976630464 blocks super 1.2 [2/1] [U_]
      [=====>...............]  recovery = 27.5% (268700000/976630464) finish=58.3min speed=202345K/sec

md2 : active raid6 sdh[0] sdi[1] sdj[2] sdk[3]
      2000000000 blocks super 1.2 level 6, 512k chunk, algorithm 2 [4/4] [UUUU]
        resync=DELAYED

unused devices: <none>
"""


def test_mdstat_health_failures_and_rebuild_progress(tmp_path):
    proc = str(tmp_path / "proc")
    _w(proc + "/mdstat", MDSTAT)
    m = _load(proc, str(tmp_path / "sys"))
    a = {r["name"]: r for r in m.read_mdstat()}
    assert a["md0"]["level"] == "raid5" and a["md0"]["health"] == "UUU" and (a["md0"]["total"], a["md0"]["up"]) == (3, 3)
    assert a["md0"]["members"] == ["sdd1", "sdc1", "sdb1"] and not a["md0"]["failed"] and not a["md0"]["action"]
    d = a["md1"]
    assert (d["total"], d["up"], d["health"]) == (2, 1, "U_")
    assert d["failed"] == ["sdf1"] and d["spares"] == ["sdg1"] and d["members"] == ["sde1"]
    assert d["action"] == "recovery" and d["progress"] == pytest.approx(27.5)
    assert d["finish"] == "58.3min" and d["speed"] == "202345K/sec"
    assert a["md2"]["action"] == "resync pending" and a["md2"]["health"] == "UUUU"


def test_the_raid_panel_says_degraded_and_shows_the_rebuild(tmp_path, monkeypatch):
    proc = str(tmp_path / "proc")
    _w(proc + "/mdstat", MDSTAT)
    m = _load(proc, str(tmp_path / "sys"))
    raid = m.read_mdstat()
    m2 = _load()
    snap = json_snap = None
    out = subprocess.run([sys.executable, PTOP, "--json", "--interval", "0.3"], capture_output=True, text=True, timeout=60)
    import json
    snap = json.loads(out.stdout)
    snap["raid"] = raid
    lines = m2.render(snap, m2.Sampler(), 160, 70, m2.Paint("none"), False)
    plain = "\n".join(lines)
    assert "RAID" in plain
    assert re.search(r"md0\s+raid5\s+\[3/3\] \[UUU\]\s+healthy", plain)
    assert re.search(r"md1\s+raid1\s+\[2/1\] \[U_\]\s+DEGRADED", plain) and "failed: sdf1" in plain
    assert re.search(r"recovery.*27\.5%.*eta 58\.3min", plain)
    assert "resync pending" in plain


def test_the_header_names_the_os_kernel_and_counts():
    env = _term_env(COLORTERM="truecolor", LANG="C.UTF-8")
    out = subprocess.run([sys.executable, PTOP, "--once", "--interval", "0.3", "--size", "160x60"],
                         capture_output=True, text=True, env=env, timeout=60)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out.stdout)
    assert "kernel " + os.uname().release in plain
    assert re.search(r"procs \d+\s+threads \d+", plain) and "booted " in plain


def test_the_header_is_decided_by_the_terminal_never_by_the_machine():
    """At the same size every node draws the same header. The portrait used to give way when THIS node's panels left
    under 8 process rows, so at 160x55 server1 showed PosterChan and nas.lan did not (2026-10-10). Two machines are
    simulated by giving the same render very different data (2 vs 14 disks, 1 vs 8 interfaces, RAID or none)."""
    import json
    m = _load()
    out = subprocess.run([sys.executable, PTOP, "--json", "--interval", "0.3"], capture_output=True, text=True,
                         timeout=60)
    base = json.loads(out.stdout)
    small = dict(base, disks=dict(list(base["disks"].items())[:2]), raid=[],
                 net={k: v for k, v in list(base["net"].items())[:1]})
    disk = {"r_s": 1.0, "w_s": 1.0, "rkB_s": 1.0, "wkB_s": 1.0, "r_await_ms": 1.0, "w_await_ms": 1.0,
            "await_ms": 1.0, "aqu_sz": 0.1, "util_pct": 1.0}
    iface = {"rx_Bps": 1.0, "tx_Bps": 1.0, "rx_total": 1, "tx_total": 1, "up": True}
    big = dict(base, disks={"sd%s" % chr(97 + i): dict(disk) for i in range(14)},
               net={"eth%d" % i: dict(iface) for i in range(8)},
               raid=[{"name": "md0", "state": "active", "level": "raid5", "members": ["sda1", "sdb1", "sdc1"],
                      "failed": [], "spares": [], "total": 3, "up": 3, "health": "UUU", "action": "",
                      "progress": None, "speed": "", "finish": ""}])
    for cols, rows in ((120, 40), (160, 50), (160, 55), (200, 70)):
        a = m.render(small, m.Sampler(), cols, rows, m.Paint("truecolor"), False)
        b = m.render(big, m.Sampler(), cols, rows, m.Paint("truecolor"), False)
        portrait = lambda lines: any("\x1b[48;2;" in ln for ln in lines[:20])
        assert portrait(a) == portrait(b), "%dx%d: one node has the portrait, the other not" % (cols, rows)
        assert portrait(a) == (rows >= 48 and cols >= 100)
        for lines in (a, b):
            plain = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(lines))
            assert "FILESYSTEMS" in plain and "PROCESSES" in plain, (cols, rows)
            assert len(lines) <= rows


# ---- what it looks like over SSH -----------------------------------------------------------------------------
# "server1's ptop don't even have a posterchan logo, its like a sphere and all the colors are jacked" (2026-10-11).
# ssh does not forward COLORTERM, so every node opened over SSH fell back to 256 colours; the fallback rounded each
# channel onto a LINEAR 0..5 scale while the xterm cube's levels are 0,95,135,175,215,255 (and ignored the grey
# ramp), so skin and hair became speckle and the portrait -- a face close-up in a ring -- read as a ball.

_CUBE = (0, 95, 135, 175, 215, 255)


def _xterm_rgb(idx):
    if idx >= 232:
        v = 8 + 10 * (idx - 232)
        return (v, v, v)
    i = idx - 16
    return (_CUBE[i // 36], _CUBE[(i // 6) % 6], _CUBE[i % 6])


def _logo_pixels(m):
    for row in m.LOGO:
        for x in range(0, len(row), 6):
            t = row[x:x + 6]
            if t != "......":
                yield tuple(int(t[i:i + 2], 16) for i in (0, 2, 4))


def test_over_ssh_without_colorterm_a_modern_terminal_still_gets_truecolor(monkeypatch):
    m = _load()
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("STY", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    for term in ("xterm-256color", "xterm", "tmux-256color", "alacritty", "xterm-kitty"):
        monkeypatch.setenv("TERM", term)
        assert m._color_mode(None) == "truecolor", term
    for term in ("linux", "vt100", "screen"):
        monkeypatch.setenv("TERM", term)
        assert m._color_mode(None) == "256", term
    monkeypatch.setenv("TERM", "xterm-256color")
    assert m._color_mode("256") == "256"                      # --colors still decides


# "why is ptop still look like ass when I ssh into server1 ... it looks like a totally different app" (2026-10-16).
# The PosterChanOS terminal runs an SSH session inside `screen -xRR` on a host without tmux, and GNU screen 4.x turns
# every 24-bit colour into one of its 8 basic colours. Measured through a real screen: truecolor in -> 0 colour codes
# out of 100+ basic ones; 256 in -> 357 256-colour codes out, none crushed.

def test_inside_gnu_screen_it_uses_the_colours_screen_can_pass(monkeypatch):
    m = _load()
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")              # what a .screenrc setenv / the outer terminal hands in
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("STY", "90067.pts-0.server1")
    assert m._color_mode(None) == "256"
    monkeypatch.delenv("STY")
    for term in ("screen", "screen-256color", "screen.xterm-256color"):
        monkeypatch.setenv("TERM", term)
        assert m._color_mode(None) == "256", term
    monkeypatch.setenv("TERM", "tmux-256color")               # tmux passes truecolor: left alone
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    monkeypatch.setenv("STY", "1.x")                          # tmux started from inside screen: tmux is what draws
    assert m._color_mode(None) == "truecolor"
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("STY", "1.x")
    monkeypatch.delenv("TMUX")
    assert m._color_mode("truecolor") == "truecolor"          # --colors still decides


def test_through_a_real_gnu_screen_the_colours_arrive():
    """Run ptop in a real screen session in a pty and read what screen sends to the terminal."""
    import shutil
    if not shutil.which("screen"):
        pytest.skip("GNU screen is not installed here")
    import pty, select, time, fcntl, termios, struct
    name = "ptoptest%d" % os.getpid()
    pid, fd = pty.fork()
    if pid == 0:
        env = {k: v for k, v in os.environ.items() if k not in ("STY", "TMUX", "SCREENDIR")}
        env.update(TERM="xterm-256color", LANG="C.UTF-8", COLORTERM="truecolor", HOME=os.environ.get("HOME", "/tmp"))
        os.execvpe("screen", ["screen", "-q", "-c", "/dev/null", "-S", name, sys.executable, PTOP, "--once",
                              "--size", "120x40"], env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    buf, end = b"", time.time() + 20
    try:
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                try:
                    d = os.read(fd, 65536)
                except OSError:
                    break
                if not d:
                    break
                buf += d
    finally:
        subprocess.run(["screen", "-S", name, "-X", "quit"], capture_output=True)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(fd)
    s = buf.decode("utf-8", "replace")
    assert "CPU" in s, s[-500:]
    rich = len(re.findall(r"\x1b\[[0-9;]*38;5;\d+", s))
    crushed = len(re.findall(r"\x1b\[3[0-7]m", s))
    assert rich > 100 and crushed < rich // 10, (rich, crushed)


def test_the_256_colour_fallback_picks_the_nearest_xterm_colour():
    m = _load()
    p = m.Paint("256")
    # NEAREST, exactly: no other of the 240 colours is closer, for the portrait's pixels and for any colour
    # (a mean-error threshold measured the picture, and a new drawing tipped it without the picker changing).
    pal = {i: _xterm_rgb(i) for i in range(16, 256)}
    err = lambda a, b: sum((x - y) ** 2 for x, y in zip(a, b))
    rnd = random.Random(1)
    colours = list(_logo_pixels(m)) + [tuple(rnd.randrange(256) for _ in range(3)) for _ in range(3000)]
    worse = []
    for rgb in colours:
        idx = int(re.search(r"\[38;5;(\d+)m", p._code(rgb)).group(1))
        best = min(err(rgb, c) for c in pal.values())
        if err(rgb, _xterm_rgb(idx)) > best:
            worse.append(rgb)
    assert not worse, "%d colours were not given their nearest xterm colour, e.g. %s" % (len(worse), worse[:5])
    # and greys use the grey ramp, not a tinted cube colour
    idx = int(re.search(r"\[38;5;(\d+)m", p._code((128, 128, 128))).group(1))
    assert _xterm_rgb(idx) == (128, 128, 128)


def test_the_portrait_is_posterchan_from_the_app_icon_with_no_box_around_her():
    """The portrait is PosterChan as the app icon draws her -- cat-ear hood, orange hair, black hoodie -- rebuilt by
    scripts/make_ptop_logo.py, transparent outside her outline so no square or ring is painted around her."""
    pytest.importorskip("PIL")
    m = _load()
    loader = importlib.machinery.SourceFileLoader("make_ptop_logo", os.path.join(ROOT, "scripts", "make_ptop_logo.py"))
    spec = importlib.util.spec_from_loader("make_ptop_logo", loader)
    gen = importlib.util.module_from_spec(spec)
    loader.exec_module(gen)
    assert tuple(gen.build()) == tuple(m.LOGO), "LOGO is not the app icon's PosterChan -- rerun scripts/make_ptop_logo.py"
    for row in (m.LOGO[0], m.LOGO[-1]):
        assert row.startswith("......") and row.endswith("......")
    px = list(_logo_pixels(m))
    orange = sum(1 for r, g, b in px if r > 180 and 70 < g < 170 and b < 90)
    dark = sum(1 for r, g, b in px if max(r, g, b) < 60)
    assert orange > len(px) * 0.07 and dark > len(px) * 0.2, (orange, dark, len(px))
