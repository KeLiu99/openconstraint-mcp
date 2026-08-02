"""Unit tests for the deterministic, domain-agnostic solution mutations."""

from __future__ import annotations

import copy

from openconstraint_mcp.pyexec.mutation import (
    ELEMENT_DROPPED,
    ELEMENT_DUPLICATED,
    MUTATION_NAMES,
    NUMERIC_FIELD_PERTURBED,
    OBJECTIVE_PERTURBED,
    SolutionMutation,
    generate_mutations,
)

# A solution shaped like a schedule: one list of task objects plus a scalar.
_SCHEDULE = {
    "makespan": 12,
    "tasks": [
        {"job": 0, "machine": 1, "start": 0},
        {"job": 1, "machine": 2, "start": 4},
    ],
}


def _by_name(solution: dict | None, objective: float | int | None) -> dict[str, SolutionMutation]:
    return {m.name: m for m in generate_mutations(solution, objective)}


def test_generate_returns_the_four_named_mutations_in_a_fixed_order() -> None:
    names = [m.name for m in generate_mutations(_SCHEDULE, 12)]

    assert names == [
        OBJECTIVE_PERTURBED,
        ELEMENT_DROPPED,
        ELEMENT_DUPLICATED,
        NUMERIC_FIELD_PERTURBED,
    ]


def test_mutation_names_matches_what_generate_emits() -> None:
    # `core` builds the fixed row set from `MUTATION_NAMES` alone when generation
    # faults, and sizes the self-test's checker budget from its length, so a name
    # or count that drifts from the generator is a silent mismatch there.
    assert list(MUTATION_NAMES) == [m.name for m in generate_mutations(_SCHEDULE, 12)]


# --- objective_perturbed -----------------------------------------------------


def test_objective_perturbed_adds_one_to_a_finite_objective() -> None:
    mutation = _by_name(_SCHEDULE, 12)[OBJECTIVE_PERTURBED]

    assert mutation.objective == 13


def test_objective_perturbed_leaves_the_solution_alone() -> None:
    # Only the objective is corrupted, so a feasibility-only checker has nothing
    # to reject here — which is exactly why one rejection, not all four, is the rule.
    mutation = _by_name(_SCHEDULE, 12)[OBJECTIVE_PERTURBED]

    assert mutation.solution == _SCHEDULE


def test_objective_perturbed_is_skipped_for_a_null_objective() -> None:
    mutation = _by_name(_SCHEDULE, None)[OBJECTIVE_PERTURBED]

    assert mutation.skipped_reason == "objective is not a finite number"


def test_objective_perturbed_skip_is_not_applied() -> None:
    mutation = _by_name(_SCHEDULE, None)[OBJECTIVE_PERTURBED]

    assert mutation.applied is False


def test_objective_perturbed_is_skipped_when_plus_one_changes_nothing() -> None:
    # A float at or above 2**53 absorbs the +1, so the "mutant" payload would be
    # byte-identical to the accepted one and add no evidence about the checker.
    mutation = _by_name(_SCHEDULE, 1e16)[OBJECTIVE_PERTURBED]

    assert mutation.applied is False


def test_the_unchanged_payload_skip_explains_why_it_was_not_run() -> None:
    mutation = _by_name(_SCHEDULE, 1e16)[OBJECTIVE_PERTURBED]

    assert mutation.skipped_reason is not None
    assert "identical to the accepted one" in mutation.skipped_reason


def test_an_int_objective_too_large_for_a_float_does_not_raise() -> None:
    # `core.normalize_objective` passes a huge int through untouched, so a real
    # `objective` can carry one; `math.isfinite` would raise OverflowError on it.
    names = [m.name for m in generate_mutations(_SCHEDULE, 10**400)]

    assert names == [
        OBJECTIVE_PERTURBED,
        ELEMENT_DROPPED,
        ELEMENT_DUPLICATED,
        NUMERIC_FIELD_PERTURBED,
    ]


def test_an_int_objective_too_large_for_a_float_is_still_perturbed() -> None:
    # An int is mathematically finite at any magnitude, so the corruption applies.
    mutation = _by_name(_SCHEDULE, 10**400)[OBJECTIVE_PERTURBED]

    assert mutation.objective == 10**400 + 1


