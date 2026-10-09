"""进程内后台任务（2026-09-22 数据更新异步化）。

为什么要有它：``h_dataset_replace`` 的重管道（xlsx 解析 → 合并 → 装载 → 快照）
在 29 万行的真实文件上要跑几分钟，全部塞在 HTTP 请求里有两个真实后果——公司
内网代理掐长连接（客户端永远等不到响应），且旧 UI 只有一条 4 秒就消失的 toast
（真机复盘：作者完全无从得知成功还是失败，最终确认导入根本没发生）。

契约：
- 同一 (reportId, datasetId) 同时只允许一个 running 任务（``active_for`` 把关，
  重复发起 409）——两个并发写同一张 SQLite 表没有意义且危险。
- 任务**只在内存**：server 重启即失。轮询打不到任务返回 404，客户端必须如实
  说「服务已重启，请检查行数确认数据状态」，绝不假装成功。
- ``work(progress)`` 在守护线程执行；异常整体变成 job 的 error 字符串（BadRequest
  的 str 就是可行动文案），数据侧保证 fail-closed（管道自身零改动语义不变）。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_KEEP_FINISHED = 40


class JobConflict(Exception):
    """同一 (reportId, datasetId) 已有 running 任务。

    ``create`` 在锁内做「查重 + 登记」原子步——HTTP 层的 ``active_for`` 预检
    （先于读 body 的快速 409）与 ``create`` 之间存在 TOCTOU 窗口，两个并发
    async=1 请求可以同时通过预检、各起一个线程写同一张 SQLite 表。这里兜底
    第二个：携带既有 jobId，端点据此回 409。"""


def create(kind: str, report_id: str, dataset_id: str) -> str:
    job_id = "job-" + uuid.uuid4().hex[:12]
    with _LOCK:
        for job in _JOBS.values():
            if job["status"] == "running" and job["reportId"] == report_id \
                    and job["datasetId"] == dataset_id:
                raise JobConflict(job["id"])
        _JOBS[job_id] = {
            "id": job_id, "kind": kind,
            "reportId": report_id, "datasetId": dataset_id,
            "status": "running", "phase": "queued",
            "startedAt": time.time(), "finishedAt": None,
            "result": None, "error": None,
        }
    return job_id


def active_for(report_id: str, dataset_id: str) -> str | None:
    with _LOCK:
        for job in _JOBS.values():
            if job["status"] == "running" and job["reportId"] == report_id \
                    and job["datasetId"] == dataset_id:
                return job["id"]
    return None


def run(job_id: str, work: Callable[[Callable[[str], None]], dict]) -> None:
    """在守护线程执行 ``work(progress)``；结果/异常写回注册表。"""

    def _execute() -> None:
        def progress(label: str) -> None:
            set_phase(job_id, label)

        try:
            result = work(progress)
            with _LOCK:
                job = _JOBS[job_id]
                job["status"] = "done"
                job["phase"] = "done"
                job["result"] = result
                job["finishedAt"] = time.time()
        except Exception as exc:  # noqa: BLE001 — 任务边界必须兜住一切，错误进轮询
            with _LOCK:
                job = _JOBS[job_id]
                job["status"] = "error"
                job["phase"] = "error"
                job["error"] = str(exc) or exc.__class__.__name__
                job["finishedAt"] = time.time()
        finally:
            _prune()

    threading.Thread(target=_execute, daemon=True, name=f"lrm-job-{job_id}").start()


def set_phase(job_id: str, phase: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job["phase"] = phase


def get(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job is not None else None


def _prune() -> None:
    """终态任务只留最近 N 条（迟到的轮询还能查到，注册表不无限涨）。"""
    with _LOCK:
        finished = sorted(
            ((key, job.get("finishedAt") or 0.0) for key, job in _JOBS.items()
             if job["status"] != "running"),
            key=lambda pair: pair[1],
        )
        for key, _ in finished[:-_KEEP_FINISHED] if len(finished) > _KEEP_FINISHED else []:
            _JOBS.pop(key, None)
