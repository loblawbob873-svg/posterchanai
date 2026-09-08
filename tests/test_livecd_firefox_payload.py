"""A clean ISO keeps its required browser payload and rejects a wrapper-only image."""
import os
from pathlib import Path
import subprocess

SOURCE = Path(os.environ.get('PC_FIREFOX_INSTALLER_SOURCE', Path(__file__).resolve().parents[1] / 'os/gentoo.sh'))


def test_clean_image_keeps_firefox_but_excludes_private_opt_siblings(tmp_path):
    source = SOURCE.read_text()
    start = source.index('\t\tfor F in /opt/*; do')
    block = source[start:source.index('\n\t\tdone', start)+len('\n\t\tdone')]
    # Redirect only the filesystem root, executing the real exclusion predicate and loop.
    block = block.replace('/opt', str(tmp_path / 'opt'))
    (tmp_path / 'opt').mkdir()
    for name in ('posterchan', 'firefox', 'firefox-private', 'android-sdk', 'private-server'):
        (tmp_path / 'opt' / name).mkdir()
    result = subprocess.run(['bash', '-c', 'EXCLUDES=();\n' + block + '\nprintf "%s\\n" "${EXCLUDES[@]}"'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    excluded = {Path(line).name for line in result.stdout.splitlines()}
    assert excluded == {'firefox-private', 'android-sdk', 'private-server'}


def test_final_image_rejects_missing_firefox_binary_or_library():
    source = SOURCE.read_text()
    marker = 'for F in usr/bin/firefox-bin opt/firefox/firefox-bin opt/firefox/libxul.so; do'
    assert marker in source, 'Final squashfs gate never verifies the Firefox payload'
    start = source.index(marker)
    block = source[start:source.index('\n\t\tdone', start)+len('\n\t\tdone')]
    assert source.index('LS="$(unsquashfs -l') < start < source.index('if [[ -n "$MISSING" ]]', start)
    paths = ('usr/bin/firefox-bin', 'opt/firefox/firefox-bin', 'opt/firefox/libxul.so')
    for absent in (None, *paths):
        listing = '\n'.join('squashfs-root/' + p for p in paths if p != absent)
        result = subprocess.run(['bash', '-c', 'MISSING="";\n' + block + '\nprintf "%s" "$MISSING"'],
                                env={**os.environ, 'LS': listing}, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ('/' + absent if absent else '')
