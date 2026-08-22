"""Background job runner: one worker thread, polled by the UI.

Stages take anywhere from seconds (a brief) to minutes (nine languages), so the
browser cannot wait on a request. Work is queued, run on a worker thread, and
the UI polls for status and log lines.

Single worker by design. The pipeline writes to a shared cache, an append-only
ledger and a translation cache; running two jobs concurrently would interleave
their log output and race on the same article directory for no real gain, since
the slow parts are rate-limited network calls anyway.
"""
from __future__ import annotations

import io
import queue
import threading
import time
import traceback
import uuid
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

MAX_LOG = 400


@dataclass
class Job:
    id: str
    kind: str
    label: str
    status: str = "queued"          # queued | running | done | failed | waiting_writer
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    log: list[str] = field(default_factory=list)
    result: Any = None
    error: str = ""
    packets: list[str] = field(default_factory=list)

    def dict(self, *, with_log: bool = True) -> dict:
        d = asdict(self)
        if not with_log:
            d["log"] = d["log"][-1:]
        return d


class _Tee(io.TextIOBase):
    """Capture pipeline log output into the job while still printing it."""

    def __init__(self, job: Job, real: io.TextIOBase):
        self.job, self.real, self._buf = job, real, ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.job.log.append(line.rstrip())
                del self.job.log[:-MAX_LOG]
        try:
            self.real.write(s)
        except Exception:
            pass
        return len(s)

    def flush(self) -> None:
        try:
            self.real.flush()
        except Exception:
            pass


class Runner:
    def __init__(self) -> None:
        self._q: "queue.Queue[tuple[Job, Callable[[Job], Any]]]" = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="studio-worker")
        self._thread.start()

    def submit(self, kind: str, label: str, fn: Callable[[Job], Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label)
        with self._lock:
            self._jobs[job.id] = job
        self._q.put((job, fn))
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 25) -> list[Job]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)
        return jobs[:limit]

    def _loop(self) -> None:
        import sys
        while True:
            job, fn = self._q.get()
            job.status, job.started = "running", time.time()
            tee_out = _Tee(job, sys.__stdout__)
            try:
                with redirect_stdout(tee_out), redirect_stderr(tee_out):
                    job.result = fn(job)
                job.status = "done"
            except Exception as exc:
                # A paused writer is an expected state, not a failure: the job
                # stops with instructions rather than an error.
                if exc.__class__.__name__ == "WriterPending":
                    job.status = "waiting_writer"
                    job.packets = list(getattr(exc, "packets", []))
                    job.log.append(str(exc))
                else:
                    job.status = "failed"
                    job.error = f"{exc.__class__.__name__}: {exc}"
                    job.log.append(job.error)
                    for line in traceback.format_exc().splitlines()[-6:]:
                        job.log.append(line)
            finally:
                job.finished = time.time()
                self._q.task_done()


RUNNER = Runner()
