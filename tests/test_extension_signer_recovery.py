"""Delegated NIP-07 auth signing through shipped crypto/background code and a controlled relay."""
from pathlib import Path
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]
SCENARIOS=['healthy','first-stalled','close-before-open','dial-timeout','close-after-publish',
           'dropped-reply','zombie','auth-message','concurrent','rejection','wrong-peer',
           'bad-signature','wrong-recipient','cancel','session-change','permission-denied',
           'healthy-nip44','dm-close','dm-drop-nip44','approval-delay','subscription-failure',
           'repeated-auth-dm-latency','stale-callback','concurrent-auth-dm']

@pytest.mark.parametrize('scenario',SCENARIOS)
def test_delegated_signer_recovery(scenario,tmp_path):
    # Source mode still uses the exact shipped crypto bundle; generated Chrome/Firefox builds
    # are exercised with the same harness by the release gate.
    import shutil
    for name in ('background.js','vaultcore.js'):
        shutil.copyfile(ROOT/'extension'/name,tmp_path/name)
    (tmp_path/'vendor').mkdir()
    shutil.copyfile(ROOT/'static/vendor/nostr/nostr.bundle.js',tmp_path/'vendor/nostr.bundle.js')
    result=subprocess.run(['node','--unhandled-rejections=strict',str(ROOT/'tests/extension_signer_recovery.mjs'),str(tmp_path),scenario],
                          capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
