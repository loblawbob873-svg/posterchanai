#!/usr/bin/env python3
"""Require the latest exact-commit Android emulator run before publishing an APK."""
import argparse
import json
import re
import subprocess
import sys
import time

WORKFLOW = 'android-emulator.yml'


class EvidenceError(RuntimeError):
    pass


def gh_api(endpoint):
    try:
        result = subprocess.run(['gh', 'api', endpoint], capture_output=True, text=True,
                                timeout=30, check=True)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise EvidenceError('Could not read Android emulator evidence from GitHub') from error


def latest_run(api, repository, sha):
    data = api(f'repos/{repository}/actions/workflows/{WORKFLOW}/runs?head_sha={sha}&per_page=100')
    if not isinstance(data, dict) or not isinstance(data.get('workflow_runs'), list):
        raise EvidenceError('Malformed workflow run listing')
    if any(not isinstance(run, dict) for run in data['workflow_runs']):
        raise EvidenceError('Malformed workflow run entry')
    matches = [run for run in data['workflow_runs']
               if run.get('head_sha') == sha
               and run.get('head_repository', {}).get('full_name') == repository
               and run.get('event') in ('push', 'workflow_dispatch')
               and run.get('path') == '.github/workflows/' + WORKFLOW]
    # Do not silently use an older green run when a newer run or rerun is pending/failed.
    return max(matches, key=lambda run: int(run['id']), default=None)


def checked_run(api, repository, sha, listed):
    run = api(f"repos/{repository}/actions/runs/{int(listed['id'])}")
    if not isinstance(run, dict):
        raise EvidenceError('Malformed Android emulator run details')
    if (run.get('id') != listed.get('id') or run.get('head_sha') != sha
            or run.get('head_repository', {}).get('full_name') != repository
            or run.get('path') != '.github/workflows/' + WORKFLOW
            or run.get('event') not in ('push', 'workflow_dispatch')
            or not isinstance(run.get('run_attempt'), int) or run['run_attempt'] < 1
            or run['run_attempt'] < listed.get('run_attempt', 1)):
        raise EvidenceError('Android emulator run identity/attempt does not match the APK commit')
    return run


REQUIRED_STEPS = (
    'Run instrumented checks on a fresh emulator',
    'Run the device checks',
    'Require both real device verdicts',
)


def check_device_job(api, repository, sha, run):
    # Scope to the CURRENT attempt. The generic jobs endpoint can mix rerun evidence.
    data = api(f"repos/{repository}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100")
    if not isinstance(data, dict) or not isinstance(data.get('jobs'), list):
        raise EvidenceError('Missing Android emulator job evidence')
    jobs = [job for job in data['jobs'] if isinstance(job, dict) and job.get('name') == 'emulator']
    if len(jobs) != 1:
        raise EvidenceError('Expected exactly one completed Android emulator job')
    job = jobs[0]
    if (job.get('run_id') != run['id'] or job.get('run_attempt') != run['run_attempt']
            or job.get('head_sha') != sha or job.get('status') != 'completed'
            or job.get('conclusion') != 'success'):
        raise EvidenceError('Android emulator job is skipped, failed, or belongs to another attempt')
    steps = job.get('steps', [])
    for required in REQUIRED_STEPS:
        matches = [step for step in steps if step.get('name') == required]
        if len(matches) != 1 or matches[0].get('conclusion') != 'success' or matches[0].get('status') != 'completed':
            raise EvidenceError('Required Android device step did not succeed: ' + required)


def wait_for_evidence(repository, sha, *, api=gh_api, clock=time.monotonic,
                      sleep=time.sleep, timeout=3900, missing_timeout=180, interval=20):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise EvidenceError('Invalid repository')
    if not re.fullmatch(r'[0-9a-fA-F]{40}', sha):
        raise EvidenceError('Publication requires the complete 40-character commit SHA')
    if timeout <= 0 or missing_timeout <= 0 or interval <= 0:
        raise EvidenceError('Polling limits must be positive')
    start = clock()
    while clock() - start < timeout:
        listed = latest_run(api, repository, sha)
        if listed is None:
            if clock() - start >= missing_timeout:
                raise EvidenceError('No matching Android emulator run for ' + sha
                    + '. Run Android emulator checks on this exact commit before publishing; '
                      'manual APK dispatch does not substitute for device evidence.')
        else:
            run = checked_run(api, repository, sha, listed)
            if run.get('status') == 'completed':
                if run.get('conclusion') != 'success':
                    raise EvidenceError(f"Android emulator run {run['id']} attempt {run['run_attempt']} "
                                        f"finished {run.get('conclusion')}; APK publication blocked")
                check_device_job(api, repository, sha, run)
                # Recheck both latest-run identity and current attempt after observing success.
                # A rerun of the same run ID must not inherit an earlier attempt's green verdict.
                newest = latest_run(api, repository, sha)
                if newest and newest['id'] == run['id']:
                    confirmed = checked_run(api, repository, sha, newest)
                    if (confirmed['run_attempt'] == run['run_attempt']
                            and confirmed.get('status') == 'completed'
                            and confirmed.get('conclusion') == 'success'):
                        print(f"Android emulator evidence: commit {sha}, run {run['id']}, "
                              f"attempt {run['run_attempt']} succeeded", flush=True)
                        return confirmed
            print(f"Waiting for latest Android emulator evidence for {sha}", flush=True)
        sleep(min(interval, max(0, timeout - (clock() - start))))
    raise EvidenceError('Timed out waiting for successful Android emulator evidence for ' + sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--timeout', type=float, default=3900)
    parser.add_argument('--missing-timeout', type=float, default=180)
    args = parser.parse_args()
    try:
        wait_for_evidence(args.repository, args.sha, timeout=args.timeout,
                          missing_timeout=args.missing_timeout)
    except (EvidenceError, KeyError, TypeError, ValueError) as error:
        print('::error title=APK publication blocked::' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
