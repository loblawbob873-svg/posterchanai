"""Exercise Media Center install persistence and Compose deployment configuration."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_installer_preserves_custom_roots_and_environment(tmp_path):
    source = (ROOT / 'scripts/install/setup.sh').read_text()
    section = source[source.index('    # Media Center source directory'):source.index('\n}\n')]
    shutil.copy(ROOT / 'media-center.env.example', tmp_path / 'media-center.env.example')
    script = '''
set -eu
sudo(){ if [ "$1" = chown ]; then return 0; fi; "$@"; }
print_success(){ :; }
setup_media(){
''' + section + '\n}\nsetup_media\n'
    env = {**os.environ, 'SCRIPT_DIR': str(tmp_path), 'UPLOAD_PATH': str(tmp_path / 'default'),
           'POSTERCHANAI_DATA': str(tmp_path / 'custom')}
    subprocess.run(['bash', '-c', script], env=env, check=True, capture_output=True)
    config = tmp_path / 'media-center.env'
    assert config.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / 'custom/media').is_dir()
    config.write_text('POSTERCHANAI_MEDIA_ROOTS=/mnt/personal\nPOSTERCHANAI_MEDIA_CACHE=/tmp/my-cache\n')
    subprocess.run(['bash', '-c', script], env=env, check=True, capture_output=True)
    assert config.read_text() == 'POSTERCHANAI_MEDIA_ROOTS=/mnt/personal\nPOSTERCHANAI_MEDIA_CACHE=/tmp/my-cache\n'


def compose_command():
    if os.environ.get('MEDIA_TEST_COMPOSE'):
        return [os.environ['MEDIA_TEST_COMPOSE']]
    if shutil.which('docker-compose'):
        return ['docker-compose']
    if shutil.which('docker') and subprocess.run(['docker', 'compose', 'version'], capture_output=True).returncode == 0:
        return ['docker', 'compose']
    pytest.skip('Docker Compose required; or set MEDIA_TEST_COMPOSE to its executable')


def test_compose_read_only_media_and_tmpfs_preserve_data_volumes(tmp_path):
    cmd = compose_command()
    env = {**os.environ, 'POSTERCHANAI_MEDIA_PATH': str(tmp_path)}
    result = subprocess.run(cmd + ['-f', 'docker-compose.yml', '-f', 'docker-compose.media.yml',
                                  '--profile', '*', 'config', '--format', 'json'],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)['services']
    for name in ('cpu', 'nostr', 'cuda', 'rocm', 'intel'):
        service = services[name]
        mounts = {v['target']: v for v in service['volumes']}
        assert mounts['/var/lib/posterchanai/media']['read_only'] is True
        assert mounts['/var/lib/posterchanai/media']['source'] == str(tmp_path)
        assert mounts['/var/lib/posterchanai/media']['bind'].get('create_host_path', False) is False
        assert mounts['/var/lib/posterchanai']['type'] == 'volume'
        assert any('/tmp/posterchan-media-center:' in v for v in service['tmpfs'])
        assert service['environment']['POSTERCHANAI_MEDIA_ROOTS'] == '/var/lib/posterchanai/media'
    env.pop('POSTERCHANAI_MEDIA_PATH')
    missing = subprocess.run(cmd + ['-f', 'docker-compose.yml', '-f', 'docker-compose.media.yml', 'config'],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert missing.returncode != 0
    assert 'POSTERCHANAI_MEDIA_PATH' in missing.stderr