# --- element_dropped ---------------------------------------------------------


def test_element_dropped_removes_the_last_element_of_the_selected_list() -> None:
    mutation = _by_name(_SCHEDULE, 12)[ELEMENT_DROPPED]

    assert mutation.solution == {"makespan": 12, "tasks": [{"job": 0, "machine": 1, "start": 0}]}


def test_element_dropped_keeps_the_objective() -> None:
    mutation = _by_name(_SCHEDULE, 12)[ELEMENT_DROPPED]

    assert mutation.objective == 12


def test_element_dropped_is_skipped_without_any_non_empty_list() -> None:
    mutation = _by_name({"x": 1, "ys": []}, 1)[ELEMENT_DROPPED]

    assert mutation.skipped_reason is not None


def test_element_dropped_works_on_a_list_of_scalars() -> None:
    # Dropping an entry never looks inside it, so a flat value list — the shape
    # a `{"assign": [3, 1, 2]}` model emits — is as mutable as a list of objects.
    mutation = _by_name({"assign": [3, 1, 2]}, None)[ELEMENT_DROPPED]

    assert mutation.solution == {"assign": [3, 1]}


def test_element_dropped_works_on_a_list_of_strings() -> None:
    # The nonogram example renders its answer as a list of row strings.
    mutation = _by_name({"grid": ["##.", ".#."]}, None)[ELEMENT_DROPPED]

    assert mutation.solution == {"grid": ["##."]}


def test_element_dropped_works_on_a_list_of_lists() -> None:
    # The social-golfers example emits one list of groups per week.
    mutation = _by_name({"week_1": [[1, 2, 3], [4, 5, 6]]}, None)[ELEMENT_DROPPED]

    assert mutation.solution == {"week_1": [[1, 2, 3]]}


# --- element_duplicated ------------------------------------------------------


def test_element_duplicated_appends_a_copy_of_the_first_element() -> None:
    mutation = _by_name(_SCHEDULE, 12)[ELEMENT_DUPLICATED]

    assert mutation.solution is not None
    assert mutation.solution["tasks"][-1] == {"job": 0, "machine": 1, "start": 0}


def test_element_duplicated_grows_the_list_by_one() -> None:
    mutation = _by_name(_SCHEDULE, 12)[ELEMENT_DUPLICATED]

    assert mutation.solution is not None
    assert len(mutation.solution["tasks"]) == 3


def test_element_duplicated_is_skipped_for_an_empty_solution() -> None:
    mutation = _by_name({}, 1)[ELEMENT_DUPLICATED]

    assert mutation.skipped_reason is not None


# --- numeric_field_perturbed -------------------------------------------------


def test_numeric_field_perturbed_bumps_the_first_int_field_of_the_first_element() -> None:
    mutation = _by_name(_SCHEDULE, 12)[NUMERIC_FIELD_PERTURBED]

    assert mutation.solution is not None
    assert mutation.solution["tasks"][0] == {"job": 1, "machine": 1, "start": 0}


def test_numeric_field_perturbed_bumps_a_top_level_int_without_a_list() -> None:
    mutation = _by_name({"x": 3, "y": 2}, None)[NUMERIC_FIELD_PERTURBED]

    assert mutation.solution == {"x": 4, "y": 2}


def test_numeric_field_perturbed_falls_back_past_a_list_with_no_int_field() -> None:
    # A selected list whose leading element is all strings still leaves a scalar
    # summary field worth perturbing; skipping here would waste the probe.
    solution = {"tasks": [{"name": "a"}], "makespan": 12}

    mutation = _by_name(solution, 1)[NUMERIC_FIELD_PERTURBED]

    assert mutation.solution == {"tasks": [{"name": "a"}], "makespan": 13}


def test_numeric_field_perturbed_bumps_a_bare_int_element() -> None:
    # A flat value list has no field to name: the element IS the number.
    mutation = _by_name({"assign": [3, 1, 2]}, None)[NUMERIC_FIELD_PERTURBED]

    assert mutation.solution == {"assign": [4, 1, 2]}


