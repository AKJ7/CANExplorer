"""Test reports: a standalone HTML page, JUnit XML for continuous integration servers and a Markdown summary
(e.g. for the job summary of GitHub Actions)."""

import html
import xml.etree.ElementTree as ET
from pathlib import Path

from can_explorer.testing.api import Verdict
from can_explorer.testing.runner import TestReport

VERDICT_COLOURS = {
    Verdict.PASSED: '#2ecc71',
    Verdict.FAILED: '#e74c3c',
    Verdict.ERROR: '#f39c12',
    Verdict.SKIPPED: '#8b8fa8',
    Verdict.PENDING: '#8b8fa8',
    Verdict.RUNNING: '#3498db',
}

_STYLE = """
:root { --bg: #171827; --card: #24263d; --alt: #1b1c2e; --border: #2e3150; --text: #e6e8f2; --muted: #8b8fa8; }
@media (prefers-color-scheme: light) {
  :root { --bg: #eef1f7; --card: #ffffff; --alt: #f3f5fa; --border: #dde2ee; --text: #1f2437; --muted: #69708c; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 32px 16px; background: var(--bg); color: var(--text);
       font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; }
h1 { margin: 0 0 4px; font-size: 26px; }
.muted { color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 20px 0; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 14px 18px; }
.card .value { font-size: 26px; font-weight: 700; }
table.env { border-collapse: collapse; margin-top: 8px; }
table.env td { padding: 2px 16px 2px 0; vertical-align: top; }
details { background: var(--card); border: 1px solid var(--border); border-radius: 12px; margin: 8px 0; }
summary { cursor: pointer; padding: 12px 16px; display: flex; gap: 12px; align-items: center; list-style: none; }
summary::-webkit-details-marker { display: none; }
.pill { border-radius: 999px; padding: 2px 10px; font-size: 12px; font-weight: 700; color: #fff; min-width: 64px;
        text-align: center; }
.title { flex: 1; font-weight: 600; }
.steps { border-top: 1px solid var(--border); padding: 8px 16px 14px; }
.steps table { width: 100%; border-collapse: collapse; font: 13px/1.5 ui-monospace, monospace; }
.steps td { padding: 2px 8px; vertical-align: top; border-bottom: 1px solid var(--alt); white-space: pre-wrap; }
.steps td.time { color: var(--muted); width: 80px; text-align: right; }
.steps td.kind { color: var(--muted); width: 60px; }
.ok { color: #2ecc71; } .nok { color: #e74c3c; }
pre.error { background: var(--alt); padding: 10px; border-radius: 8px; overflow-x: auto; }
.req { border: 1px solid var(--border); border-radius: 6px; padding: 0 6px; font-size: 12px; color: var(--muted); }
table.reqs { width: 100%; border-collapse: collapse; background: var(--card); border-radius: 12px; overflow: hidden; }
table.reqs th, table.reqs td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border); }
table.reqs th { color: var(--muted); font-weight: 600; }
"""


def _pill(verdict: str) -> str:
    colour = VERDICT_COLOURS.get(verdict, '#8b8fa8')
    return f'<span class="pill" style="background:{colour}">{html.escape(verdict or "–")}</span>'


def to_html(report: TestReport) -> str:
    total = len(report.results)
    cards = [('Verdict', _pill(report.verdict)), ('Test cases', str(total))]
    cards += [
        (label, str(report.count(verdict)))
        for label, verdict in (
            ('Passed', Verdict.PASSED),
            ('Failed', Verdict.FAILED),
            ('Errors', Verdict.ERROR),
            ('Skipped', Verdict.SKIPPED),
        )
    ]
    cards.append(('Duration', f'{report.duration:.1f} s'))
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>{html.escape(report.title)}</title><style>{_STYLE}</style></head><body><main>',
        f'<h1>{html.escape(report.title)}</h1>',
        f'<div class="muted">{report.started:%Y-%m-%d %H:%M:%S}{" — stopped" if report.stopped else ""}</div>',
        '<div class="cards">',
        *(
            f'<div class="card"><div class="muted">{label}</div><div class="value">{value}</div></div>'
            for label, value in cards
        ),
        '</div><table class="env">',
        *(
            f'<tr><td class="muted">{html.escape(key)}</td><td>{html.escape(value)}</td></tr>'
            for key, value in report.environment.items()
        ),
        '</table>',
    ]
    requirements = report.requirements()
    if requirements:
        parts += [
            '<h2>Requirements</h2><table class="reqs"><tr><th>Requirement</th><th>Verdict</th><th>Test cases</th></tr>',
            *(
                f'<tr><td>{html.escape(requirement)}</td><td>{_pill(verdict)}</td>'
                f'<td>{", ".join(html.escape(result.key) for result in results)}</td></tr>'
                for requirement, verdict, results in requirements
            ),
            '</table>',
        ]
    parts.append('<h2>Test cases</h2>')
    module = None
    for result in report.results:
        if result.module != module:
            module = result.module
            parts.append(f'<h3>{html.escape(module)}</h3>')
        is_open = ' open' if result.verdict in (Verdict.FAILED, Verdict.ERROR) else ''
        checks = f'{result.checks - result.failed_checks}/{result.checks} checks' if result.checks else ''
        parts += [
            f'<details{is_open}><summary>{_pill(result.verdict)}',
            f'<span class="title">{html.escape(result.title)}</span>',
            *(f'<span class="req">{html.escape(requirement)}</span>' for requirement in result.requirements),
            f'<span class="muted">{checks}</span><span class="muted">{result.duration:.2f} s</span></summary>',
            '<div class="steps">',
        ]
        if result.description:
            parts.append(f'<p class="muted">{html.escape(result.description)}</p>')
        if result.error and result.verdict == Verdict.ERROR:
            parts.append(f'<pre class="error">{html.escape(result.error)}</pre>')
        parts.append('<table>')
        for step in result.steps:
            mark = (
                ''
                if step.passed is None
                else '<span class="ok">✔</span> ' if step.passed else '<span class="nok">✘</span> '
            )
            parts.append(
                f'<tr><td class="time">{step.time:.3f} s</td><td class="kind">{html.escape(step.kind)}</td>'
                f'<td>{mark}{html.escape(step.text)}</td></tr>'
            )
        parts.append('</table></div></details>')
    parts.append('</main></body></html>')
    return '\n'.join(parts)


