import itertools
import json

from ortools.sat.python import cp_model

FANO_LINES: tuple[tuple[int, int, int], ...] = (
    (0, 1, 2),
    (0, 3, 4),
    (0, 5, 6),
    (1, 3, 5),
    (1, 4, 6),
    (2, 3, 6),
    (2, 4, 5),
)


def _line_positions() -> tuple[dict[int, list[int]], dict[tuple[int, int], int]]:
    incident_lines: dict[int, list[int]] = {point: [] for point in range(7)}
    edge_line: dict[tuple[int, int], int] = {}
    for line_index, line in enumerate(FANO_LINES):
        for point in line:
            incident_lines[point].append(line_index)
        for left, right in itertools.combinations(line, 2):
            edge_line[(left, right)] = line_index
    return incident_lines, edge_line


def _validate_schedule(schedule: list[list[list[int]]]) -> None:
    seen_pairs: set[tuple[int, int]] = set()
    for groups in schedule:
        week_players = sorted(player for group in groups for player in group)
        if week_players != list(range(1, 22)):
            msg = f"week is not a partition of players 1..21: {groups}"
            raise ValueError(msg)
        for group in groups:
            if len(group) != 3:
                msg = f"group does not have 3 players: {group}"
                raise ValueError(msg)
            for pair in itertools.combinations(sorted(group), 2):
                if pair in seen_pairs:
                    msg = f"pair meets twice: {pair}"
                    raise ValueError(msg)
                seen_pairs.add(pair)
    if len(seen_pairs) != 210:
        msg = f"expected 210 unique pairs, saw {len(seen_pairs)}"
        raise ValueError(msg)


def solve_social_golfers(n_groups: int = 7, group_size: int = 3, n_weeks: int = 10) -> None:
    if (n_groups, group_size, n_weeks) != (7, 3, 10):
        msg = "this compact CP-SAT construction is specialized for the 7-3-10 instance"
        raise ValueError(msg)

    incident_lines, edge_line = _line_positions()
    line_slot = {
        (point, line_index): incident_lines[point].index(line_index)
        for point in range(7)
        for line_index in incident_lines[point]
    }
    permutations = list(itertools.permutations(range(3)))

    model = cp_model.CpModel()

    # Week 1 is fixed as seven groups of three. For weeks 2..10, each Fano-plane
    # line picks one player from each of three week-1 groups. The CP-SAT search
    # chooses, for every later week and week-1 group, which permutation maps that
    # group's three players onto its three incident Fano lines.
    choices = [
        [
            model.new_int_var(0, len(permutations) - 1, f"choice_{week}_{point}")
            for point in range(7)
        ]
        for week in range(n_weeks - 1)
    ]
    line_values: dict[tuple[int, int, int], cp_model.IntVar] = {}
    for week in range(n_weeks - 1):
        for point in range(7):
            for line_index in incident_lines[point]:
                value = model.new_int_var(0, 2, f"value_{week}_{point}_{line_index}")
                model.add_element(
                    choices[week][point],
                    [perm[line_slot[(point, line_index)]] for perm in permutations],
                    value,
                )
                line_values[(week, point, line_index)] = value

    # Every pair of week-1 groups shares exactly one Fano line. Across the nine
    # remaining weeks, the ordered player slots on that shared line must cover all
    # 3 x 3 combinations, so every cross-group player pair meets exactly once.
    for left, right in itertools.combinations(range(7), 2):
        line_index = edge_line[(left, right)]
        pair_codes = []
        for week in range(n_weeks - 1):
            code = model.new_int_var(0, 8, f"pair_{week}_{left}_{right}")
            model.add(
                code
                == 3 * line_values[(week, left, line_index)]
                + line_values[(week, right, line_index)]
            )
            pair_codes.append(code)
        model.add_all_different(pair_codes)

    # Symmetry breaks: week 2 uses identity permutations, then later weeks are
    # ordered by the permutation choices. This removes week-interchange symmetry.
    for point in range(7):
        model.add(choices[0][point] == 0)
    week_codes = []
    for week in range(n_weeks - 1):
        code = model.new_int_var(0, (len(permutations) ** 7) - 1, f"week_code_{week}")
        model.add(
            code
            == sum((len(permutations) ** point) * choices[week][point] for point in range(7))
        )
        week_codes.append(code)
    for left_code, right_code in zip(week_codes, week_codes[1:], strict=False):
        model.add(left_code < right_code)

    model.add_decision_strategy(
        [choices[week][point] for week in range(n_weeks - 1) for point in range(7)],
        cp_model.CHOOSE_MIN_DOMAIN_SIZE,
        cp_model.SELECT_MIN_VALUE,
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 300.0
    solver.parameters.num_workers = 8
    solver.parameters.search_branching = cp_model.PORTFOLIO_WITH_QUICK_RESTART_SEARCH
    solver.parameters.random_seed = 21
    solver.parameters.randomize_search = True
    solver.parameters.use_lns = True
    solver.parameters.diversify_lns_params = True
    solver.parameters.symmetry_level = 2

    status = solver.solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print(json.dumps({"status": "unknown", "objective": None, "solution": None}))
        return

    schedule = [
        [[3 * group + slot + 1 for slot in range(3)] for group in range(7)]
    ]
    for week in range(n_weeks - 1):
        groups = []
        for line_index, line in enumerate(FANO_LINES):
            group = sorted(
                3 * point + solver.value(line_values[(week, point, line_index)]) + 1
                for point in line
            )
            groups.append(group)
        schedule.append(groups)

    _validate_schedule(schedule)

    print(f"Solver status: {solver.status_name(status)}")
    sol = {}
    for week, groups in enumerate(schedule, start=1):
        week_str = "  ".join(f"[{' '.join(map(str, group))}]" for group in groups)
        print(f"Week {week}: {week_str}")
        sol[f"week_{week}"] = groups
    print(json.dumps({"status": "feasible", "objective": None, "solution": sol}))


solve_social_golfers()
