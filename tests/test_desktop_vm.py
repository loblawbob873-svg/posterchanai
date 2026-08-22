import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class VmBackend(unittest.TestCase):
    def node(self, source):
        return subprocess.run(["node", "-e", source], cwd=ROOT, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout.strip()

    def test_vm_names_are_safe_for_paths_and_virsh(self):
        got = self.node("const v=require('./desktop/vm'); console.log(JSON.stringify(["
                        "v.cleanName('Windows 11'),v.cleanName('../../bad; reboot'),v.cleanName('')]))")
        self.assertEqual(json.loads(got), ["Windows-11", "bad-reboot", ""])

    def test_renderer_never_gets_a_command_string(self):
        src = (ROOT / "desktop" / "preload.js").read_text()
        self.assertIn("pc:vm:action", src)
        self.assertIn("String(action||'')", src)

    def test_fresh_os_installs_only_the_small_viewer(self):
        installer = (ROOT / "os" / "gentoo.sh").read_text()
        self.assertIn("app-emulation/libvirt", installer)
        self.assertIn("app-emulation/qemu", installer)
        self.assertIn("app-emulation/virt-viewer", installer)
        self.assertIn("app-crypt/swtpm", installer)
        self.assertIn("media-plugins/gst-plugins-pulse", installer)
        self.assertIn('app-emulation/qemu spice usbredir', installer)
        self.assertIn('app-emulation/virt-viewer spice', installer)
        self.assertNotIn("app-emulation/virt-manager", installer)

    def test_each_os_user_gets_a_libvirt_session(self):
        src = (ROOT / "desktop" / "vm.js").read_text()
        self.assertIn("qemu:///session", src)
        self.assertNotIn("qemu:///system", src)
        provision = (ROOT / "os" / "bin" / "pc-provision-user").read_text()
        self.assertIn("render kvm posterchan", provision)

    def test_windows_preset_has_uefi_tpm_and_spice_audio(self):
        src = (ROOT / "desktop" / "vm.js").read_text()
        self.assertIn('firmware="efi"', src)
        self.assertIn('name="secure-boot"', src)
        self.assertIn('version="2.0"', src)
        self.assertIn('type="spice"', src)

    def test_viewer_attaches_through_libvirt(self):
        src = (ROOT / "desktop" / "vm.js").read_text()
        self.assertIn("['--connect',URI,'--attach','--wait',name]", src)

    def test_viewer_cannot_pin_itself_over_the_desktop(self):
        sway = (ROOT / "os" / "overlay" / "app-misc" / "posterchanos-shell" / "files" / "sway.config").read_text()
        self.assertIn('[app_id="virt-viewer"] fullscreen disable, sticky disable', sway)
        self.assertIn('[class="Virt-viewer"] fullscreen disable, sticky disable', sway)

    def test_new_vm_uses_the_shared_plus_icon(self):
        src = (ROOT / "static/js/client/os.js").read_text()
        self.assertIn('data-vm-new><svg class="ic b-ic"', src)
        self.assertIn('<use href="#i-plus"></use></svg>New VM', src)


if __name__ == "__main__":
    unittest.main()
