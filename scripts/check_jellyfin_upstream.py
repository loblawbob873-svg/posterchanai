#!/usr/bin/env python3
"""Check official client drift and run the adapter suite with real client tools.

Default: fetch current official releases into a temporary directory, install the
SDK/interpreter without package scripts, run regressions, and compare reviewed
client entry points. --sources reuses an existing checkout for an offline check.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'tests/jellyfin/upstream.json'
ROKU_FILES = ['components/data/VideoData.bs', 'components/data/HomeData.bs',
    'components/ItemGrid/LoadVideoContentTask.bs', 'components/AudioMiniPlayer.bs',
    'components/mediaPlayers/AudioPlayer.bs', 'source/api/Image.bs', 'source/api/Items.bs']
MODEL_ROOT = 'jellyfin-model/src/commonMain/kotlin-generated/org/jellyfin/sdk/model/api'


def fetch_json(url):
    request = urllib.request.Request(url, headers={'User-Agent':'PosterChan-Jellyfin-compatibility'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def download(root):
    for name, repo in [('roku','jellyfin-roku'), ('kotlin','jellyfin-sdk-kotlin')]:
        release = fetch_json('https://api.github.com/repos/jellyfin/' + repo + '/releases/latest')
        archive = root / (name + '.tar.gz')
        url = 'https://api.github.com/repos/jellyfin/' + repo + '/tarball/' + release['tag_name']
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent':'PosterChan-Jellyfin-compatibility'}), timeout=90) as response:
            archive.write_bytes(response.read())
        unpack = root / (name + '-unpack'); unpack.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(unpack, filter='data')
        next(unpack.iterdir()).rename(root / name)
    subprocess.run(['npm','install','--prefix',str(root/'npm'),'--ignore-scripts','--no-audit','--no-fund',
                    '@jellyfin/sdk@latest','brs@0.45.0'],check=True)


def snapshot(root):
    paths = ['roku/' + name for name in ROKU_FILES]
    models = json.loads((ROOT/'tests/jellyfin/kotlin_contract.json').read_text())['models']
    paths += ['kotlin/' + MODEL_ROOT + '/' + name + '.kt' for name in sorted(models)]
    return {'sdk':json.loads((root/'npm/node_modules/@jellyfin/sdk/package.json').read_text())['version'],
            # The compatibility schema also retains enums used by older TVs.
            # Record absence explicitly so a removal/addition is still detected.
            'sha256':{name:hashlib.sha256((root/name).read_bytes()).hexdigest()
                      if (root/name).is_file() else None for name in paths}}


def check(root, record=False, drift_only=False):
    current = snapshot(root)
    if record:
        BASELINE.write_text(json.dumps(current, indent=2) + '\n')
        print('Recorded reviewed upstream files:', len(current['sha256']))
        return 0
    previous = json.loads(BASELINE.read_text())
    changes = [name for name, digest in current['sha256'].items() if previous['sha256'].get(name) != digest]
    if current['sdk'] != previous['sdk']:
        changes.append('@jellyfin/sdk ' + previous['sdk'] + ' → ' + current['sdk'])
    for change in changes:
        print('UPSTREAM REVIEW REQUIRED:', change)
    result = 0
    if not drift_only:
        env = {**os.environ, 'JELLYFIN_TEST_SDK':str(root/'npm/node_modules/@jellyfin/sdk'),
               'ROKU_TEST_SOURCE':str(root/'roku'), 'ROKU_TEST_BRS':str(root/'npm/node_modules/.bin/brs')}
        result = subprocess.run([sys.executable,'-m','pytest','-q','tests/test_jellyfin.py','--tb=short'],
                                cwd=ROOT,env=env).returncode
    if not result and not changes:
        print('Official client contract check passed.')
    return result or (1 if changes else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, help='Existing roku/, kotlin/, npm/ checkouts')
    parser.add_argument('--record', action='store_true', help='Explicitly accept reviewed client changes')
    parser.add_argument('--drift-only', action='store_true', help='Check upstream source changes without running server tests')
    args = parser.parse_args()
    if args.sources:
        return check(args.sources.resolve(), args.record, args.drift_only)
    with tempfile.TemporaryDirectory(prefix='pc-jellyfin-contract-') as temp:
        root = Path(temp); download(root)
        return check(root, args.record, args.drift_only)


if __name__ == '__main__':
    raise SystemExit(main())
