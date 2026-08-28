from pathlib import Path
import os
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


def test_checksum_sidecar_is_staged_published_and_read_back():
    src = PUBLISH.read_text()
    assert 'CHECKSUM_PATH="${PUBLISH_PATH}.sha256"' in src
    assert 'CHECKSUM_STAGING="${CHECKSUM_PATH}.uploading"' in src
    assert "mv -f '$CHECKSUM_STAGING' '$CHECKSUM_PATH'" in src
    assert 'PUBLISHED_SHA=' in src
    assert '[[ "$PUBLISHED_SHA" != "$LOCAL_SHA" ]]' in src
    assert src.index("REMOTE_SHA=") < src.index("CHECKSUM_STAGING' && chmod")


def test_livecd_only_publishes_after_success_and_only_when_clean():
    src = GENTOO.read_text()
    publish = src.index('"$PUBLISHER" "$ISO"')
    assert src.index('grub-mkrescue -o "$ISO"') < publish
    assert src.index('if [[ "${CLEAN,,}" == y*') < publish
    assert "Personal rescue image: not publishing it." in src


def test_installed_livecd_has_a_package_owned_publisher():
    """The packaged /usr/bin/gentoo.sh must not resolve its helper as /usr/scripts."""
    src = GENTOO.read_text()
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
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


def test_successful_publish_writes_checksum_after_verified_iso(tmp_path):
    """Exercise the shell flow without a network; the fake tools log the remote transaction."""
    image = tmp_path / "posterchan-live.iso"
    image.write_bytes(b"verified image bytes")
    expected = subprocess.check_output(["sha256sum", str(image)], text=True).split()[0]
    bindir, log = tmp_path / "bin", tmp_path / "commands"
    bindir.mkdir()
    (bindir / "scp").write_text("#!/bin/sh\necho scp \"$@\" >> \"$PC_TEST_LOG\"\n")
    (bindir / "ssh").write_text(
        "#!/bin/sh\n"
        "echo ssh \"$@\" >> \"$PC_TEST_LOG\"\n"
        "case \"$2\" in\n"
        "  sha256sum*) printf '%s  staged.iso\\n' \"$PC_TEST_SHA\";;\n"
        "  awk*) printf '%s\\n' \"$PC_TEST_SHA\";;\n"
        "esac\n"
    )
    os.chmod(bindir / "scp", 0o755)
    os.chmod(bindir / "ssh", 0o755)
    env = os.environ.copy()
    env.update(PATH=f"{bindir}:{env['PATH']}", PC_TEST_LOG=str(log), PC_TEST_SHA=expected,
               PC_ISO_PUBLISH_HOST="root@test", PC_ISO_PUBLISH_PATH="/iso/posterchanos.iso")
    result = subprocess.run([str(PUBLISH), str(image)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    commands = log.read_text()
    assert "posterchanos.iso.sha256.uploading" in commands
    transaction = next(line for line in commands.splitlines()
                       if "chmod 0644" in line and "posterchanos.iso.sha256" in line)
    assert transaction.index("posterchanos.iso.uploading' '/iso/posterchanos.iso'") < transaction.index(
        "posterchanos.iso.sha256.uploading' '/iso/posterchanos.iso.sha256'")
    assert "and /iso/posterchanos.iso.sha256" in result.stdout
