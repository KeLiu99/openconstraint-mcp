"""Deterministic, domain-agnostic mutations of a CP-SAT solution payload.

Feeds the checker self-test: this module produces generic mutations;
``core.run_cpsat_python_file_checked`` runs the checker against them and reports
the verdicts. A rejected mutation shows that the checker does not accept every
payload, but an accepted mutation is inconclusive because the mutation may
remain feasible.

The mutations derive from the payload alone — the ``solution`` dict and the
``objective`` — with zero problem-domain knowledge, so the gate works for any
CSP. Two HEURISTICS choose what to mutate:

- The element mutations target "the longest non-empty list among the solution's
  top-level values", a stand-in for "the collection that carries the answer" (a
  schedule, an assignment, a route). Ties resolve by sorted key order, so the
  choice is identical across runs and across processes. The element type is
  deliberately UNCONSTRAINED: dropping or duplicating an entry never looks
  inside it, so a list of scalars (``{"assign": [3, 1, 2]}``), of strings (a
  rendered grid), or of nested lists is as mutable as a list of objects — and
  those shapes are common enough that requiring objects left the probe inert on
  whole classes of model.
- The numeric mutation bumps a number reachable from that list's first element:
  its first int-valued field when the element is an object, or the element
  itself when it is an int. It falls back to the first top-level int when there
  is no list OR its leading element yields no int. If there is no integer
  anywhere, it flips the first boolean using the same list-first search. These
  fallbacks keep the probe live for a flat variable-to-value ``solution`` — the
  shape a pure satisfaction model usually emits, and one that supports no
  element mutation at all.

Field order, unlike list selection, is INSERTION order: the order the keys
arrived in over the JSON transport, which mirrors the script's own dict. That is
stable for a fixed script, but a script that builds its solution by iterating a
set can shift which field the probe targets between runs.

A mutation that cannot apply is returned as SKIPPED with a reason — never
silently dropped, so a caller can always tell "the checker tolerated this" from
"this was never tried".

Pure and dependency-light: stdlib only, no I/O, no subprocess, and the caller's
``solution`` is never mutated in place. Pure is not TOTAL, though: ``deepcopy``
can exhaust the recursion limit on a deeply nested payload, so callers must
treat ``generate_mutations`` as fallible.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TypeGuard

# The fixed mutation names, in generation order.
OBJECTIVE_PERTURBED = "objective_perturbed"
ELEMENT_DROPPED = "element_dropped"
ELEMENT_DUPLICATED = "element_duplicated"
NUMERIC_FIELD_PERTURBED = "numeric_field_perturbed"

# Every name ``generate_mutations`` emits, in generation order. Exported so a
# caller can build the full fixed-shape row set WITHOUT calling the generator —
# both to size the checker budget and to report the rows as skipped when
# generation itself fails.
MUTATION_NAMES: tuple[str, ...] = (
    OBJECTIVE_PERTURBED,
    ELEMENT_DROPPED,
    ELEMENT_DUPLICATED,
    NUMERIC_FIELD_PERTURBED,
)

_NO_LIST_REASON = "no non-empty list among the solution's top-level values"


@dataclass(frozen=True)
class SolutionMutation:
    """One named mutation of a ``(solution, objective)`` pair.

    ``skipped_reason`` is set IFF the mutation could not apply, in which case
    ``solution``/``objective`` carry no meaning and must not be run. The
    ``applied`` property is the single place that split is expressed.
    """

    name: str
    solution: dict | None = None
    objective: float | int | None = None
    skipped_reason: str | None = None

    @property
    def applied(self) -> bool:
        return self.skipped_reason is None


def _is_plain_int(value: object) -> TypeGuard[int]:
    """True for an ``int`` that is not a ``bool``.

    ``bool`` is an ``int`` subclass, but flipping ``True`` to ``2`` corrupts a
    type rather than a quantity, which is a weaker probe of a numeric constraint.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _select_list_key(solution: dict) -> str | None:
    """Return the key of the longest non-empty list, or ``None``.

    Element type is not filtered: the element mutations move whole entries and
    never look inside one, so any non-empty list is a valid target. Ties break on
    sorted key order (the first key wins), so a solution with two equally long
    collections always yields the same choice.
    """
    best_key: str | None = None
    best_len = 0
    for key in sorted(solution):
        value = solution[key]
        if not isinstance(value, list) or not value:
            continue
        if len(value) > best_len:
            best_key, best_len = key, len(value)
    return best_key


