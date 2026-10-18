"""The Tests view of a connection: test scripts, running them and their results."""

from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.scripting import ScriptError
from can_explorer.sys.log_handler import get_logger
from can_explorer.testing.api import Verdict
from can_explorer.testing.report import write_report
from can_explorer.testing.runner import TestRunner

logger = get_logger(__name__)

UPDATE_INTERVAL_MS = 100  # Results change with every check; the view is updated at most this often


def _local_path(url: QUrl | str) -> Path:
    if isinstance(url, QUrl):
        return Path(url.toLocalFile() if url.isLocalFile() else url.toString())
    return Path(url)


class TestController(QObject):
    """Every action returns an error text, or '' on success."""

    __test__ = False
    changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.connection = connection
        session = connection.session
        self.runner = TestRunner(
            session,
            lambda: session.decoder.database,
            connection.simulation.runtime.variables,
            on_change=self._schedule_update,
        )
        self._deselected: Set[str] = set()
        self._current = ''  # Test case shown in the details
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(UPDATE_INTERVAL_MS)
        self._timer.timeout.connect(self.changed)

    def _schedule_update(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def _update_now(self) -> None:
        self._timer.stop()
        self.changed.emit()

    # Scripts

    @pyqtProperty('QVariantList', notify=changed)
    def scripts(self) -> List[Dict[str, Any]]:
        return [
            {
                'path': str(script.path),
                'name': script.path.name,
                'cases': sum(len(module.cases) for module in script.modules),
                'error': script.error,
            }
            for script in self.runner.scripts
        ]

    @pyqtSlot(QUrl, result=str)
    def load_script(self, url: QUrl) -> str:
        if self.runner.running:
            return 'Stop the tests before loading scripts'
        script = self.runner.load(_local_path(url))
        if not self._current and self.runner.cases:
            self._current = self.runner.cases[0].key
        self._update_now()
        return script.error

    @pyqtSlot(result=str)
    def reload_scripts(self) -> str:
        """Reload all scripts from disk, e.g. after editing them."""
        if self.runner.running:
            return 'Stop the tests before reloading'
        errors = [self.runner.load(script.path).error for script in list(self.runner.scripts)]
        self._update_now()
        return '\n'.join(error for error in errors if error)

    @pyqtSlot(int)
    def remove_script(self, index: int):
        if not self.runner.running and 0 <= index < len(self.runner.scripts):
            self.runner.unload(self.runner.scripts[index].path)
            self._update_now()

    # Test cases and results

    def _results(self) -> Dict[str, Any]:
        report = self.runner.report
        return {result.key: result for result in report.results} if report else {}

    @pyqtProperty('QVariantList', notify=changed)
    def items(self) -> List[Dict[str, Any]]:
        """Modules and their test cases, as rows of a list."""
        results = self._results()
        rows = []
        for module in self.runner.modules:
            module_results = [results.get(case.key) for case in module.cases]
            verdicts = {result.verdict for result in module_results if result is not None}
            module_verdict = next(
                (verdict for verdict in (Verdict.RUNNING, Verdict.ERROR, Verdict.FAILED) if verdict in verdicts),
                Verdict.PASSED if verdicts and verdicts <= {Verdict.PASSED, Verdict.SKIPPED} else '',
            )
            rows.append(
                {
                    'kind': 'module',
                    'key': module.name,
                    'title': module.name,
                    'description': module.description,
                    'verdict': module_verdict,
                    'selected': any(case.key not in self._deselected for case in module.cases),
                    'duration': '',
                }
            )
            for case in module.cases:
                result = results.get(case.key)
                rows.append(
                    {
                        'kind': 'case',
                        'key': case.key,
                        'title': case.title,
                        'description': case.description,
                        'verdict': result.verdict if result else '',
                        'selected': case.key not in self._deselected,
                        'duration': f'{result.duration:.2f} s' if result and result.duration else '',
                    }
                )
        return rows

    @pyqtSlot(str, bool)
    def set_selected(self, key: str, selected: bool):
        """Select a test case ('Module.case') or all test cases of a module ('Module') for running."""
        keys = {case.key for case in self.runner.cases if case.key == key or case.module == key}
        if selected:
            self._deselected -= keys
        else:
            self._deselected |= keys
        self._update_now()

    @pyqtSlot(bool)
    def select_all(self, selected: bool):
        self._deselected = set() if selected else {case.key for case in self.runner.cases}
        self._update_now()

    @pyqtProperty(int, notify=changed)
    def selected_count(self) -> int:
        return sum(1 for case in self.runner.cases if case.key not in self._deselected)

    @pyqtProperty(str, notify=changed)
    def current(self) -> str:
        return self._current

    @pyqtSlot(str)
    def show_case(self, key: str):
        self._current = key
        self._update_now()

    @pyqtProperty('QVariantMap', notify=changed)
    def details(self) -> Dict[str, Any]:
        """The test case shown in the details: title, description, verdict, error and steps."""
        case = next((case for case in self.runner.cases if case.key == self._current), None)
        if case is None:
            return {}
        result = self._results().get(case.key)
        steps = result.steps if result else []
        return {
            'key': case.key,
            'title': case.title,
            'description': case.description,
            'verdict': result.verdict if result else '',
            'duration': f'{result.duration:.2f} s' if result and result.duration else '',
            'error': result.error if result and result.verdict == Verdict.ERROR else '',
            'steps': [
                {'time': f'{step.time:.3f}', 'kind': step.kind, 'text': step.text, 'passed': step.passed}
                for step in steps
            ],
        }

    # Running

    @pyqtProperty(bool, notify=changed)
    def running(self) -> bool:
        return self.runner.running

    @pyqtProperty(str, notify=changed)
    def running_case(self) -> str:
        return self.runner.current if self.runner.running else ''

    @pyqtProperty('QVariantMap', notify=changed)
    def summary(self) -> Dict[str, Any]:
        report = self.runner.report
        if report is None:
            return {'total': 0, 'done': 0, 'verdict': ''}
        done = sum(1 for result in report.results if result.verdict not in (Verdict.PENDING, Verdict.RUNNING))
        return {
            'total': len(report.results),
            'done': done,
            'passed': report.count(Verdict.PASSED),
            'failed': report.count(Verdict.FAILED),
            'errors': report.count(Verdict.ERROR),
            'skipped': report.count(Verdict.SKIPPED),
            'duration': f'{report.duration:.1f} s' if not self.runner.running else '',
            'verdict': '' if self.runner.running else report.verdict,
            'stopped': report.stopped,
        }

    @pyqtSlot(result=str)
    def run_selected(self) -> str:
        return self._run({case.key for case in self.runner.cases if case.key not in self._deselected})

    @pyqtSlot(str, result=str)
    def run_case(self, key: str) -> str:
        """Run one test case, or all test cases of a module."""
        self._current = key if '.' in key else self._current
        return self._run({case.key for case in self.runner.cases if case.key == key or case.module == key})

    def _run(self, selection: Set[str]) -> str:
        if not selection:
            return 'Select test cases to run'
        if not self.connection.session.is_open:
            return 'Connect before running tests'
        try:
            task = self.runner.start(selection, title=f'Tests on {self.connection.name}')
        except ScriptError as e:
            return str(e)
        task.add_done_callback(lambda _: self._update_now())
        self._update_now()
        return ''

    @pyqtSlot()
    def stop(self):
        self.runner.stop()

    @pyqtSlot(QUrl, result=str)
    def export_report(self, url: QUrl) -> str:
        """Write the results of the last run as HTML or JUnit XML (by the extension). Returns a message."""
        if self.runner.report is None:
            return 'Run tests first'
        try:
            path = write_report(self.runner.report, _local_path(url))
        except (OSError, ValueError) as e:
            return f'Could not write the report: {e}'
        return f'Report written to {path.name}'

    def shutdown(self) -> None:
        self.runner.stop()