def to_junit(report: TestReport) -> str:
    suites = ET.Element(
        'testsuites',
        name=report.title,
        tests=str(len(report.results)),
        failures=str(report.count(Verdict.FAILED)),
        errors=str(report.count(Verdict.ERROR)),
        skipped=str(report.count(Verdict.SKIPPED)),
        time=f'{report.duration:.3f}',
        timestamp=report.started.isoformat(timespec='seconds'),
    )
    for module in dict.fromkeys(result.module for result in report.results):
        results = [result for result in report.results if result.module == module]
        suite = ET.SubElement(
            suites,
            'testsuite',
            name=module,
            tests=str(len(results)),
            failures=str(sum(result.verdict == Verdict.FAILED for result in results)),
            errors=str(sum(result.verdict == Verdict.ERROR for result in results)),
            skipped=str(sum(result.verdict == Verdict.SKIPPED for result in results)),
            time=f'{sum(result.duration for result in results):.3f}',
        )
        for result in results:
            case = ET.SubElement(suite, 'testcase', classname=module, name=result.name, time=f'{result.duration:.3f}')
            if result.requirements:
                properties = ET.SubElement(case, 'properties')
                for requirement in result.requirements:
                    ET.SubElement(properties, 'property', name='requirement', value=requirement)
            failed_steps = [step.text for step in result.steps if step.passed is False]
            if result.verdict == Verdict.FAILED:
                ET.SubElement(case, 'failure', message='; '.join(failed_steps)[:500]).text = '\n'.join(failed_steps)
            elif result.verdict == Verdict.ERROR:
                ET.SubElement(case, 'error', message=result.error.split('\n')[0][:500]).text = result.error
            elif result.verdict == Verdict.SKIPPED:
                ET.SubElement(case, 'skipped', message=result.error)
            log = '\n'.join(
                f'{step.time:9.3f} {step.kind:5} '
                f'{"" if step.passed is None else "PASS " if step.passed else "FAIL "}{step.text}'
                for step in result.steps
            )
            ET.SubElement(case, 'system-out').text = log
    ET.indent(suites)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suites, encoding='unicode') + '\n'


MARKDOWN_SYMBOLS = {
    Verdict.PASSED: '✅',
    Verdict.FAILED: '❌',
    Verdict.ERROR: '💥',
    Verdict.SKIPPED: '⏭️',
    Verdict.PENDING: '⏳',
    Verdict.RUNNING: '⏳',
}


def _cell(text: str) -> str:
    return text.replace('|', '\\|').replace('\n', ' ')


def to_markdown(report: TestReport) -> str:
    """A summary: overall verdict, the requirements and the test cases, with the failed checks."""
    counts = ', '.join(
        f'{report.count(verdict)} {label}'
        for label, verdict in (
            ('passed', Verdict.PASSED),
            ('failed', Verdict.FAILED),
            ('errors', Verdict.ERROR),
            ('skipped', Verdict.SKIPPED),
        )
    )
    lines = [
        f'## {MARKDOWN_SYMBOLS.get(report.verdict, "")} {report.title}',
        '',
        f'{len(report.results)} test cases: {counts} in {report.duration:.1f} s',
        '',
    ]
    requirements = report.requirements()
    if requirements:
        lines += ['| Requirement | Verdict | Test cases |', '|---|---|---|']
        lines += [
            f'| {_cell(requirement)} | {MARKDOWN_SYMBOLS.get(verdict, "")} {verdict} | '
            f'{_cell(", ".join(result.key for result in results))} |'
            for requirement, verdict, results in requirements
        ]
        lines.append('')
    lines += ['| Test case | Verdict | Duration | Notes |', '|---|---|---|---|']
    for result in report.results:
        notes = [step.text for step in result.steps if step.passed is False]
        if result.verdict in (Verdict.ERROR, Verdict.SKIPPED) and result.error:
            notes.append(result.error.split('\n')[0])
        lines.append(
            f'| {_cell(result.key)} | {MARKDOWN_SYMBOLS.get(result.verdict, "")} {result.verdict} | '
            f'{result.duration:.2f} s | {_cell("; ".join(notes))} |'
        )
    return '\n'.join(lines) + '\n'


def write_report(report: TestReport, path: str | Path) -> Path:
    """Write the report as HTML, JUnit XML or Markdown, by the extension (.html/.htm, .xml or .md)."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in ('.html', '.htm'):
        path.write_text(to_html(report), encoding='utf-8')
    elif suffix == '.xml':
        path.write_text(to_junit(report), encoding='utf-8')
    elif suffix == '.md':
        path.write_text(to_markdown(report), encoding='utf-8')
    else:
        raise ValueError(f'Reports are written as .html, .xml (JUnit) or .md, not {suffix or path.name!r}')
    return path
