import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class BluetoothBackend(unittest.TestCase):
    def test_rejects_an_address_before_running_bluetoothctl(self):
        code = "const b=require('./desktop/bluetooth'); b.device('; reboot','connect').then(x=>console.log(JSON.stringify(x)))"
        out = subprocess.check_output(["node", "-e", code], cwd=ROOT, text=True)
        self.assertIn("invalid Bluetooth address", out)

    def test_fresh_install_has_bluez_audio_and_service(self):
        src = (ROOT / "os" / "gentoo.sh").read_text()
        self.assertIn("net-wireless/bluez", src)
        self.assertIn("media-video/pipewire sound-server bluetooth", src)
        self.assertIn("SERVICES+=(sshd systemd-timesyncd libvirtd bluetooth", src)

    def test_pairing_is_inside_the_volume_mixer(self):
        src = (ROOT / "static" / "js" / "client" / "osshell.js").read_text()
        self.assertIn('data-os="bluetooth"', src)
        self.assertIn("bluetoothPanel", src)
        self.assertIn("Pair", src)
        self.assertIn("Forget", src)


if __name__ == "__main__":
    unittest.main()
