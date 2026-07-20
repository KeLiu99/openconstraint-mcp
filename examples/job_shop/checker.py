"""Checker script for model.py.

Validates that the emitted job shop schedule is feasible for the ft06
benchmark (data_ft06.json): every task appears exactly once with the right
machine and duration, tasks within a job run in order, no machine runs two
tasks at once, and the reported objective matches the schedule's makespan.

Checker protocol:
- Receives the payload JSON path as sys.argv[1].
- Payload keys: problem (str|null), solution (dict), objective (float|null),
  solver_status (str).
- Prints exactly one JSON object as its final stdout line:
  {"status": "accepted"|"rejected"|"error", "errors": [...], "details": {}}
- "accepted" with an empty errors list is the only passing verdict.

Runs standalone: python checker.py <payload.json>
"""

from __future__ import annotations

import json
import sys
from typing import Any

# Mirrors data_ft06.json. Duplicated (not loaded from disk) because the
# checker runs in an isolated temp directory with only itself and the
# payload present.
JOBS: tuple[tuple[tuple[int, int], ...], ...] = (
    ((2, 1), (0, 3), (1, 6), (3, 7), (5, 3), (4, 6)),
    ((1, 8), (2, 5), (4, 10), (5, 10), (0, 10), (3, 4)),
    ((2, 5), (3, 4), (5, 8), (0, 9), (1, 1), (4, 7)),
    ((1, 5), (0, 5), (2, 5), (3, 3), (4, 8), (5, 9)),
    ((2, 9), (1, 3), (4, 5), (5, 4), (0, 3), (3, 1)),
    ((1, 3), (3, 3), (5, 9), (0, 10), (4, 4), (2, 1)),
)
NUM_MACHINES = 6


def _load_schedule(solution: object) -> tuple[list[dict[str, Any]] | None, list[str]]:
    if not isinstance(solution, dict):
        return None, ["solution is not a dict"]
    schedule = solution.get("schedule")
    if not isinstance(schedule, list):
        return None, ["solution.schedule must be a list"]

    errors: list[str] = []
    for i, entry in enumerate(schedule):
        if not isinstance(entry, dict):
            errors.append(f"schedule[{i}] is not a dict")
            continue
        for key in ("job", "task", "machine", "start", "duration", "end"):
            if not isinstance(entry.get(key), int):
                errors.append(f"schedule[{i}].{key} missing or not an int")
    return (None, errors) if errors else (schedule, errors)


def check_payload(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    solver_status = payload.get("solver_status")
    if solver_status not in {"optimal", "feasible"}:
        errors.append(f"solver_status is {solver_status!r}, expected optimal or feasible")

    schedule, schedule_errors = _load_schedule(payload.get("solution"))
    errors.extend(schedule_errors)

    if schedule is not None:
        seen: set[tuple[int, int]] = set()
        by_machine: dict[int, list[tuple[int, int, int]]] = {m: [] for m in range(NUM_MACHINES)}
        max_end = 0

        for entry in schedule:
            job_id, task_id = entry["job"], entry["task"]
            key = (job_id, task_id)
            if key in seen:
                errors.append(f"job {job_id} task {task_id} appears more than once")
                continue
            seen.add(key)

            if not (0 <= job_id < len(JOBS)) or not (0 <= task_id < len(JOBS[job_id])):
                errors.append(f"job {job_id} task {task_id} is out of range")
                continue

            expected_machine, expected_duration = JOBS[job_id][task_id]
            start, duration, end, machine = (
                entry["start"],
                entry["duration"],
                entry["end"],
                entry["machine"],
            )
            if machine != expected_machine:
                errors.append(
                    f"job {job_id} task {task_id} runs on machine {machine}, "
                    f"expected {expected_machine}"
                )
            if duration != expected_duration:
                errors.append(
                    f"job {job_id} task {task_id} has duration {duration}, "
                    f"expected {expected_duration}"
                )
            if start < 0 or end != start + duration:
                errors.append(f"job {job_id} task {task_id} has inconsistent start/duration/end")

            by_machine[machine].append((start, end, job_id))
            max_end = max(max_end, end)

        missing = {
            (job_id, task_id) for job_id, job in enumerate(JOBS) for task_id in range(len(job))
        } - seen
        if missing:
            errors.append(f"missing tasks: {sorted(missing)}")

        for job_id in range(len(JOBS)):
            job_entries = sorted(
                (e for e in schedule if e["job"] == job_id), key=lambda e: e["task"]
            )
            for prev, nxt in zip(job_entries, job_entries[1:], strict=False):
                if nxt["start"] < prev["end"]:
                    errors.append(
                        f"job {job_id} task {nxt['task']} starts before task {prev['task']} ends"
                    )

        for machine, intervals in by_machine.items():
            intervals.sort()
            for (_start_a, end_a, job_a), (start_b, _end_b, job_b) in zip(
                intervals, intervals[1:], strict=False
            ):
                if start_b < end_a:
                    errors.append(f"machine {machine} overlaps job {job_a} and job {job_b}")

        objective = payload.get("objective")
        if not isinstance(objective, int | float) or isinstance(objective, bool):
            errors.append(
                f"objective must be a number equal to the schedule makespan {max_end}, "
                f"got {objective!r}"
            )
        elif objective != max_end:
            errors.append(f"objective {objective} does not match schedule makespan {max_end}")

    status = "accepted" if not errors else "rejected"
    return {
        "status": status,
        "errors": errors,
        "details": {"num_jobs": len(JOBS), "num_machines": NUM_MACHINES},
    }


def main() -> None:
    if len(sys.argv) != 2:
        print(
            json.dumps(
                {
                    "status": "error",
                    "errors": ["usage: python checker.py <payload.json>"],
                    "details": {},
                }
            )
        )
        return

    with open(sys.argv[1], encoding="utf-8") as payload_file:
        payload = json.load(payload_file)
    print(json.dumps(check_payload(payload)))


if __name__ == "__main__":
    main()
