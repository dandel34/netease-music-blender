# -*- coding: utf-8 -*-
"""后台任务：网络请求放到子线程，主线程用定时器收集结果。

Blender 的界面只在主线程刷新，直接在主线程里发 HTTP 会把界面卡住十几秒，
所以所有请求都走这里：``submit()`` 起线程，``drain()`` 在主线程定时器里
取回已完成的任务并调用回调（回调里可以安全地读写 bpy 属性）。
"""

from __future__ import annotations

import threading
import traceback

from . import utils

__all__ = ["Job", "submit", "drain", "busy", "cancel_all", "status_text", "clear_log"]


class Job:
    def __init__(self, job_id: int, label: str, fn, on_done=None, on_error=None):
        self.id = job_id
        self.label = label
        self.fn = fn
        self.on_done = on_done
        self.on_error = on_error
        self.result = None
        self.error = None
        self.finished = False
        self.thread = None

    def run(self):
        try:
            self.result = self.fn()
        except BaseException as exc:  # noqa: BLE001 - 线程里必须兜住所有异常
            self.error = exc
            self.traceback = traceback.format_exc()
        finally:
            self.finished = True


_lock = threading.Lock()
_running = {}      # id -> Job（未结束）
_finished = []     # 已完成、等待主线程处理
_counter = 0
_log = []          # 最近的任务记录，显示在面板上


def submit(label: str, fn, on_done=None, on_error=None) -> int:
    """在子线程里执行 ``fn``；返回任务 id。"""
    global _counter
    with _lock:
        _counter += 1
        job = Job(_counter, label, fn, on_done, on_error)
        _running[job.id] = job
    thread = threading.Thread(target=job.run, name="netease-%s" % label, daemon=True)
    job.thread = thread
    thread.start()
    utils.log_debug("启动任务", label)
    return job.id


def drain() -> int:
    """（主线程调用）把已完成任务的结果交给回调，返回处理数量。"""
    with _lock:
        ready = [job for job in _running.values() if job.finished]
        for job in ready:
            _running.pop(job.id, None)
        still = [job for job in _running.values()]
    for job in ready:
        if job.error is not None:
            _log.append((job.label, "失败：%s" % job.error))
            utils.log("任务失败：%s → %s" % (job.label, job.error))
            utils.log_debug(getattr(job, "traceback", ""))
            if job.on_error:
                try:
                    job.on_error(job.error)
                except Exception:  # noqa: BLE001
                    utils.log("错误回调自身出错：\n%s" % traceback.format_exc())
        else:
            _log.append((job.label, "完成"))
            if job.on_done:
                try:
                    job.on_done(job.result)
                except Exception:  # noqa: BLE001
                    utils.log("完成回调自身出错：\n%s" % traceback.format_exc())
    del _log[:-30]
    return len(ready)


def busy() -> bool:
    with _lock:
        return bool(_running)


def running_labels() -> list:
    with _lock:
        return [job.label for job in _running.values()]


def status_text() -> str:
    labels = running_labels()
    if not labels:
        return ""
    return "正在处理：%s" % "、".join(labels[:3])


def cancel_all():
    """标记取消：线程无法强杀，但回调不会再执行。"""
    with _lock:
        for job in _running.values():
            job.on_done = None
            job.on_error = None
        _running.clear()


def log_entries():
    return list(_log)


def clear_log():
    del _log[:]
