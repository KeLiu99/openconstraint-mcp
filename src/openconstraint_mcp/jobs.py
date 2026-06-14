"""In-process registry for background (async) MiniZinc solve jobs.

The single deliberate, bounded exception to the repo's "no global mutable state"
rule (AGENTS.md): one ``JobRegistry`` instance is created per server in
``create_mcp_server`` and owned by that server's lifecycle — never a module-level
singleton. It lets a client submit a solve as a background job, poll its status,
fetch the final ``SolveResult``, and cancel a running job, so hard solves no longer
hit synchronous MCP client timeouts.

Bounding (D1.3 / D1.5): at most ``max_running_jobs`` solves run concurrently (a
fixed ``ThreadPoolExecutor`` pool); further submissions sit ``queued`` until a
worker frees up, up to ``max_queued_jobs``; a submit beyond that is rejected with
``JobRejectedError`` rather than growing unbounded. Retained terminal jobs are
capped at ``max_retained_terminal`` (oldest evicted), and ``shutdown`` terminates
any still-running child process tree.

Layering: this is a server-layer module — it imports the ``minizinc`` solve
machinery and ``schemas``; it never imports ``server``.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from subprocess import Popen
from typing import cast
from uuid import uuid4

# jobs reuses core's internal solve helpers (validation, arg-building, process
# teardown) rather than re-implementing them; they're package-internal, not a public API.
# noinspection PyProtectedMember
from .minizinc.core import (
    DEFAULT_SOLVE_TIMEOUT_MS,
    DEFAULT_SOLVER,
    _solve_extra_args,
    _terminate_process_tree,
    _validate_model_and_timeout,
    solve_model_cancellable,
)

# _RESULT_BEARING_STATES is imported, not re-declared: it is the load-bearing
# D1.9 invariant ("result present iff state in this set") and schemas owns it, so
# _finalize and the SolveJobStatus validator can never drift apart.
# noinspection PyProtectedMember
from .schemas import (
    _RESULT_BEARING_STATES,
    JobState,
    SolveJobStatus,
    SolveResult,
    job_state_for_result,
)

# States with no further transitions (jobs-only; the result-bearing subset of
# these is _RESULT_BEARING_STATES, imported from schemas).
_TERMINAL_STATES: frozenset[JobState] = cast(
    "frozenset[JobState]", frozenset({"succeeded", "failed", "timeout", "cancelled"})
)


class JobRejectedError(RuntimeError):
    """Raised when a submit would exceed the bounded running+queued capacity (D1.3)."""


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class _SolveRequest:
    """The immutable solve parameters for one job (mirrors ``solve_model``)."""

    model: str
    solver: str
    data: str | None
    checker: str | None
    timeout_ms: int
    free_search: bool
    parallel: int | None
    random_seed: int | None
    all_solutions: bool
    num_solutions: int | None


@dataclass
class _JobRecord:
    """Mutable per-job state, guarded by the registry lock."""

    job_id: str
    request: _SolveRequest
    submitted_at_ms: int
    state: JobState
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    elapsed_ms: int | None = None
    result: SolveResult | None = None
    message: str | None = None
    handle: Popen[str] | None = None
    future: Future[None] | None = None
    cancel_requested: bool = False


class JobRegistry:
    """A bounded, single-owned registry of background solve jobs.

    All record mutation happens under ``_lock``; the critical sections are kept
    trivial. A worker writes only its own record. The bounded pool caps concurrent
    running solves (and thus live MiniZinc subprocesses); a single ``in_flight``
    counter (running + queued) enforces the queue bound, so admission is exactly
    D1.3's three cases: ``in_flight < max_running`` runs now, ``< max_running +
    max_queued`` queues, otherwise rejects.
    """

    def __init__(
        self,
        *,
        max_running_jobs: int = 4,
        max_queued_jobs: int = 16,
        max_retained_terminal: int = 64,
    ) -> None:
        if max_running_jobs < 1:
            raise ValueError("max_running_jobs must be >= 1")
        if max_queued_jobs < 0:
            raise ValueError("max_queued_jobs must be >= 0")
        if max_retained_terminal < 1:
            raise ValueError("max_retained_terminal must be >= 1")
        self._max_running = max_running_jobs
        self._max_queued = max_queued_jobs
        self._max_retained_terminal = max_retained_terminal
        self._lock = threading.Lock()
        self._records: dict[str, _JobRecord] = {}
        self._terminal_order: list[str] = []
        self._in_flight = 0
        self._executor = ThreadPoolExecutor(
            max_workers=max_running_jobs, thread_name_prefix="solve-job"
        )

    def submit(
        self,
        *,
        model: str,
        solver: str = DEFAULT_SOLVER,
        data: str | None = None,
        checker: str | None = None,
        timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
        free_search: bool = False,
        parallel: int | None = None,
        random_seed: int | None = None,
        all_solutions: bool = False,
        num_solutions: int | None = None,
    ) -> str:
        """Admit a solve as a background job; return its server-generated ``job_id``.

        Validates the model/timeout and solver/search controls up front with the
        exact ``solve_model`` rules (so a bad ``num_solutions``/``parallel`` fails
        fast as a ``ValueError`` before any job exists), then applies D1.3 admission
        under the lock. Returns immediately (state ``queued`` or ``running``)
        without awaiting the solve; raises ``JobRejectedError`` when the bounded
        queue is full — no worker or subprocess is created in that case.
        """
        _validate_model_and_timeout(model, timeout_ms)
        # Validates the controls (parallel/num_solutions ranges + the solver-gated
        # -n); the args are rebuilt by the worker's solve, so they're discarded here.
        _solve_extra_args(
            solver=solver,
            free_search=free_search,
            parallel=parallel,
            random_seed=random_seed,
            all_solutions=all_solutions,
            num_solutions=num_solutions,
        )
        request = _SolveRequest(
            model=model,
            solver=solver,
            data=data,
            checker=checker,
            timeout_ms=timeout_ms,
            free_search=free_search,
            parallel=parallel,
            random_seed=random_seed,
            all_solutions=all_solutions,
            num_solutions=num_solutions,
        )
        with self._lock:
            if self._in_flight >= self._max_running + self._max_queued:
                raise JobRejectedError(
                    f"Job queue is full ({self._max_running} running + "
                    f"{self._max_queued} queued). Retry once a running job finishes."
                )
            job_id = uuid4().hex
            now = _now_ms()
            runs_now = self._in_flight < self._max_running
            record = _JobRecord(
                job_id=job_id,
                request=request,
                submitted_at_ms=now,
                state="running" if runs_now else "queued",
                started_at_ms=now if runs_now else None,
            )
            self._records[job_id] = record
            self._in_flight += 1
            record.future = self._executor.submit(self._run_job, job_id)
        return job_id

    def get(self, job_id: str) -> SolveJobStatus:
        with self._lock:
            return self._to_status(self._require_record(job_id))

    def list(self) -> list[SolveJobStatus]:
        with self._lock:
            return [self._to_status(record) for record in self._records.values()]

    def cancel(self, job_id: str) -> SolveJobStatus:
        """Cancel a job: drop it if still queued, else terminate its process tree.

        A no-op on an already-terminal job. Cancellation before the worker starts
        is handled by ``Future.cancel``; for a running job the live handle's process
        tree is terminated and the worker records the ``cancelled`` state. If the
        handle is not yet recorded (a cancel that races process startup), the
        ``on_start`` hook terminates as soon as it captures the handle.
        """
        with self._lock:
            record = self._require_record(job_id)
            if record.state in _TERMINAL_STATES:
                return self._to_status(record)
            record.cancel_requested = True
            future = record.future
            handle = record.handle
        if future is not None and future.cancel():
            # Cancelled before the worker started: it will never run, so finalize here.
            with self._lock:
                self._finalize(record, "cancelled", None, "Cancelled before start")
                return self._to_status(record)
        if handle is not None:
            _terminate_process_tree(handle)
        with self._lock:
            return self._to_status(record)

    def shutdown(self) -> None:
        """Terminate running children and tear down the worker pool (lifespan exit).

        Cancels not-yet-started queued jobs and finalizes them as ``cancelled`` so
        no record is left non-terminal; terminates every live child process tree
        (orphan handling) — whose worker then finalizes itself — and joins the pool.
        A hard server kill bypasses this — acceptable for a local, non-persistent v1.
        """
        with self._lock:
            # Mark every non-terminal record cancel_requested FIRST. A worker that
            # is running but has not yet recorded its handle (the launch window)
            # would otherwise slip past both the future.cancel() and the handle
            # snapshot below; with the flag set, its own on_start terminates the
            # child the instant it launches, so wait=True joins promptly instead of
            # blocking on the full solve timeout.
            for record in self._records.values():
                if record.state not in _TERMINAL_STATES:
                    record.cancel_requested = True
            records = list(self._records.values())
        # A pending job's future.cancel() succeeds, so its worker will never run to
        # finalize it; do that here. A running job's cancel() returns False — its
        # handle is terminated below and its worker finalizes it (joined by wait).
        for record in records:
            future = record.future
            if future is not None and future.cancel():
                with self._lock:
                    self._finalize(record, "cancelled", None, "Cancelled at shutdown")
        with self._lock:
            handles = [
                r.handle
                for r in self._records.values()
                if r.handle is not None and r.state not in _TERMINAL_STATES
            ]
        for handle in handles:
            _terminate_process_tree(handle)
        self._executor.shutdown(wait=True, cancel_futures=True)

    # --- internals (assume the caller holds the lock unless noted) -------------

    def _require_record(self, job_id: str) -> _JobRecord:
        record = self._records.get(job_id)
        if record is None:
            raise ValueError(f"unknown job_id: {job_id}")
        return record

    @staticmethod
    def _to_status(record: _JobRecord) -> SolveJobStatus:
        # `result` is stored only on result-bearing terminal states, so passing it
        # straight through already satisfies the SolveJobStatus invariant.
        # `elapsed_ms` is frozen at finalize for terminal jobs; for a started-but-
        # running job it is derived from `started_at_ms` on each read so it advances
        # (a `running` job reports `state` + `elapsed_ms` — README / SolveJobStatus).
        if record.state in _TERMINAL_STATES:
            elapsed_ms = record.elapsed_ms
        elif record.started_at_ms is not None:
            elapsed_ms = max(_now_ms() - record.started_at_ms, 0)
        else:
            elapsed_ms = None
        return SolveJobStatus(
            job_id=record.job_id,
            state=record.state,
            solver=record.request.solver,
            submitted_at_ms=record.submitted_at_ms,
            started_at_ms=record.started_at_ms,
            finished_at_ms=record.finished_at_ms,
            elapsed_ms=elapsed_ms,
            result=record.result,
            message=record.message,
        )

    def _finalize(
        self,
        record: _JobRecord,
        state: JobState,
        result: SolveResult | None,
        message: str | None,
    ) -> None:
        # Caller holds the lock. Idempotent against a late cancel: a record already
        # terminal is left untouched.
        if record.state in _TERMINAL_STATES:
            return
        now = _now_ms()
        record.state = state
        record.finished_at_ms = now
        if record.started_at_ms is not None:
            record.elapsed_ms = max(now - record.started_at_ms, 0)
        record.result = result if state in _RESULT_BEARING_STATES else None
        record.message = message
        self._in_flight -= 1
        self._terminal_order.append(record.job_id)
        self._evict_terminal_overflow()

    def _evict_terminal_overflow(self) -> None:
        # Caller holds the lock. FIFO eviction of the oldest terminal jobs beyond
        # the retention cap, so a long-lived server cannot grow unbounded (D1.5).
        while len(self._terminal_order) > self._max_retained_terminal:
            oldest = self._terminal_order.pop(0)
            self._records.pop(oldest, None)

    def _on_start(self, job_id: str, proc: Popen[str]) -> None:
        # Called by the runner the instant the child is launched. Record the handle
        # and, if a cancel already arrived during startup, terminate immediately so
        # the cancel can't slip through the launch window.
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                cancel_now = True  # job evicted mid-flight; don't leave a child
            else:
                record.handle = proc
                cancel_now = record.cancel_requested
        if cancel_now:
            _terminate_process_tree(proc)

    def _run_job(self, job_id: str) -> None:
        # The worker callable: mark running, solve, then record the terminal state.
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return  # evicted before it could start
            request = record.request
            record.state = "running"
            if record.started_at_ms is None:
                record.started_at_ms = _now_ms()
        try:
            result = solve_model_cancellable(
                request.model,
                solver=request.solver,
                data=request.data,
                checker=request.checker,
                timeout_ms=request.timeout_ms,
                free_search=request.free_search,
                parallel=request.parallel,
                random_seed=request.random_seed,
                all_solutions=request.all_solutions,
                num_solutions=request.num_solutions,
                on_start=lambda proc: self._on_start(job_id, proc),
            )
        except Exception as exc:  # noqa: BLE001 - worker boundary: never leak; record as failed
            with self._lock:
                self._finalize(record, "failed", None, _exception_summary(exc))
            return
        with self._lock:
            if record.cancel_requested:
                self._finalize(record, "cancelled", None, "Cancelled by client")
            else:
                self._finalize(record, job_state_for_result(result), result, None)


def _exception_summary(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"
