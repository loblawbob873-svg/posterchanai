"""Publishing must wait for exact source/attempt evidence, before touching rolling assets."""
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('android_publish_gate', ROOT / 'scripts/android_publish_gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
REPO = 'owner/project'
SHA = 'a' * 40


def run(number=1, **extra):
    return {'id': number, 'head_sha': SHA, 'head_repository': {'full_name': REPO},
            'path': '.github/workflows/android-emulator.yml', 'event': 'push',
            'run_attempt': 1, 'status': 'completed', 'conclusion': 'success', **extra}


def device_job(number=1, attempt=1):
    return {'name': 'emulator', 'run_id': number, 'run_attempt': attempt, 'head_sha': SHA,
            'status': 'completed', 'conclusion': 'success', 'steps': [
                {'name': name, 'status': 'completed', 'conclusion': 'success'}
                for name in gate.REQUIRED_STEPS]}


class Api:
    def __init__(self, listings, details, jobs=None):
        self.listings, self.details, self.calls = list(listings), list(details), []
        self.jobs = [device_job()] if jobs is None else jobs

    def __call__(self, endpoint):
        self.calls.append(endpoint)
        if '/jobs?' in endpoint:
            return {'jobs': self.jobs}
        values = self.listings if '/workflows/' in endpoint else self.details
        value = values.pop(0) if len(values) > 1 else values[0]
        return {'workflow_runs': value} if '/workflows/' in endpoint else value


class Clock:
    def __init__(self):
        self.time = 0

    def __call__(self):
        return self.time

    def sleep(self, seconds):
        self.time += seconds


def wait(api, **kwargs):
    clock = Clock()
    return gate.wait_for_evidence(REPO, SHA, api=api, clock=clock, sleep=clock.sleep,
                                  timeout=4, missing_timeout=2, interval=1, **kwargs)


def test_exact_commit_success_requires_current_attempt_confirmation():
    api = Api([[run()]], [run()])
    assert wait(api)['id'] == 1
    assert len(api.calls) == 5  # current-attempt job evidence plus independent list/details confirmation
    assert all('head_sha=' + SHA in call for call in api.calls if '/workflows/' in call)


@pytest.mark.parametrize('event', ['push', 'workflow_dispatch'])
def test_queued_and_running_matching_run_eventually_passes(event):
    api = Api([[run(event=event)]], [run(status='queued'), run(status='in_progress'), run(event=event)])
    assert wait(api)['conclusion'] == 'success'


@pytest.mark.parametrize('conclusion', ['failure', 'cancelled', 'timed_out', 'skipped', 'neutral', 'action_required', None])
def test_any_non_success_completion_blocks_publication(conclusion):
    with pytest.raises(gate.EvidenceError, match='publication blocked'):
        wait(Api([[run()]], [run(conclusion=conclusion)]))


@pytest.mark.parametrize('mismatch', [
    {'head_sha': 'b' * 40}, {'head_repository': {'full_name': 'foreign/fork'}},
    {'event': 'pull_request'}, {'path': '.github/workflows/other.yml'},
])
def test_other_commits_repositories_workflows_and_pr_runs_are_not_evidence(mismatch):
    with pytest.raises(gate.EvidenceError, match='No matching Android emulator run'):
        wait(Api([[run(**mismatch)]], []))


def test_manual_dispatch_without_evidence_fails_clearly():
    with pytest.raises(gate.EvidenceError, match='manual APK dispatch'):
        wait(Api([[]], []))


def test_newer_failed_run_cannot_be_hidden_by_older_success():
    api = Api([[run(2, conclusion='failure'), run(1)]], [run(2, conclusion='failure')])
    with pytest.raises(gate.EvidenceError, match='run 2'):
        wait(api)


def test_newer_running_run_cannot_be_hidden_by_older_success():
    api = Api([[run(1), run(2, status='in_progress')]], [run(2, status='in_progress')])
    with pytest.raises(gate.EvidenceError, match='Timed out'):
        wait(api)


def test_successful_old_attempt_does_not_pass_when_rerun_started_during_confirmation():
    api = Api([[run()]], [run(), run(run_attempt=2, status='in_progress')])
    with pytest.raises(gate.EvidenceError, match='Timed out'):
        wait(api)


def test_successful_old_attempt_does_not_pass_when_rerun_failed():
    api = Api([[run()]], [run(), run(run_attempt=2, conclusion='failure')])
    with pytest.raises(gate.EvidenceError, match='attempt 2'):
        wait(api)


def test_new_run_appearing_during_confirmation_blocks_old_green():
    api = Api([[run()], [run(1), run(2)]], [run(1), run(2, conclusion='cancelled')])
    with pytest.raises(gate.EvidenceError, match='run 2'):
        wait(api)


@pytest.mark.parametrize('mismatch', [{'head_sha': 'b' * 40}, {'id': 2}, {'run_attempt': None},
                                     {'run_attempt': 0}, {'event': 'pull_request'}])
def test_detailed_run_identity_is_checked_independently(mismatch):
    with pytest.raises(gate.EvidenceError, match='identity/attempt'):
        wait(Api([[run()]], [run(**mismatch)]))


def test_api_failure_does_not_fall_back_to_prior_evidence():
    def broken(endpoint):
        raise gate.EvidenceError('network unavailable')
    with pytest.raises(gate.EvidenceError, match='network unavailable'):
        wait(broken)


def workflows():
    # BaseLoader preserves GitHub's `on` key instead of treating it as YAML1.1 boolean.
    return [yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)
            for name in ('android.yml', 'android-emulator.yml')]


