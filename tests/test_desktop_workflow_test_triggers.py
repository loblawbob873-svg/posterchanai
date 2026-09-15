"""A change to required deployment tests must schedule Desktop release validation."""
import ast
import fnmatch
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _paths():
    workflow = yaml.safe_load((ROOT / '.github/workflows/desktop.yml').read_text())
    # PyYAML's YAML 1.1 loader interprets the unquoted GitHub Actions key `on` as True.
    trigger = workflow['on'] if 'on' in workflow else workflow[True]
    return trigger['push']['paths']


def _match(path, pattern):
    """Match the segment globs used here: ** crosses directories, * does not."""
    parts, globs = path.split('/'), pattern.split('/')
    def walk(i, j):
        if j == len(globs):
            return i == len(parts)
        if globs[j] == '**':
            return walk(i, j+1) or (i < len(parts) and walk(i+1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], globs[j]) and walk(i+1, j+1)
    return walk(0, 0)


def _included(path, patterns):
    included = False
    for pattern in patterns:
        negative = pattern.startswith('!')
        if _match(path, pattern[1:] if negative else pattern):
            included = not negative
    return included


def test_every_required_gate_test_schedules_desktop_ci():
    tree = ast.parse((ROOT / 'scripts/deploy_regression_gate.py').read_text())
    definitions = [node.value for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'TESTS' for t in node.targets)]
    assert len(definitions) == 1, 'could not read the actual required gate TESTS'
    cases = ast.literal_eval(definitions[0])
    assert cases, 'required gate has no tests'
    files = sorted({case.split('::', 1)[0] for case in cases})
    missing = [file for file in files if not _included(file, _paths())]
    assert not missing, 'required tests do not schedule Desktop CI: ' + ', '.join(missing)


@pytest.mark.parametrize('path', [
    'tests/test_future_regression.py',
    'tests/client/test_future_ui.py',
    'tests/client/fixtures/deep/new_helper.cjs',
    'tests/fixtures/new_native_client.c',
])
def test_new_tests_and_their_fixtures_are_automatically_watched(path):
    assert _included(path, _paths()), path


def test_path_matcher_respects_directory_depth_and_ordered_exclusions():
    assert _included('tests/client/example.py', ['tests/**'])
    assert not _included('tests/client/example.py', ['tests/*.py'])
    assert not _included('tests/client/example.py', ['tests/**', '!tests/client/**'])
    assert _included('tests/client/example.py', ['tests/**', '!tests/client/**', 'tests/client/example.py'])
    assert not _included('app/example.py', ['tests/**'])
