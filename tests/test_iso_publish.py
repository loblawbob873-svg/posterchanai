from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ROOT / "scripts" / "publish_iso.sh"
GENTOO = ROOT / "os" / "gentoo.sh"


def test_publisher_has_the_stable_release_destination():
    src = PUBLISH.read_text()
    assert "root@198.55.116.7" in src
    assert "/iso/posterchanos.iso" in src


def test_publish_is_atomic_and_checksum_guarded():
    src = PUBLISH.read_text()
    assert 'STAGING_PATH="${PUBLISH_PATH}.uploading"' in src
    assert 'REMOTE_SHA=' in src
    assert '[[ "$REMOTE_SHA" != "$LOCAL_SHA" ]]' in src
    assert "mv -f '$STAGING_PATH' '$PUBLISH_PATH'" in src
    assert src.index("REMOTE_SHA=") < src.index("mv -f '$STAGING_PATH' '$PUBLISH_PATH'")


def test_livecd_only_publishes_after_success_and_only_when_clean():
    src = GENTOO.read_text()
    publish = src.index('"$PUBLISHER" "$ISO"')
    assert src.index('grub-mkrescue -o "$ISO"') < publish
    assert src.index('if [[ "${CLEAN,,}" == y*') < publish
    assert "Personal rescue image: not publishing it." in src


def test_publish_script_parses_and_rejects_no_artifact():
    parsed = subprocess.run(["bash", "-n", str(PUBLISH)], capture_output=True, text=True)
    assert parsed.returncode == 0, parsed.stderr
    missing = subprocess.run([str(PUBLISH)], capture_output=True, text=True)
    assert missing.returncode == 2


def test_destination_overrides_cannot_inject_remote_commands(tmp_path):
    image = tmp_path / "clean.iso"
    image.write_bytes(b"not actually uploaded")
    bad = subprocess.run(
        [str(PUBLISH), str(image)],
        env={"PATH": "/usr/bin:/bin", "PC_ISO_PUBLISH_HOST": "root@example;touch /tmp/pwned"},
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 2
    assert "unsafe publish host" in bad.stderr
