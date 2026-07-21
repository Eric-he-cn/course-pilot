from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor

from .service import KnowledgeService


class KnowledgeJobWorker:
    """Small local worker with a bounded in-memory execution queue.

    The durable jobs table is the source of truth.  The in-memory executor only
    schedules queued rows; it can be discarded and reconstructed on restart.
    """

    def __init__(self, service: KnowledgeService, *, workers: int, queue_capacity: int) -> None:
        self._service = service
        self._capacity = max(workers, queue_capacity)
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="coursepilot-jobs")
        self._lock = threading.Lock()
        self._futures: dict[str, Future[object]] = {}
        self._accepting = False

    def start(self) -> None:
        with self._lock:
            if self._accepting:
                return
            self._accepting = True
        for job_id in self._service.recover_jobs_after_restart():
            self.submit(job_id)

    def submit(self, job_id: str) -> bool:
        with self._lock:
            if not self._accepting or len(self._futures) >= self._capacity:
                rejected = True
            else:
                rejected = False
                future = self._executor.submit(self._service.run_job, job_id=job_id)
                future.add_done_callback(lambda _future, current_id=job_id: self._finished(current_id))
                self._futures[job_id] = future
        if rejected:
            self._service.reject_queued_job(job_id=job_id, reason="后台任务队列已满或服务正在关闭；请稍后重新发起。")
            return False
        return True

    def _finished(self, job_id: str) -> None:
        with self._lock:
            self._futures.pop(job_id, None)

    def shutdown(self) -> None:
        with self._lock:
            self._accepting = False
            pending = list(self._futures.items())
        for job_id, future in pending:
            if future.cancel():
                self._service.reject_queued_job(job_id=job_id, reason="应用关闭前任务未执行；请重新发起。")
        self._executor.shutdown(wait=False, cancel_futures=True)
