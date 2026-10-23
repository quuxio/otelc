"""Report semantic candidates without claiming their compilation provenance."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Sequence

from otelc.python_code_candidate import Candidate
from otelc.python_code_comparison import MAX_STEPS, compare
from otelc.python_code_index import Descriptor


@dataclass(frozen=True)
class Attribution:
    """Matches use candidate position and local code-node ID, never app addresses."""

    status: Literal['unique-match', 'ambiguous', 'unmatched', 'incomplete']
    matches: tuple[tuple[int, int], ...]
    steps: int
    source_complete: bool = False


def locate(captured: Descriptor, candidates: Sequence[Candidate], *,
           budget: int = MAX_STEPS) -> Attribution:
    """Check every nested code root within one finite comparison budget.

    A failed candidate or exhausted budget can hide another match, so even an
    observed single match remains incomplete in those cases. Equal retained
    inputs remain separate candidates. This does not select a loader/compiler
    path or check pyc cache validity against source bytes.
    """
    if type(budget) is not int or not 1 <= budget <= MAX_STEPS:
        raise ValueError('invalid code attribution budget')
    matches: list[tuple[int, int]] = []
    steps = 0
    incomplete = False
    for position, candidate in enumerate(candidates):
        if steps >= budget:
            return Attribution('incomplete', tuple(matches), steps)
        steps += 1
        if candidate.status != 'decoded' or candidate.descriptor is None:
            incomplete = True
            continue
        for identity, node in enumerate(candidate.descriptor.nodes, 1):
            if steps >= budget:
                return Attribution('incomplete', tuple(matches), steps)
            steps += 1
            if node.kind != 'code':
                continue
            if steps >= budget:
                return Attribution('incomplete', tuple(matches), steps)
            result = compare(captured, replace(candidate.descriptor, root=identity),
                             budget=budget - steps)
            steps += result.steps
            if result.status == 'match':
                matches.append((position, identity))
            elif result.status == 'incomplete':
                incomplete = True
    status = ('incomplete' if incomplete else 'ambiguous' if len(matches) > 1
              else 'unique-match' if matches else 'unmatched')
    return Attribution(status, tuple(matches), steps)
