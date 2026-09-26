from pathlib import Path
import subprocess
from tests.overlay_paths import shell_ebuild


ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ROOT / "scripts" / "publish_iso.sh"
GENTOO = ROOT / "os" / "gentoo.sh"


def test_livecd_only_publishes_after_success_and_only_when_clean():
    src = GENTOO.read_text()
    publish = src.index('"$PUBLISHER" "$ISO"')
    assert src.index('grub-mkrescue -o "$ISO"') < publish
    assert src.index('if [[ "${CLEAN,,}" == y*') < publish
    assert "Personal rescue image: not publishing it." in src


def test_publisher_serves_from_r2_not_from_home():
    """iso.poster.place is the R2 bucket's custom domain; tests/test_publish_r2.py RUNS the flow."""
    src = PUBLISH.read_text()
    assert "publish_r2.py" in src and "https://iso.poster.place/posterchanos.iso" in src
    assert "198.55.116.7" not in src.replace("(198.55.116.7, deleted", ""), "the deleted VPS must not be a target"


def test_installed_livecd_has_a_package_owned_publisher():
    """The packaged /usr/bin/gentoo.sh must not resolve its helper as /usr/scripts."""
    src = GENTOO.read_text()
    ebuild = shell_ebuild().read_text()
    overlay = (ROOT / "scripts/publish_overlay.sh").read_text()
    assert 'INSTALLED_PUBLISHER="/usr/local/libexec/posterchanos/publish_iso.sh"' in src
    assert 'doexe "${FILESDIR}/publish_iso.sh"' in ebuild
    assert 'scripts/publish_iso.sh' in overlay
    assert src.index('[[ -x "$INSTALLED_PUBLISHER" ]]') < src.index('"$PUBLISHER" "$ISO"')


def test_publish_script_parses_and_rejects_no_artifact():
    parsed = subprocess.run(["bash", "-n", str(PUBLISH)], capture_output=True, text=True)
    assert parsed.returncode == 0, parsed.stderr
    missing = subprocess.run([str(PUBLISH)], capture_output=True, text=True)
    assert missing.returncode == 2


def test_a_build_does_not_publish_itself_before_its_gates():
    """2026-09-21: a clean build uploaded itself before the install/boot gates had run."""
    src = (ROOT / "os" / "gentoo.sh").read_text()
    assert 'PUBLISH_ISO="${PC_ISO_PUBLISH:-n}"' in src
    assert "scripts/publish_iso.sh $ISO" in src, "the build must name the publish step it did not take"
