import json

from ortools.sat.python import cp_model


def solve_social_golfers(n_groups: int = 6, group_size: int = 3, n_weeks: int = 8) -> None:
    n_golfers = n_groups * group_size
    model = cp_model.CpModel()

    # schedule[w][g][s]: golfer (0-based) at sorted slot s in group g, week w
    schedule = [
        [
            [model.new_int_var(0, n_golfers - 1, f"sch_{w}_{g}_{s}") for s in range(group_size)]
            for g in range(n_groups)
        ]
        for w in range(n_weeks)
    ]

    # golfer_group[w][p]: group (0-based) of golfer p in week w
    golfer_group = [
        [model.new_int_var(0, n_groups - 1, f"gg_{w}_{p}") for p in range(n_golfers)]
        for w in range(n_weeks)
    ]

    # Each week is a permutation of all golfers
    for w in range(n_weeks):
        model.add_all_different(
            [schedule[w][g][s] for g in range(n_groups) for s in range(group_size)]
        )

    # Sorted slots within each group (breaks intra-group permutation symmetry)
    for w in range(n_weeks):
        for g in range(n_groups):
            for s in range(group_size - 1):
                model.add(schedule[w][g][s] < schedule[w][g][s + 1])

    # Channel schedule → golfer_group:  golfer_group[w][schedule[w][g][s]] == g
    for w in range(n_weeks):
        for g in range(n_groups):
            for s in range(group_size):
                model.add_element(schedule[w][g][s], golfer_group[w], g)

    # ── Symmetry breaks ────────────────────────────────────────────────────────
    # Week 1: natural partition
    for g in range(n_groups):
        for s in range(group_size):
            model.add(schedule[0][g][s] == g * group_size + s)

    # Week 2: transversal (valid when n_groups % group_size == 0)
    has_transversal = n_groups % group_size == 0
    if has_transversal:
        for b in range(n_groups // group_size):
            bs = b * group_size
            for g_i in range(group_size):
                for s_i in range(group_size):
                    model.add(schedule[1][bs + g_i][s_i] == (bs + s_i) * group_size + g_i)

    fixed_weeks = {0} | ({1} if has_transversal else set())
    free_weeks = [w for w in range(n_weeks) if w not in fixed_weeks]

    # Group ordering for free weeks: min member of group g < min of group g+1
    for w in free_weeks:
        for g in range(n_groups - 1):
            model.add(schedule[w][g][0] < schedule[w][g + 1][0])

    # ── Identify pairs that already met in fixed weeks ─────────────────────────
    met_already: set[tuple[int, int]] = set()

    def add_group_pairs(members: list[int]) -> None:
        for i, p1 in enumerate(members):
            for p2 in members[i + 1 :]:
                met_already.add((min(p1, p2), max(p1, p2)))

    for g in range(n_groups):
        add_group_pairs([g * group_size + s for s in range(group_size)])
    if has_transversal:
        for b in range(n_groups // group_size):
            bs = b * group_size
            for g_i in range(group_size):
                add_group_pairs([(bs + s_i) * group_size + g_i for s_i in range(group_size)])

    # Already-met: hard "never again" inequalities (tight propagation)
    for p1, p2 in met_already:
        for w in free_weeks:
            model.add(golfer_group[w][p1] != golfer_group[w][p2])

    # ── Soft pair uniqueness: minimize excess meetings ─────────────────────────
    # For each not-yet-met pair, count free-week co-group appearances.
    # same[w][p1][p2]: bool defined via abs_diff + conditional linear constraints
    # (avoids conditional != which is unreliable across OR-Tools versions).
    # Target: minimize total violations to 0 → provably feasible schedule.
    all_violations: list = []

    for p1 in range(n_golfers):
        for p2 in range(p1 + 1, n_golfers):
            if (p1, p2) in met_already:
                continue
            same_week_bools = []
            for w in free_weeks:
                abs_diff = model.new_int_var(0, n_groups - 1, f"ad_{w}_{p1}_{p2}")
                model.add_abs_equality(abs_diff, golfer_group[w][p1] - golfer_group[w][p2])
                same = model.new_bool_var(f"sg_{w}_{p1}_{p2}")
                model.add(abs_diff == 0).only_enforce_if(same)
                model.add(abs_diff >= 1).only_enforce_if(same.negated())
                same_week_bools.append(same)

            # violation = max(0, meetings - 1): reaches 0 iff pair meets at most once
            viol = model.new_int_var(0, len(free_weeks) - 1, f"v_{p1}_{p2}")
            model.add_max_equality(viol, [cp_model.LinearExpr.sum(same_week_bools) - 1, 0])
            all_violations.append(viol)

    # Minimize total violations. Objective = 0 ↔ valid social-golfer schedule.
    model.minimize(cp_model.LinearExpr.sum(all_violations))

    # ── Search ─────────────────────────────────────────────────────────────────
    # First-fail (CHOOSE_MIN_DOMAIN_SIZE) on schedule vars mirrors Gecode's strategy.
    model.add_decision_strategy(
        [schedule[w][g][s] for w in free_weeks for g in range(n_groups) for s in range(group_size)],
        cp_model.CHOOSE_MIN_DOMAIN_SIZE,
        cp_model.SELECT_MIN_VALUE,
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 240.0
    solver.parameters.num_workers = 8
    # Portfolio with quick restarts: diverse search strategies share learned clauses
    solver.parameters.search_branching = cp_model.PORTFOLIO_WITH_QUICK_RESTART_SEARCH

    status = solver.solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        obj = solver.objective_value
        print(f"Solver status: {solver.status_name(status)}, violations: {int(obj)}")
        if int(obj) > 0:
            print(f"Best found has {int(obj)} unresolved pair-meetings — not yet feasible.")
            print(json.dumps({"status": "unknown", "objective": obj, "solution": None}))
            return
        sol = {}
        for w in range(n_weeks):
            groups = [
                [solver.value(schedule[w][g][s]) + 1 for s in range(group_size)]
                for g in range(n_groups)
            ]
            week_str = "  ".join(f"[{' '.join(map(str, sorted(grp)))}]" for grp in groups)
            print(f"Week {w + 1}: {week_str}")
            sol[f"week_{w + 1}"] = [sorted(grp) for grp in groups]
        print(json.dumps({"status": "feasible", "objective": None, "solution": sol}))
    else:
        print(json.dumps({"status": "unknown", "objective": None, "solution": None}))


solve_social_golfers(n_groups=6, group_size=3, n_weeks=8)