def test_numeric_field_perturbed_prefers_a_top_level_int_to_a_list_head_bool() -> None:
    solution = {"tasks": [True], "makespan": 12}

    mutation = _by_name(solution, 1)[NUMERIC_FIELD_PERTURBED]

    assert mutation.solution == {"tasks": [True], "makespan": 13}


def test_numeric_field_perturbed_is_skipped_for_a_list_of_strings() -> None:
    # A string head yields no number, and there is no top-level int to fall
    # back to.
    mutation = _by_name({"grid": ["##.", ".#."]}, None)[NUMERIC_FIELD_PERTURBED]

    assert mutation.skipped_reason is not None


def test_numeric_field_perturbed_flips_a_bool_field_when_no_int_exists() -> None:
    solution = {"tasks": [{"name": "a", "flag": True}, {"name": "b"}]}

    mutation = _by_name(solution, 1)[NUMERIC_FIELD_PERTURBED]

    assert mutation.solution == {"tasks": [{"name": "a", "flag": False}, {"name": "b"}]}


def test_numeric_field_perturbed_flips_a_flat_boolean_assignment() -> None:
    mutation = _by_name({"x1": True, "x2": False, "x3": True}, None)[
        NUMERIC_FIELD_PERTURBED
    ]

    assert mutation.solution == {"x1": False, "x2": False, "x3": True}


def test_numeric_field_perturbed_skip_names_the_selected_list() -> None:
    solution = {"tasks": [{"name": "a"}]}

    mutation = _by_name(solution, 1)[NUMERIC_FIELD_PERTURBED]

    assert mutation.skipped_reason is not None
    assert "'tasks'" in mutation.skipped_reason


def test_numeric_field_perturbed_ignores_a_bool_field() -> None:
    # Preserve existing numeric targeting when both types are available.
    solution = {"tasks": [{"done": True, "start": 5}]}

    mutation = _by_name(solution, 1)[NUMERIC_FIELD_PERTURBED]

    assert mutation.solution is not None
    assert mutation.solution["tasks"][0] == {"done": True, "start": 6}


# --- selection heuristic -----------------------------------------------------


def test_longest_list_of_objects_wins_over_a_shorter_one() -> None:
    solution = {"a": [{"v": 1}], "b": [{"v": 2}, {"v": 3}, {"v": 4}]}

    mutation = _by_name(solution, 1)[ELEMENT_DROPPED]

    assert mutation.solution == {"a": [{"v": 1}], "b": [{"v": 2}, {"v": 3}]}


def test_a_tie_resolves_by_sorted_key_order() -> None:
    # Insertion order puts "z" first; the documented tie-break must not follow it.
    solution = {"z": [{"v": 1}], "a": [{"v": 2}]}

    mutation = _by_name(solution, 1)[ELEMENT_DROPPED]

    assert mutation.solution == {"z": [{"v": 1}], "a": []}


def test_a_list_of_mixed_types_is_selectable() -> None:
    # Element type never gates selection — only length does.
    solution = {"mixed": [{"v": 1}, 2], "objs": [{"v": 3}]}

    mutation = _by_name(solution, 1)[ELEMENT_DROPPED]

    assert mutation.solution == {"mixed": [{"v": 1}], "objs": [{"v": 3}]}


def test_an_empty_list_is_never_selected() -> None:
    solution = {"empty": [], "objs": [{"v": 3}]}

    mutation = _by_name(solution, 1)[ELEMENT_DROPPED]

    assert mutation.solution == {"empty": [], "objs": []}


# --- purity ------------------------------------------------------------------


def test_the_callers_solution_is_never_mutated_in_place() -> None:
    original = copy.deepcopy(_SCHEDULE)

    generate_mutations(original, 12)

    assert original == _SCHEDULE


def test_mutations_do_not_share_nested_objects_with_the_caller() -> None:
    # A shallow copy would let a later mutation edit the caller's nested dicts.
    original = copy.deepcopy(_SCHEDULE)
    mutation = _by_name(original, 12)[NUMERIC_FIELD_PERTURBED]

    assert mutation.solution is not None
    assert mutation.solution["tasks"][0] is not original["tasks"][0]


def test_a_none_solution_is_treated_as_empty() -> None:
    mutation = _by_name(None, 12)[OBJECTIVE_PERTURBED]

    assert mutation.solution == {}
