from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "os" / "gentoo.sh"


def test_installer_uses_the_posterchan_gentoo_mirror_for_every_artifact():
    text = SCRIPT.read_text()
    assert "sync-uri = https://gentoo.poster.place" in text
    assert (
        "sync-uri = https://gentoo.poster.place/releases/amd64/"
        "binpackages/23.0/x86-64/"
    ) in text
    assert 'GENTOO_MIRRORS=\\"https://gentoo.poster.place\\"' in text
    assert "sync-webrsync-verify-signature = true" in text


def test_installer_does_not_fall_back_to_the_public_repo_endpoint():
    text = SCRIPT.read_text()
    assert "sync-uri = https://distfiles.gentoo.org" not in text
    assert 'GENTOO_MIRRORS=\\"https://distfiles.gentoo.org\\"' not in text


def test_stage3_download_uses_the_local_release_mirror():
    text = SCRIPT.read_text()
    url = (
        "https://gentoo.poster.place/releases/amd64/autobuilds/"
        "current-stage3-amd64-systemd/"
    )
    assert text.count(url) == 2


def test_shell_update_migrates_existing_systems_to_the_local_mirror():
    ebuild = next((SCRIPT.parents[0] / "overlay/app-misc/posterchanos-shell").glob("*.ebuild"))
    text = ebuild.read_text()
    assert "sync-uri = https://gentoo.poster.place" in text
    assert "https://gentoo.poster.place/releases/amd64/binpackages/23.0/x86-64/" in text
    assert "sync-webrsync-verify-signature = true" in text
