"""Exercise the guest collector against real log files; stale verdicts must never pass."""
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('installed_welcome', ROOT/'scripts/check_livecd_welcome.py')
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)
VERDICT = '[firstrun] showing step=network blocked=0 state={}\n' + MOD.SHELL_READY + '\n'


def collect(homes, baseline=None, *, succeeds=True):
    proc = subprocess.run([sys.executable, '-c', MOD.recovery_collector(homes, baseline)],
                          capture_output=True, text=True, timeout=10)
    if not succeeds:
        assert proc.returncode != 0
        return proc.stderr
    assert proc.returncode == 0, proc.stderr
    return json.loads(base64.b64decode(proc.stdout.strip().split('=', 1)[1]))


def log_file(tmp_path, content):
    path = tmp_path/'posterchan/.config/posterchan-desktop/shell.log'
    path.parent.mkdir(parents=True)
    path.write_text(content)
    return path


def test_old_correct_verdict_cannot_pass_a_new_boot(tmp_path):
    path = log_file(tmp_path, VERDICT)
    baseline = collect(tmp_path)
    with path.open('a') as stream:
        stream.write('new boot: GPU process crashed before the wizard\n')
    fresh = collect(tmp_path, baseline)['posterchan']['fresh']
    assert VERDICT not in fresh
    assert MOD.judge(fresh, 'installed disk') == 2


def test_identical_new_verdict_is_observed_but_not_the_old_one(tmp_path):
    path = log_file(tmp_path, VERDICT)
    baseline = collect(tmp_path)
    with path.open('a') as stream:
        stream.write('new session\n' + VERDICT)
    fresh = collect(tmp_path, baseline)['posterchan']['fresh']
    assert fresh == 'new session\n' + VERDICT
    assert MOD.judge(fresh, 'installed disk') == 0


def test_old_rendering_readiness_cannot_validate_new_wizard_only_boot(tmp_path):
    path = log_file(tmp_path, VERDICT)
    baseline = collect(tmp_path)
    with path.open('a') as stream:
        stream.write('[firstrun] showing step=network blocked=0 state={}\n')
    fresh = collect(tmp_path, baseline)['posterchan']['fresh']
    assert MOD.SHELL_READY not in fresh
    assert MOD.judge(fresh, 'installed disk') == 1


def test_new_wrong_verdict_is_a_failure_even_if_old_one_passed(tmp_path):
    path = log_file(tmp_path, VERDICT)
    baseline = collect(tmp_path)
    with path.open('a') as stream:
        stream.write('[firstrun] skipped step=none blocked=0 state={}\n')
    assert MOD.judge(collect(tmp_path, baseline)['posterchan']['fresh'], 'disk') == 1


@pytest.mark.parametrize('replacement', ['short', 'x' * len(VERDICT)])
def test_truncated_or_changed_prefix_is_never_treated_as_fresh(tmp_path, replacement):
    path = log_file(tmp_path, VERDICT)
    baseline = collect(tmp_path)
    path.write_text(replacement)
    assert 'shell log ' in collect(tmp_path, baseline, succeeds=False)


def test_first_boot_new_account_log_is_allowed(tmp_path):
    baseline = collect(tmp_path)
    log_file(tmp_path, VERDICT)
    assert collect(tmp_path, baseline)['posterchan']['fresh'] == VERDICT


def test_disk_without_recovery_configuration_refuses_before_boot(tmp_path, monkeypatch, capsys):
    disk = tmp_path/'installed.qcow2'
    disk.write_bytes(b'existing install')
    monkeypatch.setattr(sys, 'argv', ['check', '--disk', str(disk)])
    monkeypatch.setattr(MOD, 'run_guest', lambda *a, **k: pytest.fail('unsupported guest was booted'))
    assert MOD.main() == 2
    assert '--recovery-iso and --disk-key-file' in capsys.readouterr().out
    assert disk.read_bytes() == b'existing install'


