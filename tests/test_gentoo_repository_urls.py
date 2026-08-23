from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "os" / "gentoo.sh"


def test_installer_uses_real_gentoo_repository_and_binhost_endpoints():
    text = SCRIPT.read_text()
    assert "sync-uri = https://distfiles.gentoo.org" in text
    assert (
        "sync-uri = https://distfiles.gentoo.org/releases/amd64/"
        "binpackages/23.0/x86-64/"
    ) in text
    assert 'GENTOO_MIRRORS=\\"https://distfiles.gentoo.org\\"' in text


def test_installer_does_not_treat_posterchan_distfiles_as_a_gentoo_mirror():
    text = SCRIPT.read_text()
    assert "https://gentoo.poster.place/releases/" not in text
    assert "sync-uri = https://gentoo.poster.place\"" not in text
    assert 'GENTOO_MIRRORS=\\"https://gentoo.poster.place\\"' not in text


def test_stage3_download_uses_the_official_release_tree():
    text = SCRIPT.read_text()
    url = (
        "https://distfiles.gentoo.org/releases/amd64/autobuilds/"
        "current-stage3-amd64-systemd/"
    )
    assert text.count(url) == 2


def test_shell_update_repairs_the_bad_binhost_on_existing_systems():
    ebuild = next((SCRIPT.parents[0] / "overlay/app-misc/posterchanos-shell").glob("*.ebuild"))
    text = ebuild.read_text()
    assert "gentoo\\.poster\\.place/releases/amd64/binpackages" in text
    assert "https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/x86-64/" in text
