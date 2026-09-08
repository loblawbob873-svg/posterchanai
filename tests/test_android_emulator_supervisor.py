"""Exercise real process exit/signal capture and independent cold-boot verdict wiring."""
from pathlib import Path
import os
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'scripts/android_emulator_supervisor.sh'
WORKFLOW=(ROOT/'.github/workflows/android-emulator.yml').read_text()


@pytest.mark.parametrize('ending,expected,signal', [('exit 0',0,0),('exit 17',17,0),('kill -TERM $$',143,15)])
def test_supervisor_reaps_and_reports_real_child_exit(tmp_path,ending,expected,signal):
    emulator=tmp_path/'emulator-real';emulator.write_text('#!/bin/bash\n'+ending+'\n');emulator.chmod(0o755)
    report=tmp_path/'report'
    env=os.environ|{'PC_EMULATOR_REAL':str(emulator),'PC_EMULATOR_EVIDENCE':str(report)}
    result=subprocess.run(['bash',str(SCRIPT),'-avd','test'],env=env,capture_output=True,text=True,timeout=5)
    assert result.returncode==expected,result.stderr
    text=report.read_text();assert 'started' in text
    assert f'exit={expected} signal={signal}' in text


def test_version_probe_does_not_record_a_false_emulator_run(tmp_path):
    emulator=tmp_path/'emulator-real';emulator.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n');emulator.chmod(0o755)
    report=tmp_path/'report'
    result=subprocess.run(['bash',str(SCRIPT),'-version'],env=os.environ|{'PC_EMULATOR_REAL':str(emulator),'PC_EMULATOR_EVIDENCE':str(report)},capture_output=True,text=True,timeout=5)
    assert result.returncode==0 and result.stdout.strip()=='-version'
    assert not report.exists()


def test_two_cold_boots_both_mandatory_even_if_first_fails():
    import yaml
    workflow=yaml.safe_load(WORKFLOW)
    steps=workflow['jobs']['emulator']['steps']
    boots=[s for s in steps if s.get('uses')=='reactivecircus/android-emulator-runner@v2']
    assert len(boots)==2
    first,last=boots
    assert 'android_instrumented.sh' in first['with']['script']
    assert 'android_device_checks.sh' in last['with']['script']
    assert 'always()' in last['if']
    for boot in boots:
        assert '-no-snapshot -wipe-data ' in boot['with']['emulator-options']
    verdict=next(s for s in steps if s.get('name')=='Require both real device verdicts')
    assert verdict['if']=='always()'
    assert 'echo 125' in verdict['run']  # a boot that never reaches its script cannot look passed
    assert '/tmp/pc-emulator-supervisor.txt' in WORKFLOW