def test_every_apk_trigger_also_runs_device_gate():
    apk, emulator = workflows()
    assert set(apk['on']['push']['paths']) == set(emulator['on']['push']['paths'])
    assert apk['on']['push']['branches'] == emulator['on']['push']['branches']
    assert set(emulator['on']['push']['paths']) <= set(emulator['on']['pull_request']['paths'])
    for path in ['static/css/**', 'static/i18n/**', '.github/workflows/android.yml',
                 'scripts/android_publish_gate.py']:
        assert path in emulator['on']['push']['paths']


def test_evidence_gate_precedes_every_release_mutation_and_has_only_read_actions_permission():
    apk, _ = workflows()
    steps = apk['jobs']['build']['steps']
    index = next(i for i, step in enumerate(steps) if 'android_publish_gate.py' in step.get('run', ''))
    check = steps[index]
    assert '--sha "$GITHUB_SHA"' in check['run']
    assert '--repository "$GITHUB_REPOSITORY"' in check['run']
    assert 'continue-on-error' not in check and 'if' not in check
    assert '|| true' not in check['run']
    assert check['timeout-minutes'] == '67'
    assert apk['permissions']['actions'] == 'read'
    for i, step in enumerate(steps):
        if ('gh release delete' in step.get('run', '')
                or step.get('uses', '').startswith('softprops/action-gh-release')
                or step.get('name') == 'Publish to Zapstore'):
            assert i > index, step
            assert 'always()' not in step.get('if', '')


def test_details_cannot_reuse_success_from_an_attempt_older_than_the_listing():
    api = Api([[run(run_attempt=2, status='in_progress')]], [run(run_attempt=1)])
    with pytest.raises(gate.EvidenceError, match='identity/attempt'):
        wait(api)


@pytest.mark.parametrize('jobs', [[], [device_job() | {'conclusion': 'skipped'}],
    [device_job() | {'run_attempt': 2}], [device_job() | {'head_sha': 'b' * 40}],
    [device_job() | {'run_id': 2}], [device_job() | {'steps': []}],
    [device_job(), device_job()]])
def test_successful_workflow_without_matching_executed_device_job_is_blocked(jobs):
    with pytest.raises(gate.EvidenceError):
        wait(Api([[run()]], [run()], jobs=jobs))


@pytest.mark.parametrize('required', gate.REQUIRED_STEPS)
@pytest.mark.parametrize('conclusion', ['skipped', 'failure', None])
def test_required_device_steps_must_each_have_completed_success(required, conclusion):
    job = device_job()
    next(step for step in job['steps'] if step['name'] == required)['conclusion'] = conclusion
    with pytest.raises(gate.EvidenceError, match='Required Android device step'):
        wait(Api([[run()]], [run()], jobs=[job]))


def test_required_device_step_names_match_actual_workflow():
    _, emulator = workflows()
    names = {step.get('name') for step in emulator['jobs']['emulator']['steps']}
    assert set(gate.REQUIRED_STEPS) <= names
