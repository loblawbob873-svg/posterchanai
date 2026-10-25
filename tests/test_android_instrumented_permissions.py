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
    gradle=android/'gradlew';gradle.write_text('#!/bin/sh\nprintf "gradle %s\\n" "$*" >> "$CALLS"\nmkdir -p app/build/outputs/androidTest-results/connected\nprintf \'<testsuite tests="1"><testcase name="fixture"/></testsuite>\' > app/build/outputs/androidTest-results/connected/TEST-fixture.xml\nexit 0\n');gradle.chmod(0o755)
    script=tmp_path/'runner.sh'
    # Retain every command/branch; only relocate its report artifacts away from other active gates.
    script.write_text((ROOT/'scripts/android_instrumented.sh').read_text().replace('/tmp/pc-',str(tmp_path/'pc-')))
    (tmp_path / 'android_crash_verdict.py').write_text((ROOT / 'scripts/android_crash_verdict.py').read_text())   # the runner's companion, beside it as in scripts/
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


def test_the_play_services_retry_grants_the_permissions_again(tmp_path):
    """Gradle UNINSTALLS the app when a connected run ends, and its own reinstall grants no runtime permission. So
    when Play Services killed the app on attempt 1 (the one cause that earns a second attempt), attempt 2 ran a
    fresh install with nothing granted: Android's real "Allow PosterChan to access your phone call logs?" dialog
    covered every Phone/Texts screen and ten unrelated device tests failed together (CI run 40163947917, app id
    10192 -> 10194 between attempts). Every attempt must start from the granted fixture."""
    android=tmp_path/'mobile/android';android.mkdir(parents=True)
    source=android/'app/src/androidTest/Test.java';source.parent.mkdir(parents=True);source.write_text('// fixture')
    apk=android/'app/build/outputs/apk/debug/app-debug.apk';apk.parent.mkdir(parents=True);apk.write_bytes(b'fixture')
    bindir=tmp_path/'bin';bindir.mkdir();log=tmp_path/'calls';runs=tmp_path/'runs'
    adb=bindir/'adb';adb.write_text('''#!/bin/sh
printf 'adb %s\\n' "$*" >> "$CALLS"
case "$1" in
 devices) printf 'List of devices attached\\nemulator-fixture\\tdevice\\n';;
 logcat) case "$*" in *threadtime*) case "$*" in *crash*) ;; *)
   [ "$(cat "$RUNS" 2>/dev/null)" = 1 ] && printf '10-25 05:06:49.4 524 1711 I ActivityManager: Killing 5096:place.poster.app/u0a192 (adj 0): depends on provider com.google.android.gms/.fonts.provider.FontsProvider in dying proc com.google.android.gms (adj 100)\\n';;
 esac;; esac;;
esac
''');adb.chmod(0o755)
    gradle=android/'gradlew';gradle.write_text('''#!/bin/sh
printf "gradle %s\\n" "$*" >> "$CALLS"
n=$(( $(cat "$RUNS" 2>/dev/null || echo 0) + 1 )); echo $n > "$RUNS"
[ $n = 1 ] && exit 1
mkdir -p app/build/outputs/androidTest-results/connected
printf '<testsuite tests="1"><testcase name="fixture"/></testsuite>' > app/build/outputs/androidTest-results/connected/TEST-fixture.xml
exit 0
''');gradle.chmod(0o755)
    script=tmp_path/'runner.sh'
    script.write_text((ROOT/'scripts/android_instrumented.sh').read_text().replace('/tmp/pc-',str(tmp_path/'pc-')))
    (tmp_path / 'android_crash_verdict.py').write_text((ROOT / 'scripts/android_crash_verdict.py').read_text())
    result=subprocess.run(['bash',str(script)],cwd=tmp_path,env=os.environ|{'PATH':str(bindir)+os.pathsep+os.environ['PATH'],'CALLS':str(log),'RUNS':str(runs)},capture_output=True,text=True)
    calls=log.read_text().splitlines()
    gradles=[i for i,line in enumerate(calls) if line.startswith('gradle ')]
    assert len(gradles)==2,('the Play Services death did not earn its one retry',result.stdout[-800:]+result.stderr[-800:])
    install='adb install -r -g app/build/outputs/apk/debug/app-debug.apk'
    for k,g in enumerate(gradles):
        before=calls[(gradles[k-1] if k else 0):g]
        assert install in before,('attempt %d ran without granting the runtime permissions first' % (k+1))
    assert result.returncode==0,result.stdout[-800:]+result.stderr[-800:]
