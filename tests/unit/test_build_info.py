import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

from can_explorer import build_info as build_info_module
from can_explorer.build_info import BuildInfo, build_info, source_commit

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def fresh_build_info():
    build_info.cache_clear()
    yield
    build_info.cache_clear()


def test_describe_a_bundle_and_a_source_tree():
    bundle = BuildInfo('1.7.1', '0c63aeb', '2026-11-13 10:02 UTC', 'PyInstaller')
    assert bundle.describe() == '1.7.1 (commit 0c63aeb, built 2026-11-13 10:02 UTC with PyInstaller)'
    assert BuildInfo('1.7.1', '0c63aeb-dirty').describe() == '1.7.1 (commit 0c63aeb-dirty, from source)'
    assert BuildInfo('1.7.1').describe() == '1.7.1 (from source)'
    assert BuildInfo('1.7.1', backend='Nuitka').details() == 'built with Nuitka'


def test_a_bundle_reads_its_build_module(monkeypatch):
    module = types.ModuleType('can_explorer._build_version')
    module.VERSION, module.COMMIT, module.BUILD_DATE, module.BACKEND = (
        '1.7.1',
        'abc1234',
        '2026-11-13 10:02 UTC',
        'Nuitka',
    )
    monkeypatch.setitem(sys.modules, 'can_explorer._build_version', module)
    info = build_info()
    assert (info.commit, info.date, info.backend) == ('abc1234', '2026-11-13 10:02 UTC', 'Nuitka')


def test_a_source_tree_reads_git(monkeypatch):
    monkeypatch.setitem(sys.modules, 'can_explorer._build_version', None)  # Import fails: no build module
    monkeypatch.setattr(build_info_module, 'source_commit', lambda: 'abc1234')
    assert build_info() == BuildInfo(build_info().version, 'abc1234')


def _git(outputs):
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments[1])
        output = outputs.get(arguments[1])
        if isinstance(output, Exception):
            raise output
        return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr='')

    return run, calls


def test_source_commit(tmp_path):
    (tmp_path / '.git').mkdir()
    run, _ = _git({'rev-parse': 'abc1234\n', 'status': ''})
    assert source_commit(tmp_path, run) == 'abc1234'
    run, _ = _git({'rev-parse': 'abc1234\n', 'status': ' M can_explorer/x.py\n'})
    assert source_commit(tmp_path, run) == 'abc1234-dirty'
    run, _ = _git({'rev-parse': FileNotFoundError('git')})  # git not installed
    assert source_commit(tmp_path, run) == ''
    run, _ = _git({'rev-parse': subprocess.TimeoutExpired('git', 2)})
    assert source_commit(tmp_path, run) == ''


def test_source_commit_without_a_git_tree(tmp_path):
    run, calls = _git({})
    assert source_commit(tmp_path, run) == '' and calls == []


def _load_build_script():
    spec = importlib.util.spec_from_file_location('packaging_build', ROOT / 'packaging' / 'build.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_build_script_writes_what_build_info_reads(monkeypatch, tmp_path):
    build = _load_build_script()
    monkeypatch.setattr(build, '_commit', lambda: 'abc1234')
    monkeypatch.setenv('SOURCE_DATE_EPOCH', '1794571200')  # Reproducible builds pin the date
    monkeypatch.setattr(build, 'VERSION_MODULE', tmp_path / '_build_version.py')
    with build.build_module('PyInstaller'):
        namespace = {}
        exec((tmp_path / '_build_version.py').read_text(), namespace)
    assert not (tmp_path / '_build_version.py').exists()  # Removed after the build
    assert namespace['COMMIT'] == 'abc1234' and namespace['BACKEND'] == 'PyInstaller'
    assert namespace['BUILD_DATE'] == '2026-11-13 12:00 UTC'
    assert namespace['VERSION'] == build_info().version


def test_the_build_script_finds_the_commit_from_the_ci_environment(monkeypatch):
    build = _load_build_script()

    def no_git(*args, **kwargs):
        raise FileNotFoundError('git')

    monkeypatch.setattr(build.subprocess, 'run', no_git)
    monkeypatch.setenv('GITHUB_SHA', '0c63aeb5e1d2f3a4b5c6d7e8f9a0b1c2d3e4f5a6')
    assert build._commit() == '0c63aeb'
