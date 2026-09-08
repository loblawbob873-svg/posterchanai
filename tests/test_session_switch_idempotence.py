"""Both distributed session helpers preserve the signed-in desktop on repeated sign-in."""
import os
from pathlib import Path
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('relative',['os/bin/pc-session-switch','os/overlay/app-misc/posterchanos-shell/files/pc-session-switch'])
@pytest.mark.parametrize('previous_identity,previous_user,restarts',[
    ('same','same',False),('different','same',True),('same','different',True),('absent','absent',True),
])
def test_authenticated_identity_switch_restarts_only_when_console_identity_changes(tmp_path,relative,previous_identity,previous_user,restarts):
    source=(ROOT/relative).read_text()
    start=source.index('write_getty(){');write_getty=source[start:source.index('\n}',start)+2]
    tail=source[source.index('WAS_NPUB='):]
    # Authentication and safe-home checks still precede the restart optimization.
    assert source.index('pc-session-auth verify') < source.index('WAS_NPUB=')
    assert source.index("die 'unsafe home ownership'") < source.index('WAS_NPUB=')
    state=tmp_path/'state';state.mkdir();getty=tmp_path/'override.conf';log=tmp_path/'restarts'
    if previous_identity!='absent':(state/'current-npub').write_text('npub-fixture\n' if previous_identity=='same' else 'npub-other\n')
    if previous_user!='absent':getty.write_text('ExecStart=-/sbin/agetty --autologin '+('pc-fixture' if previous_user=='same' else 'pc-other')+' --noclear %I $TERM\n')
    script='set -eu\n'+write_getty+'\nrestart_later(){ printf restart >> "$RESTART_LOG"; }\n'+tail
    result=subprocess.run(['bash','-c',script],env={**os.environ,'STATE_ROOT':str(state),'GETTY':str(getty),
        'RESTART_LOG':str(log),'NPUB':'npub-fixture','USER_NAME':'pc-fixture','OUT':'user=pc-fixture','TERM':'xterm'},capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert log.exists()==restarts
    assert result.stdout.strip()=='user=pc-fixture'
    assert (state/'current-npub').read_text()=='npub-fixture\n'
    assert '--autologin pc-fixture' in getty.read_text()
    assert 'After=NetworkManager.service network-online.target' in getty.read_text()
