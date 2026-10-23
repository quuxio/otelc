"""Post-run semantic comparisons over retained native journal bytes only."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from time import monotonic

from otelc import python_input_journal as inputs
from otelc.python_code_attribution import locate
from otelc.python_code_candidate import build
from otelc.python_code_comparison import MAX_STEPS
from otelc.python_code_index import decode


def inspect(journal: Path, *, expected_pid: int, interpreter: dict,
            flags: int = 0, optimize: int = 0, timeout: float = 30) -> dict:
    """Require final framing/PID, then compare without reopening source files.

    Call only after the owning application and SDK are terminal. The native
    journal's finalisation is checked here, not process liveness or SDK health.
    Whole-program source/library closure and actual compiler origin remain
    separate requirements even when every selected descriptor has one match.
    """
    if (type(timeout) not in (int, float) or not 0 < timeout <= 30
            or type(flags) is not int or not 0 <= flags <= 0x7fffffff
            or type(optimize) is not int or optimize not in (0, 1, 2)):
        raise ValueError('invalid journal comparison profile')
    retained = inputs.read(journal, expected_pid=expected_pid)
    captured = [decode(value) for value in retained.codes]
    result = {'schema_version': 1, 'kind': 'otelc.python.code-comparisons',
              'application_pid': expected_pid, 'source_complete': False,
              'phase': 'incomplete', 'candidates': [], 'codes': [],
              'compile_flags': flags, 'optimize': optimize,
              'capture_failures': retained.capture_failures,
              'journal_failures': retained.journal_failures,
              'python_finalized': retained.python_finalized}
    if not retained.python_finalized or retained.capture_failures or retained.journal_failures:
        result['reason'] = 'capture-incomplete'
        return result
    if not captured:
        result.update(phase='no-selected-code', reason='no-code-descriptors')
        return result
    profiles = {(value.python_version, value.filesystem_encoding, value.filesystem_errors)
                for value in captured}
    if len(profiles) != 1:
        result['reason'] = 'mixed-code-profiles'
        return result
    version, encoding, errors = next(iter(profiles))
    deadline = monotonic() + timeout
    candidates = []
    for source in (*retained.sources, *retained.imports):
        remaining = deadline - monotonic()
        if remaining <= 0:
            result['reason'] = 'comparison-deadline'
            return result
        kind = 'pyc' if source.path.endswith(b'.pyc') else 'source'
        candidate = build(interpreter, python_version=version, content=source.content,
            filename=source.path.decode(encoding, errors), kind=kind,
            flags=flags if kind == 'source' else 0,
            optimize=optimize if kind == 'source' else 0, timeout=min(10, remaining))
        candidates.append(candidate)
        result['candidates'].append({'path_hex': source.path.hex(), 'sha256': source.sha256,
            'kind': kind, 'status': candidate.status, 'reason': candidate.reason,
            'evidence': candidate.evidence})
    budget = MAX_STEPS
    incomplete = False
    for code in captured:
        if budget <= 0 or monotonic() >= deadline:
            result['reason'] = 'comparison-deadline-or-budget'
            return result
        attribution = locate(code, candidates, budget=budget)
        budget -= attribution.steps
        result['codes'].append({'id': code.identity, 'sha256': code.sha256,
                                **asdict(attribution)})
        incomplete |= attribution.status == 'incomplete'
    result['phase'] = 'incomplete' if incomplete else 'compared'
    return result
