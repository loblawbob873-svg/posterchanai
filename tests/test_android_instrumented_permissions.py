"""Execute the real instrumented runner with adb/Gradle transport and output paths isolated."""
import os
from pathlib import Path
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('install_status,expected',[(0,0),(1,1)])
def test_fresh_instrumentation_boot_installs_permissions_before_running_tests(tmp_path,install_status,expected):
    android=tmp_path/'mobile/android';android.mkdir(parents=True)
    source=android/'app/src/androidTest/Test.java';source.parent.mkdir(parents=True);source.write_text('// fixture')
    apk=android/'app/build/outputs/apk/debug/app-debug.apk';apk.parent.mkdir(parents=True);apk.write_bytes(b'fixture')
    bindir=tmp_path/'bin';bindir.mkdir();log=tmp_path/'calls'
    adb=bindir/'adb';adb.write_text('''#!/bin/sh
printf 'adb %s\n' "$*" >> "$CALLS"
case "$1" in
 devices) printf 'List of devices attached\nemulator-fixture\tdevice\n';;
 install) exit "$INSTALL_STATUS";;
esac
''');adb.chmod(0o755)
    gradle=android/'gradlew';gradle.write_text('#!/bin/sh\nprintf "gradle %s\\n" "$*" >> "$CALLS"\nexit 0\n');gradle.chmod(0o755)
    script=tmp_path/'runner.sh'
    # Retain every command/branch; only relocate its report artifacts away from other active gates.
    script.write_text((ROOT/'scripts/android_instrumented.sh').read_text().replace('/tmp/pc-',str(tmp_path/'pc-')))
    result=subprocess.run(['bash',str(script)],cwd=tmp_path,env=os.environ|{'PATH':str(bindir)+os.pathsep+os.environ['PATH'],'CALLS':str(log),'INSTALL_STATUS':str(install_status)},capture_output=True,text=True)
    assert result.returncode==expected,result.stdout+result.stderr
    calls=log.read_text().splitlines()
    install='adb install -r -g app/build/outputs/apk/debug/app-debug.apk'
    assert install in calls,'cold instrumentation must establish its own granted-runtime-permission fixture'
    gradles=[i for i,line in enumerate(calls) if line.startswith('gradle ')]
    if install_status==0:
        assert len(gradles)==1 and calls.index(install)<gradles[0]
        assert ':app:connectedDebugAndroidTest' in calls[gradles[0]]
    else:
        assert not gradles,'failed installation must not run tests with unknown fixture permissions'
