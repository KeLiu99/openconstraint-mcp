"""Deterministic, domain-agnostic mutations for CP-SAT checker self-tests."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TypeGuard

# The fixed mutation names, in generation order.
OBJECTIVE_PERTURBED = "objective_perturbed"
ELEMENT_DROPPED = "element_dropped"
ELEMENT_DUPLICATED = "element_duplicated"
NUMERIC_FIELD_PERTURBED = "numeric_field_perturbed"

# Fixed output shape used to budget checker probes and report generation failures.
MUTATION_NAMES: tuple[str, ...] = (
    OBJECTIVE_PERTURBED,
    ELEMENT_DROPPED,
    ELEMENT_DUPLICATED,
    NUMERIC_FIELD_PERTURBED,
)

_NO_LIST_REASON = "no non-empty list among the solution's top-level values"


@dataclass(frozen=True)
class SolutionMutation:
    """One applied or skipped ``(solution, objective)`` mutation.

    A skipped mutation has no usable payload.
    """

    name: str
    solution: dict | None = None
    objective: float | int | None = None
    skipped_reason: str | None = None

    @property
    def applied(self) -> bool:
        return self.skipped_reason is None


def _is_plain_int(value: object) -> TypeGuard[int]:
    """True for an ``int`` other than ``bool``; booleans are flipped, not incremented."""
    return isinstance(value, int) and not isinstance(value, bool)


def _select_list_key(solution: dict) -> str | None:
    """Return the longest non-empty top-level list; ties break by sorted key."""
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
    """Return four fixed-order mutations without mutating ``solution``.

    Deep-copying deeply nested input can raise ``RecursionError``.
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
    # Core already normalizes this to ``None`` or a finite numeric objective.
    if objective is None:
        return SolutionMutation(
            name=OBJECTIVE_PERTURBED,
            skipped_reason="objective is not a finite number",
        )
    perturbed = objective + 1
    # Skip a float mutation when ``+ 1`` leaves the payload unchanged.
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
                if isinstance(value, bool) if booleans else _is_plain_int(value):
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
