"""Removable drives for Files on PosterChanOS (desktop/drives.js).

"important, we need to make sure files can see/mount/read/write removeable drives like USB". Runs the
shipped module under node with recorded lsblk output and a recording udisksctl.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LSBLK = {"blockdevices": [
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk", "rm": False, "hotplug": False, "tran": "nvme",
     "size": 512110190592, "fstype": None, "mountpoints": [None], "children": [
        {"name": "nvme0n1p2", "path": "/dev/nvme0n1p2", "type": "part", "rm": False, "hotplug": False,
         "size": 500000000000, "fstype": "ext4", "label": "root", "mountpoints": ["/"]}]},
    {"name": "sdb", "path": "/dev/sdb", "type": "disk", "rm": True, "hotplug": True, "tran": "usb",
     "size": 32015679488, "fstype": None, "vendor": "SanDisk ", "model": "Ultra", "mountpoints": [None], "children": [
        {"name": "sdb1", "path": "/dev/sdb1", "type": "part", "rm": True, "hotplug": True,
         "size": 32014630912, "fstype": "vfat", "label": "STICK", "mountpoints": [None]},
        {"name": "sdb2", "path": "/dev/sdb2", "type": "part", "rm": True, "hotplug": True,
         "size": 1048576, "fstype": "swap", "label": None, "mountpoints": [None]}]},
    {"name": "sdc", "path": "/dev/sdc", "type": "disk", "rm": False, "hotplug": True, "tran": "usb",
     "size": 1000204886016, "fstype": "exfat", "label": "Backup", "mountpoints": ["/run/media/alice/Backup"]},
]}

SCRIPT = r"""
const { createDrives } = require(%(mod)s);
const calls = [];
let lsblk = %(lsblk)s, failWith = null;
const run = async (cmd, args) => {
  calls.push([cmd, ...args]);
  if (cmd === 'lsblk') return JSON.stringify(lsblk);
  if (failWith) { const e = new Error(failWith.msg); if (failWith.code) e.code = failWith.code; e.stderr = failWith.stderr || ''; throw e; }
  if (cmd === 'udisksctl' && args[0] === 'mount') {
    lsblk.blockdevices[1].children[0].mountpoints = ['/run/media/alice/STICK'];
    return 'Mounted /dev/sdb1 at /run/media/alice/STICK\n';
  }
  return '';
};
const d = createDrives({ run });
(async () => {
  const out = {};
  out.list = await d.list();
  calls.length = 0;
  out.mount = await d.mount('/dev/sdb1');
  out.mountCalls = calls.filter(c => c[0] === 'udisksctl');
  calls.length = 0;
  try { await d.mount('/dev/nvme0n1p2'); out.internal = 'mounted'; } catch (e) { out.internal = e.message; }
  out.internalCalls = calls.filter(c => c[0] === 'udisksctl').length;
  calls.length = 0;
  out.eject = await d.eject('/dev/sdb1');
  out.ejectCalls = calls.filter(c => c[0] === 'udisksctl');
  failWith = { msg: 'spawn udisksctl ENOENT', code: 'ENOENT' };
  lsblk.blockdevices[1].children[0].mountpoints = [null];
  try { await d.mount('/dev/sdb1'); } catch (e) { out.missing = e.message; }
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def _run():
    script = SCRIPT % {"mod": json.dumps(str(ROOT / "desktop/drives.js")), "lsblk": json.dumps(LSBLK)}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_usb_drives_are_listed_and_internal_disks_swap_never_are():
    r = _run()
    got = {d["dev"]: d for d in r["list"]}
    assert set(got) == {"/dev/sdb1", "/dev/sdc"}, got.keys()
    assert got["/dev/sdb1"]["label"] == "STICK" and got["/dev/sdb1"]["mountpoint"] == ""
    assert got["/dev/sdc"]["mountpoint"] == "/run/media/alice/Backup" and got["/dev/sdc"]["fstype"] == "exfat"


def test_a_stick_mounts_as_the_user_and_files_learns_where():
    r = _run()
    assert r["mount"]["path"] == "/run/media/alice/STICK", r["mount"]
    assert r["mountCalls"] == [["udisksctl", "mount", "-b", "/dev/sdb1", "--no-user-interaction"]]


def test_an_internal_disk_is_never_mounted_by_name():
    r = _run()
    assert r["internal"] == "not a removable drive" and r["internalCalls"] == 0, r


def test_eject_unmounts_then_powers_the_stick_off():
    r = _run()
    assert r["ejectCalls"] == [["udisksctl", "unmount", "-b", "/dev/sdb1", "--no-user-interaction"],
                               ["udisksctl", "power-off", "-b", "/dev/sdb", "--no-user-interaction"]], r["ejectCalls"]


def test_a_machine_without_udisks_is_told_how_to_fix_it():
    r = _run()
    assert "not installed" in r["missing"] and "update" in r["missing"], r["missing"]


def test_the_os_installs_udisks_and_lets_the_user_mount_removable_media():
    eb = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0-r5.ebuild").read_text()
    assert "sys-fs/udisks:2" in eb and "sys-fs/exfatprogs" in eb
    assert "50-posterchan-removable-drives.rules" in eb
    rule = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/50-posterchan-removable-drives.rules").read_text()
    assert "org.freedesktop.udisks2.filesystem-mount\"" in rule
    assert "filesystem-mount-system" not in rule.split("polkit.addRule", 1)[1], "internal disks must still need an admin"