def test_each_network_mode_boots_a_separate_overlay_and_uses_same_baseline(tmp_path, monkeypatch):
    disk = tmp_path/'installed.qcow2'
    disk.write_bytes(b'original disk')
    args = SimpleNamespace(disk=str(disk), seconds=120)
    baseline = {'posterchan': {'size': 50, 'sha256': 'baseline'}}
    created, booted, observed = [], [], []
    monkeypatch.setattr(MOD.subprocess, 'run', lambda cmd, **kwargs: created.append(cmd))
    monkeypatch.setattr(MOD, 'run_guest', lambda args, iso, disk, seconds, networked: booted.append((disk, networked)))
    def recover(args, overlay, old):
        observed.append((str(overlay), old))
        return {'posterchan': {'fresh': VERDICT}}
    monkeypatch.setattr(MOD, 'recover_logs', recover)
    assert MOD.run_installed_guest(args, baseline, False) == VERDICT
    assert MOD.run_installed_guest(args, baseline, True) == VERDICT
    assert booted[0][0] != booted[1][0]
    assert [b[1] for b in booted] == [False, True]
    assert all(cmd[:8] == ['qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'qcow2', '-b'] for cmd in created)
    assert all(cmd[8] == str(disk.resolve()) for cmd in created)
    assert all(old is baseline for _, old in observed)
    assert disk.read_bytes() == b'original disk'


def test_missing_old_log_has_no_fresh_verdict(tmp_path):
    path = log_file(tmp_path, VERDICT)
    baseline = collect(tmp_path)
    path.unlink()
    snapshot = collect(tmp_path, baseline)
    assert MOD.judge('\n'.join(row['fresh'] for row in snapshot.values()), 'disk') == 2


def test_recovery_attaches_installed_disk_and_iso_read_only(tmp_path, monkeypatch):
    from scripts import check_livecd_install_vm as installer
    code, variables, key = [tmp_path/name for name in ('code.fd', 'vars.fd', 'key')]
    for path in (code, variables):
        path.write_bytes(b'firmware fixture')
    key.write_text('vm-disposable-password\n')
    args = SimpleNamespace(disk_key_file=str(key), recovery_iso=str(tmp_path/'live.iso'),
                           memory=4096, cpus=2, seconds=90)
    monkeypatch.setitem(sys.modules, 'check_livecd_install_vm', installer)
    monkeypatch.setattr(installer, 'ovmf', lambda: (code, variables))
    commands = []
    class BeforeGuestLaunch(Exception):
        pass
    def capture(cmd, **kwargs):
        commands.append(cmd)
        raise BeforeGuestLaunch
    monkeypatch.setattr(MOD.subprocess, 'Popen', capture)
    disk = tmp_path/'existing.qcow2'
    with pytest.raises(BeforeGuestLaunch):
        MOD.recover_logs(args, disk)
    cmd = commands[0]
    drives = [cmd[i+1] for i, arg in enumerate(cmd[:-1]) if arg == '-drive']
    assert f'file={disk},if=virtio,format=qcow2,readonly=on' in drives
    assert f'file={args.recovery_iso},media=cdrom,readonly=on' in drives
    assert cmd[-2:] == ['-nic', 'none']
    assert not any('vm-disposable-password' in arg for arg in cmd)


def test_evidence_write_failure_still_closes_console_and_stops_qemu(tmp_path, monkeypatch):
    from scripts import check_livecd_install_vm as installer
    code, variables, key = [tmp_path/name for name in ('code.fd', 'vars.fd', 'key')]
    for path in (code, variables):
        path.write_bytes(b'firmware fixture')
    key.write_text('vm-disposable-password\n')
    args = SimpleNamespace(disk_key_file=str(key), recovery_iso=str(tmp_path/'live.iso'),
                           memory=4096, cpus=2, seconds=1, evidence_dir=str(tmp_path/'evidence'))
    monkeypatch.setitem(sys.modules, 'check_livecd_install_vm', installer)
    monkeypatch.setattr(installer, 'ovmf', lambda: (code, variables))
    events = []
    class Process:
        stopped = False
        def __init__(self, cmd, **kwargs):
            serial = next(x for x in cmd if x.startswith('socket,id=pcserial,'))
            path = serial.split('path=', 1)[1].split(',', 1)[0]
            Path(path).touch()
        def poll(self):
            return 0 if self.stopped else None
        def terminate(self):
            events.append('terminate')
            self.stopped = True
        def wait(self, timeout):
            events.append('wait')
    class Socket:
        def close(self):
            events.append('close-console')
    class Console:
        def __init__(self, *args):
            self.sock, self.buf = Socket(), ''
        def expect(self, *args):
            return None  # The recovery guest failed to reach its shell.
    monkeypatch.setattr(MOD.subprocess, 'Popen', Process)
    monkeypatch.setattr(installer, 'Serial', Console)
    original_write = Path.write_text
    def fail_evidence(path, *args, **kwargs):
        if path.name.endswith('-recovery.log'):
            raise OSError('evidence volume full')
        return original_write(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', fail_evidence)
    with pytest.raises(OSError, match='evidence volume full'):
        MOD.recover_logs(args, tmp_path/'existing.qcow2')
    assert events == ['close-console', 'terminate', 'wait']


def test_failed_recovery_serial_connection_closes_its_socket(tmp_path, monkeypatch):
    import io
    from scripts import check_livecd_install_vm as installer
    sockets = []
    real_socket = installer.socket.socket
    def tracked_socket(*args, **kwargs):
        sock = real_socket(*args, **kwargs)
        sockets.append(sock)
        return sock
    monkeypatch.setattr(installer.socket, 'socket', tracked_socket)
    # Retain the exception/traceback so garbage collection cannot conceal a leaked descriptor.
    with pytest.raises(OSError) as failure:
        installer.Serial(tmp_path/'absent.sock', io.StringIO())
    assert failure.value is not None
    assert len(sockets) == 1
    assert sockets[0].fileno() == -1


def test_key_prompt_is_visible_after_osc_marker_but_not_in_echoed_command():
    import re
    import shlex
    marker = re.compile(r'(?m)^PC_WELCOME_KEY_READY\r?$')
    command = MOD.disk_key_prompt()
    # The command echo contains literal backslash-n sequences, not a standalone marker.
    assert not marker.search('live@posterchan:~$ ' + command + '\r\n')
    osc = '\x1b]3008;start=fixture\x1b\\'
    proc = subprocess.run(['bash', '-c', 'printf %s ' + shlex.quote(osc) + '; ' + command],
                          input='vm-disposable-password\n', capture_output=True, text=True, timeout=5)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(osc + '\n')
    assert marker.search(proc.stdout), repr(proc.stdout)
    assert 'vm-disposable-password' not in proc.stdout + proc.stderr


def test_collector_record_starts_a_line_after_shell_command_markers(tmp_path):
    import re
    log_file(tmp_path, VERDICT)
    command_marker = '\x1b]3008;start=fixture;type=command\x1b\\'
    script = 'import sys;sys.stdout.write(' + repr(command_marker) + ');' + MOD.recovery_collector(tmp_path)
    proc = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    record = re.search(r'(?m)^PC_WELCOME_DATA=([A-Za-z0-9+/=]+)\r?$', proc.stdout)
    assert record, 'collector output was hidden behind the shell OSC command marker'
    snapshot = json.loads(base64.b64decode(record.group(1)))
    assert snapshot['posterchan']['size'] == len(VERDICT.encode())
