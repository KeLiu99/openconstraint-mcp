"""In-process registry for background (async) CP-SAT Python jobs.

Parallel to ``jobs.py`` (MiniZinc job registry) but for the CP-SAT Python
execution path. One ``CpsatJobRegistry`` instance is created per server and
captured by the tool closures; it is never a module-level singleton.

Layering: imports ``pyexec.core`` (executor), ``schemas`` (output models),
``proc`` (tree-kill), ``job_errors`` (shared rejection error). Never imports
``minizinc``, ``runtime``, ``server``, or ``jobs``.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from subprocess import Popen
from typing import cast
from uuid import uuid4

from ..job_errors import JobRejectedError
from ..proc import terminate_process_tree as _terminate_process_tree
from ..schemas import (
    _CPSAT_RESULT_BEARING_STATES,
    CpsatJobState,
    CpsatPythonJobStatus,
    CpsatPythonResult,
    cpsat_job_state_for_result,
)

# These are package-internal helpers not promoted to the public API.
# noinspection PyProtectedMember
from .core import (
    DEFAULT_PYEXEC_TIMEOUT_MS,
    _validate_script_path,
    run_cpsat_python,
    run_cpsat_python_file,
)

_TERMINAL_STATES: frozenset[CpsatJobState] = cast(
    "frozenset[CpsatJobState]",
    frozenset({"succeeded", "failed", "timeout", "cancelled"}),
)


@dataclass(frozen=True)
class _CpsatJobRequest:
    """Immutable per-job parameters; kind discriminates source vs. file path."""

    source: str | None
    script_path: Path | None
    timeout_ms: int

    @property
    def is_file(self) -> bool:
        return self.script_path is not None


@dataclass
class _CpsatJobRecord:
    """Mutable per-job state, guarded by the registry lock."""

    job_id: str
    request: _CpsatJobRequest
    submitted_at_ms: int
    state: CpsatJobState
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    elapsed_ms: int | None = None
    result: CpsatPythonResult | None = None
    message: str | None = None
    handle: Popen[str] | None = None
    future: Future[None] | None = None
    cancel_requested: bool = False


def _now_ms() -> int:
    return int(time.time() * 1000)


def _exception_summary(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


class CpsatJobRegistry:
    """A bounded, single-owned registry of background CP-SAT Python jobs.

    Mirrors ``JobRegistry`` (MiniZinc) in structure and contract. Supports two
    submission flavors:
    - ``submit_source`` — inline Python source (same as ``run_cpsat_python``).
    - ``submit_file`` — local script path (same as ``run_cpsat_python_file``).

    ``get`` / ``list`` / ``cancel`` / ``shutdown`` are kind-agnostic. The
    result-presence invariant ``result present ⇔ state ∈ {succeeded, timeout}``
    is enforced by ``CpsatPythonJobStatus``'s model validator (D3). Cancel
    post-run checks ``cancel_requested`` and overrides the executor's ``error``
    result with ``cancelled`` (D4).
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
        self._records: dict[str, _CpsatJobRecord] = {}
        self._terminal_order: list[str] = []
        self._in_flight = 0
        self._executor = ThreadPoolExecutor(
            max_workers=max_running_jobs, thread_name_prefix="cpsat-job"
        )

    def submit_source(
        self,
        source: str,
        *,
        timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    ) -> str:
        """Admit an inline CP-SAT source as a background job; return ``job_id``.

        Validates ``timeout_ms`` up front (positive gate), then admits under the
        lock. Returns immediately; raises ``ValueError`` on bad args or
        ``JobRejectedError`` when the bounded queue is full.
        """
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        request = _CpsatJobRequest(source=source, script_path=None, timeout_ms=timeout_ms)
        with self._lock:
            if self._in_flight >= self._max_running + self._max_queued:
                raise JobRejectedError(self._queue_full_message())
            return self._admit_locked(request)

    def submit_file(
        self,
        script_path: Path,
        *,
        timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    ) -> str:
        """Admit a CP-SAT script file as a background job; return ``job_id``.

        Validates ``timeout_ms`` AND the path (exists / regular file / non-empty /
        UTF-8) before admission so a bad path raises ``ValueError`` synchronously
        and no job record is created. Raises ``JobRejectedError`` when the queue is
        full.
        """
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        resolved = _validate_script_path(script_path)
        request = _CpsatJobRequest(source=None, script_path=resolved, timeout_ms=timeout_ms)
        with self._lock:
            if self._in_flight >= self._max_running + self._max_queued:
                raise JobRejectedError(self._queue_full_message())
            return self._admit_locked(request)

    def get(self, job_id: str) -> CpsatPythonJobStatus:
        with self._lock:
            return self._to_status(self._require_record(job_id))

    def list(self) -> list[CpsatPythonJobStatus]:
        with self._lock:
            return [self._to_status(record) for record in self._records.values()]

    def cancel(self, job_id: str) -> CpsatPythonJobStatus:
        """Cancel a job: drop it if still queued, else terminate its process tree.

        A no-op on an already-terminal job. Mirrors ``JobRegistry.cancel``.
        """
        with self._lock:
            record = self._require_record(job_id)
            if record.state in _TERMINAL_STATES:
                return self._to_status(record)
            record.cancel_requested = True
            future = record.future
            handle = record.handle
        if future is not None and future.cancel():
            with self._lock:
                self._finalize(record, "cancelled", None, "Cancelled before start")
                return self._to_status(record)
        if handle is not None:
            _terminate_process_tree(handle)
        with self._lock:
            return self._to_status(record)

    def shutdown(self) -> None:
        """Terminate running children and tear down the worker pool (lifespan exit)."""
        with self._lock:
            for record in self._records.values():
                if record.state not in _TERMINAL_STATES:
                    record.cancel_requested = True
            records = list(self._records.values())
        for record in records:
            future = record.future
            if future is not None and future.cancel():
                with self._lock:
                    self._finalize(record, "cancelled", None, "Cancelled at shutdown")
        with self._lock:
            handles = [
                cast("Popen[str]", r.handle)
                for r in self._records.values()
                if r.handle is not None and r.state not in _TERMINAL_STATES
            ]
        for handle in handles:
            _terminate_process_tree(handle)
        self._executor.shutdown(wait=True, cancel_futures=True)

    # --- internals (assume the caller holds the lock unless noted) -------------

    def _queue_full_message(self) -> str:
        return (
            f"CP-SAT job queue is full "
            f"({self._max_running} running + {self._max_queued} queued). "
            "Retry once a running job finishes."
        )

    def _admit_locked(self, request: _CpsatJobRequest) -> str:
        job_id = uuid4().hex
        now = _now_ms()
        runs_now = self._in_flight < self._max_running
        record = _CpsatJobRecord(
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

    def _require_record(self, job_id: str) -> _CpsatJobRecord:
        record = self._records.get(job_id)
        if record is None:
            raise ValueError(f"unknown job_id: {job_id}")
        return record

    @staticmethod
    def _to_status(record: _CpsatJobRecord) -> CpsatPythonJobStatus:
        if record.state in _TERMINAL_STATES:
            elapsed_ms = record.elapsed_ms
        elif record.started_at_ms is not None:
            elapsed_ms = max(_now_ms() - record.started_at_ms, 0)
        else:
            elapsed_ms = None
        return CpsatPythonJobStatus(
            job_id=record.job_id,
            state=record.state,
            timeout_ms=record.request.timeout_ms,
            submitted_at_ms=record.submitted_at_ms,
            started_at_ms=record.started_at_ms,
            finished_at_ms=record.finished_at_ms,
            elapsed_ms=elapsed_ms,
            result=record.result,
            message=record.message,
        )

    def _finalize(
        self,
        record: _CpsatJobRecord,
        state: CpsatJobState,
        result: CpsatPythonResult | None,
        message: str | None,
    ) -> None:
        if record.state in _TERMINAL_STATES:
            return
        now = _now_ms()
        record.state = state
        record.finished_at_ms = now
        if record.started_at_ms is not None:
            record.elapsed_ms = max(now - record.started_at_ms, 0)
        record.result = result if state in _CPSAT_RESULT_BEARING_STATES else None
        record.message = message
        self._in_flight -= 1
        self._terminal_order.append(record.job_id)
        self._evict_terminal_overflow()

    def _evict_terminal_overflow(self) -> None:
        while len(self._terminal_order) > self._max_retained_terminal:
            oldest = self._terminal_order.pop(0)
            self._records.pop(oldest, None)

    def _on_start(self, job_id: str, proc: Popen[str]) -> None:
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                cancel_now = True
            else:
                record.handle = proc
                cancel_now = record.cancel_requested
        if cancel_now:
            _terminate_process_tree(proc)

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return
            request = record.request
            record.state = "running"
            if record.started_at_ms is None:
                record.started_at_ms = _now_ms()
        try:
            if request.is_file:
                assert request.script_path is not None
                result = run_cpsat_python_file(
                    request.script_path,
                    timeout_ms=request.timeout_ms,
                    on_start=lambda proc: self._on_start(job_id, proc),
                )
            else:
                assert request.source is not None
                result = run_cpsat_python(
                    request.source,
                    timeout_ms=request.timeout_ms,
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
                self._finalize(record, cpsat_job_state_for_result(result), result, None)
