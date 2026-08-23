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
        self.assertIn("app-emulation/spice-vdagent", installer)
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
        group_line = next(x for x in provision.splitlines() if x.startswith("for g in "))
        for group in ("render", "kvm", "posterchan"):
            self.assertIn(group, group_line)

    def test_windows_preset_has_uefi_tpm_and_spice_audio(self):
        src = (ROOT / "desktop" / "vm.js").read_text()
        self.assertIn('firmware="efi"', src)
        self.assertIn('name="secure-boot"', src)
        self.assertIn('version="2.0"', src)
        self.assertIn('type="spice"', src)

    def test_virtual_gpu_can_render_a_wayland_desktop(self):
        src = (ROOT / "desktop" / "vm.js").read_text(encoding="utf-8")
        self.assertIn('<gl enable="yes"/>', src)
        self.assertIn('<acceleration accel3d="yes"/>', src)

    def test_new_vm_network_works_without_guest_virtio_drivers(self):
        src = (ROOT / "desktop" / "vm.js").read_text()
        self.assertIn('<interface type="user"><model type="e1000e"/></interface>', src)
        self.assertIn("domain-network.xml", src)
        self.assertNotIn("attach-interface',d.name,'user'", src)

    def test_viewer_attaches_through_libvirt(self):
        src = (ROOT / "desktop" / "vm.js").read_text()
        self.assertIn("'--connect',URI,'--attach','--wait',name", src)

    def test_viewer_pointer_and_framebuffer_use_the_same_scale(self):
        src = (ROOT / "desktop" / "vm.js").read_text()
        self.assertIn("['--auto-resize=always','--cursor=local']", src)
        self.assertIn("args.push(...viewerArgs,d.out.trim())", src)

    def test_guest_agent_is_started_for_spice_virtual_machines(self):
        installer = (ROOT / "os" / "gentoo.sh").read_text()
        launcher = (ROOT / "os" / "bin" / "pc-shell-start").read_text()
        self.assertIn("/dev/virtio-ports/com.redhat.spice.0", launcher)
        self.assertIn("spice-vdagent", launcher)

    def test_gaming_uses_a_captured_relative_mouse(self):
        src = (ROOT / "desktop" / "vm.js").read_text()
        self.assertIn('<input type="mouse" bus="ps2"/>', src)
        self.assertIn('<input type="tablet" bus="usb"/>', src)
        create = src[src.index('async function create('):src.index('async function view(')]
        self.assertNotIn('<input type="mouse" bus="ps2"/>', create,
                         "new VMs must not capture the pointer merely when it enters the viewer")
        self.assertIn('<input type="tablet" bus="usb"/>', create)
        self.assertIn("async function gamingMouse", src)
        ui = (ROOT / "static/js/client/os.js").read_text()
        self.assertIn("data-vme-mouse", ui)
        self.assertIn("Ctrl+Alt releases it", ui)
        self.assertIn("Enable gaming mouse capture", ui)

    def test_powered_off_vm_hardware_is_editable_in_the_ui(self):
        ui = (ROOT / "static/js/client/os.js").read_text()
        for api in ("details", "update", "addDisk", "changeIso", "ejectIso", "addNetwork"):
            self.assertIn("pcVM." + api, ui)
        self.assertIn("data-vm-edit-open", ui)

    def test_installation_iso_can_be_ejected_for_a_real_disk_boot(self):
        backend = (ROOT / "desktop" / "vm.js").read_text()
        preload = (ROOT / "desktop" / "preload.js").read_text()
        main = (ROOT / "desktop" / "main.js").read_text()
        ui = (ROOT / "static" / "js" / "client" / "os.js").read_text()
        self.assertIn("['change-media',d.name,cd.target,'--eject','--config']", backend)
        self.assertIn("pc:vm:eject-iso", preload)
        self.assertIn("pc:vm:eject-iso", main)
        self.assertIn('data-vme-eject>Eject ISO', ui)

    def test_boot_drive_can_be_selected_and_eject_makes_disk_first(self):
        backend = (ROOT / "desktop" / "vm.js").read_text()
        ui = (ROOT / "static" / "js" / "client" / "os.js").read_text()
        self.assertIn("async function setBootOrder", backend)
        self.assertIn("setBootOrder(d.name,'disk')", backend)
        self.assertIn('data-vme-boot', ui)
        self.assertIn("bootOrder:$('[data-vme-boot]'", ui)

    def test_post_install_disk_boot_is_one_click(self):
        backend = (ROOT / "desktop" / "vm.js").read_text()
        preload = (ROOT / "desktop" / "preload.js").read_text()
        main = (ROOT / "desktop" / "main.js").read_text()
        ui = (ROOT / "static" / "js" / "client" / "os.js").read_text()
        self.assertIn("async function bootDisk", backend)
        self.assertIn("mounted?ejectIso(d.name):setBootOrder(d.name,'disk')", backend)
        self.assertIn("pc:vm:boot-disk", preload)
        self.assertIn("pc:vm:boot-disk", main)
        self.assertIn("Startup disk: installed system", ui)
        self.assertIn("Preparing installed system", ui)
        self.assertIn("await new Promise(resolve=>setTimeout(resolve,1000))", backend)
        self.assertIn("Choose startup disk / edit hardware", ui)
        self.assertIn("await pcVM.bootDisk(n)", ui)
        self.assertNotIn('p.canceled||!p.path', ui, "the ISO picker returns a path string")

    def test_editor_keeps_the_common_path_simple(self):
        ui = (ROOT / "static" / "js" / "client" / "os.js").read_text()
        self.assertIn('<section class="vmui-section"><h3>Performance</h3>', ui)
        self.assertIn('<section class="vmui-section"><h3>Installation disc</h3>', ui)
        self.assertIn('<details class="vmui-advanced">', ui)
        self.assertIn('Eject ISO and boot from disk', ui)

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