def generate_mutations(
    solution: dict | None, objective: float | int | None
) -> list[SolutionMutation]:
    """Return the four mutations of ``(solution, objective)``, applied or skipped.

    Always returns exactly four entries (``MUTATION_NAMES``), in a fixed order,
    so a caller's report has a stable shape regardless of the payload. The
    returned solutions are deep copies; the caller's ``solution`` is left
    untouched.

    Stdlib-pure does NOT mean infallible: ``copy.deepcopy`` recurses in Python,
    burning ~2 frames per nesting level, while the JSON decode that produced
    ``solution`` is bounded by the C stack instead. A payload nested deeply
    enough therefore arrives intact and raises ``RecursionError`` here. Callers
    must treat this function as fallible — ``core._run_checker_test`` does.
    """
    original = solution or {}
    list_key = _select_list_key(original)
    return [
        _objective_perturbed(original, objective),
        _element_dropped(original, objective, list_key),
        _element_duplicated(original, objective, list_key),
        _numeric_field_perturbed(original, objective, list_key),
    ]


def _objective_perturbed(solution: dict, objective: float | int | None) -> SolutionMutation:
    # No bool/non-finite check: the only caller (`core.run_cpsat_python_file_checked`)
    # passes `run_result.objective`, which already cleared `core._envelope_violation`'s
    # `normalize_objective` gate before either `CpsatPythonResult` construction site
    # stores it — so here it is always `None` or a genuine finite int/float.
    if objective is None:
        return SolutionMutation(
            name=OBJECTIVE_PERTURBED,
            skipped_reason="objective is not a finite number",
        )
    perturbed = objective + 1
    # A float at or above 2**53 absorbs the +1 (``1e16 + 1 == 1e16``), so the
    # mutant payload would be byte-identical to the baseline. Running it would
    # spawn a child, get the correct acceptance, and miscount that acceptance as
    # evidence about the checker. An unchanged payload is not a mutation —
    # report it as skipped.
    if perturbed == objective:
        return SolutionMutation(
            name=OBJECTIVE_PERTURBED,
            skipped_reason=(
                f"objective {objective!r} is too large for +1 to change its value, "
                "so the mutated payload would be identical to the accepted one"
            ),
        )
    return SolutionMutation(
        name=OBJECTIVE_PERTURBED,
        solution=copy.deepcopy(solution),
        objective=perturbed,
    )


def _element_dropped(
    solution: dict, objective: float | int | None, list_key: str | None
) -> SolutionMutation:
    if list_key is None:
        return SolutionMutation(name=ELEMENT_DROPPED, skipped_reason=_NO_LIST_REASON)
    mutated = copy.deepcopy(solution)
    mutated[list_key].pop()
    return SolutionMutation(name=ELEMENT_DROPPED, solution=mutated, objective=objective)


def _element_duplicated(
    solution: dict, objective: float | int | None, list_key: str | None
) -> SolutionMutation:
    if list_key is None:
        return SolutionMutation(name=ELEMENT_DUPLICATED, skipped_reason=_NO_LIST_REASON)
    mutated = copy.deepcopy(solution)
    mutated[list_key].append(copy.deepcopy(mutated[list_key][0]))
    return SolutionMutation(name=ELEMENT_DUPLICATED, solution=mutated, objective=objective)


def _numeric_field_perturbed(
    solution: dict, objective: float | int | None, list_key: str | None
) -> SolutionMutation:
    # Search the selected list's head before the top level, but exhaust integer
    # candidates before using a boolean fallback anywhere.
    candidates: list[tuple[object, str | None]] = [(solution, None)]
    if list_key is not None:
        candidates.insert(0, (solution[list_key][0], list_key))

    for booleans in (False, True):
        for candidate, candidate_list_key in candidates:
            values = candidate.items() if isinstance(candidate, dict) else ((0, candidate),)
            for field, value in values:
                if (isinstance(value, bool) if booleans else _is_plain_int(value)):
                    mutated = copy.deepcopy(solution)
                    if candidate_list_key is None:
                        target = mutated
                    elif isinstance(candidate, dict):
                        target = mutated[candidate_list_key][0]
                    else:
                        target = mutated[candidate_list_key]
                    if booleans:
                        target[field] = not target[field]
                    else:
                        target[field] += 1
                    return SolutionMutation(
                        name=NUMERIC_FIELD_PERTURBED, solution=mutated, objective=objective
                    )

    return SolutionMutation(
        name=NUMERIC_FIELD_PERTURBED,
        skipped_reason=(
            "the solution has no top-level integer- or boolean-valued field"
            if list_key is None
            else (
                f"neither the first element of solution[{list_key!r}] nor the solution's "
                "top level yields an integer or boolean to perturb"
            )
        ),
    )
