from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts/check_installed_os_core_package.sh"


def test_gate_parses_and_is_explicitly_installed_package_scoped():
    result = subprocess.run(["bash", "-n", str(GATE)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    source = GATE.read_text()
    assert "qlist -Iv app-misc/posterchan-desktop" in source
    assert "qlist -Iv app-misc/posterchanos-shell" in source
    assert 'qfile -q "$file"' in source


def test_gate_covers_update_first_run_and_security_critical_modes():
    source = GATE.read_text()
    for path in ("/opt/posterchan/posterchan-desktop",
                 "/opt/posterchan/resources/tor/tor/tor",
                 "/usr/local/bin/update-posterchan",
                 "/usr/local/bin/pc-shell-start", "/usr/bin/gentoo.sh"):
        assert path in source
    assert "chrome-sandbox" in source and "4755" in source
    assert source.count("sudoers.d/") >= 2 and "440" in source
    assert "posterchan-update.lock" in source
    assert "emaint sync -r posterchan" in source
    assert "exec sway" in source and "autologin_user" in source
    assert 'getent passwd "$autologin_user"' in source
    # PosterChanOS installs Gentoo's prebuilt www-client/firefox-bin package.  It intentionally
    # exposes /usr/bin/firefox-bin rather than relying on a distribution-specific `firefox` alias.
    assert "firefox-bin" in source


def test_firefox_gate_executes_payload_not_just_the_surviving_wrapper(tmp_path):
    import os
    source = GATE.read_text()
    start = source.index('check_firefox_payload(){')
    function = source[start:source.index('\n}\n', start)+3]
    wrapper = tmp_path / 'firefox-bin'
    for body, ok in (
        ('echo "Mozilla Firefox 142.0"', True),
        ('echo "/opt/firefox/firefox-bin: No such file or directory" >&2; exit 127', False),
        ('echo "error while loading shared libraries: libxul.so" >&2; exit 127', False),
        ('echo "wrapper exists"', False),
    ):
        wrapper.write_text('#!/bin/bash\n[ "$1" = --version ] || exit 99\n' + body + '\n')
        wrapper.chmod(0o755)
        result = subprocess.run(['bash', '-c', function + '\ncheck_firefox_payload'],
                                env={**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH']},
                                capture_output=True, text=True, timeout=15)
        assert (result.returncode == 0) == ok, result.stderr
        if not ok:
            assert 'Installed Firefox' in result.stderr
