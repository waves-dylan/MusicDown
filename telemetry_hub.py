# -*- coding: utf-8 -*-
"""
telemetry_hub.py - 全场景过程遥测与进度事件中枢 (Project Telemetry)

核心职责：
1. 定义标准化的任务生命周期与遥测数据模型 (TaskStage, TaskTelemetry)；
2. 线程安全的单例事件总线 TelemetryEventHub，接收高频下载/搜索进度更新；
3. EWMA 瞬时速率平滑算法与 ETA 预估计算；
4. 终端 Rich 渲染器 (RichTerminalRenderer)，复刻并增强多任务细线进度条；
5. Qt 信号节流适配桥梁 (QtTelemetryBridge)，为 GUI 渲染提供 10 FPS 安全定频。
"""

import os
import sys
import time
import math
import threading
from enum import Enum
from typing import Dict, List, Optional, Callable, Any

try:
    from rich.progress import (
        Progress,
        SpinnerColumn,
        TextColumn,
        BarColumn,
        TaskProgressColumn,
        DownloadColumn,
        TransferSpeedColumn,
        TimeRemainingColumn,
    )
    from rich.console import Console
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    Progress = None
    Console = None

try:
    from PyQt5.QtCore import QObject, pyqtSignal, QTimer
    PYQT_AVAILABLE = True
except ImportError:
    PYQT_AVAILABLE = False
    QObject = object
    pyqtSignal = None
    QTimer = None


class TaskStage(Enum):
    """任务生命周期阶段枚举"""
    QUEUED = "queued"                  # 排队等待中
    PREPARING = "preparing"            # 准备握手 / 解析直链
    SEARCHING = "searching"            # 检索音源与分页
    DOWNLOADING = "downloading"        # 正在流式下载音频
    PROCESSING_LYRICS = "lyrics"       # 双语歌词拉取与时序对齐
    REPAIRING_METADATA = "metadata"    # 探测真实格式 / 写入 ID3 标签与封面
    SUCCESS = "success"                # 任务全部成功
    FAILED = "failed"                  # 发生异常失败


class TelemetryUtils:
    """遥测常用数据格式化工具"""

    @staticmethod
    def format_bytes(num_bytes: Optional[int]) -> str:
        """把字节大小转为人类可读文本，如 16.8 MB"""
        if num_bytes is None or num_bytes < 0:
            return "0 B"
        if num_bytes == 0:
            return "0 B"
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        idx = min(int(math.log(max(1, num_bytes), 1024)), len(units) - 1)
        val = num_bytes / (1024 ** idx)
        return f"{val:.1f} {units[idx]}" if idx > 0 else f"{int(val)} B"

    @staticmethod
    def format_speed(bps: float) -> str:
        """把字节/秒速率转为人类可读格式，如 3.5 MB/s"""
        if bps <= 0:
            return "0 B/s"
        return f"{TelemetryUtils.format_bytes(int(bps))}/s"

    @staticmethod
    def format_time(seconds: Optional[float]) -> str:
        """把秒数转为 mm:ss 或 hh:mm:ss 格式"""
        if seconds is None or seconds < 0 or math.isinf(seconds) or math.isnan(seconds):
            return "--:--"
        s = int(round(seconds))
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"


class RateSmoother:
    """基于 EWMA (指数加权移动平均) 的下载速率与剩余时间平滑估算器"""

    def __init__(self, alpha: float = 0.25):
        self.alpha = max(0.05, min(0.95, alpha))
        self.last_ts: float = 0.0
        self.last_bytes: int = 0
        self.speed_bps: float = 0.0

    def update(self, current_bytes: int, now: Optional[float] = None) -> float:
        now = now or time.time()
        if self.last_ts == 0.0:
            self.last_ts = now
            self.last_bytes = current_bytes
            return 0.0

        dt = now - self.last_ts
        if dt < 0.1:  # 采样间隔过短跳过
            return self.speed_bps

        delta_bytes = max(0, current_bytes - self.last_bytes)
        if delta_bytes == 0:
            if dt > 1.0:  # 超过 1 秒无新字节到达，迅速衰减速率
                self.speed_bps = max(0.0, self.speed_bps * 0.4)
                if self.speed_bps < 1024:
                    self.speed_bps = 0.0
            return self.speed_bps

        instant_speed = delta_bytes / dt

        if self.speed_bps <= 0:
            self.speed_bps = instant_speed
        else:
            self.speed_bps = self.alpha * instant_speed + (1.0 - self.alpha) * self.speed_bps

        self.last_ts = now
        self.last_bytes = current_bytes
        return self.speed_bps


class TaskTelemetry:
    """单一任务的完整遥测状态模型"""

    def __init__(
        self,
        task_id: str,
        category: str = "download",  # "search" 或 "download"
        title: str = "未知任务",
        subtitle: str = "",
        source: str = "MusicClient",
        total_bytes: int = 0,
        parent_id: Optional[str] = None,
    ):
        self.task_id = str(task_id)
        self.category = category
        self.title = title
        self.subtitle = subtitle
        self.source = source
        self.parent_id = parent_id

        self.stage: TaskStage = TaskStage.QUEUED
        self.completed_bytes: int = 0
        self.total_bytes: int = max(0, int(total_bytes or 0))

        self.speed_bps: float = 0.0
        self.eta_seconds: Optional[float] = None
        self.status_text: str = "等待中"
        self.error_message: str = ""

        self.created_at: float = time.time()
        self.updated_at: float = self.created_at
        self.finished_at: Optional[float] = None

        self._smoother = RateSmoother()
        self._dirty = True

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 100.0 if self.stage == TaskStage.SUCCESS else 0.0
        return min(100.0, max(0.0, (self.completed_bytes / self.total_bytes) * 100.0))

    @property
    def is_finished(self) -> bool:
        return self.stage in (TaskStage.SUCCESS, TaskStage.FAILED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "category": self.category,
            "title": self.title,
            "subtitle": self.subtitle,
            "source": self.source,
            "parent_id": self.parent_id,
            "stage": self.stage.value,
            "stage_display": self.stage_display(),
            "completed_bytes": self.completed_bytes,
            "total_bytes": self.total_bytes,
            "percent": self.percent,
            "speed_bps": self.speed_bps,
            "speed_display": TelemetryUtils.format_speed(self.speed_bps),
            "eta_seconds": self.eta_seconds,
            "eta_display": TelemetryUtils.format_time(self.eta_seconds),
            "status_text": self.status_text,
            "error_message": self.error_message,
            "is_finished": self.is_finished,
            "elapsed_seconds": (self.finished_at or time.time()) - self.created_at,
        }

    def stage_display(self) -> str:
        mapping = {
            TaskStage.QUEUED: "排队中",
            TaskStage.PREPARING: "准备中",
            TaskStage.SEARCHING: "搜索中",
            TaskStage.DOWNLOADING: "下载中",
            TaskStage.PROCESSING_LYRICS: "处理歌词",
            TaskStage.REPAIRING_METADATA: "写入标签",
            TaskStage.SUCCESS: "已完成",
            TaskStage.FAILED: "失败",
        }
        return mapping.get(self.stage, self.stage.value)


class RichTerminalRenderer:
    """基于 rich.progress 的终端多任务细线进度条渲染器（复刻 CLI 原生惊艳效果）"""

    def __init__(self):
        self._progress: Optional[Progress] = None
        self._task_map: Dict[str, int] = {}  # task_id -> rich_task_id
        self._overall_task_id: Optional[int] = None
        self._lock = threading.Lock()
        self._is_active = False

    def is_terminal_attached(self) -> bool:
        """检查是否有终端输出环境，若完全脱离终端则静默避免无谓计算"""
        if not RICH_AVAILABLE:
            return False
        try:
            return bool(sys.stdout and (sys.stdout.isatty() or os.environ.get("FORCE_COLOR") or os.environ.get("TERM")))
        except Exception:
            return False

    def start(self, title: str = "Task Execution", total_items: int = 0):
        if not RICH_AVAILABLE or not self.is_terminal_attached():
            return
        with self._lock:
            if self._is_active and self._progress:
                return
            columns = [
                SpinnerColumn(spinner_name="dots", style="bold cyan"),
                TextColumn("[bold cyan]{task.fields[source_tag]}[/]"),
                TextColumn("{task.description}"),
                BarColumn(bar_width=32, style="grey23", complete_style="magenta", finished_style="green"),
                TaskProgressColumn(),
                DownloadColumn(binary_units=True),
                TransferSpeedColumn(),
                TimeRemainingColumn(compact=True, elapsed_when_finished=True),
            ]
            try:
                self._progress = Progress(*columns, refresh_per_second=15, expand=True)
                self._progress.start()
                self._is_active = True
                self._task_map.clear()

                # 添加总览条
                self._overall_task_id = self._progress.add_task(
                    f"{title} >>> Completed (0/{total_items})",
                    total=max(1, total_items),
                    source_tag="Overall >>>",
                )
            except Exception:
                self._progress = None
                self._is_active = False

    def add_task(self, task: TaskTelemetry):
        if not self._is_active or not self._progress:
            return
        with self._lock:
            source_tag = f"{task.source} >>>"
            desc = f"{task.title} ({task.stage_display()})"
            rich_id = self._progress.add_task(
                desc,
                total=task.total_bytes if task.total_bytes > 0 else None,
                completed=task.completed_bytes,
                source_tag=source_tag,
            )
            self._task_map[task.task_id] = rich_id

    def update_task(self, task: TaskTelemetry):
        if not self._is_active or not self._progress:
            return
        with self._lock:
            rich_id = self._task_map.get(task.task_id)
            if rich_id is None:
                return

            desc = f"{task.title} ({task.status_text or task.stage_display()})"
            update_kwargs: Dict[str, Any] = {
                "description": desc,
                "completed": task.completed_bytes,
            }
            if task.total_bytes > 0:
                update_kwargs["total"] = task.total_bytes

            self._progress.update(rich_id, **update_kwargs)

    def advance_overall(self, completed_count: int, total_count: int, title: str = "Completed"):
        if not self._is_active or not self._progress or self._overall_task_id is None:
            return
        with self._lock:
            self._progress.update(
                self._overall_task_id,
                completed=completed_count,
                total=total_count,
                description=f"{title} >>> Completed ({completed_count}/{total_count})",
            )

    def stop(self):
        with self._lock:
            if self._progress and self._is_active:
                try:
                    self._progress.stop()
                except Exception:
                    pass
            self._progress = None
            self._is_active = False
            self._task_map.clear()
            self._overall_task_id = None


class TelemetryEventHub:
    """统一进度与遥测事件中枢（单例模式，线程安全）"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(TelemetryEventHub, cls).__new__(cls)
                cls._instance._init_hub()
            return cls._instance

    def _init_hub(self):
        self._tasks: Dict[str, TaskTelemetry] = {}
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._hub_lock = threading.RLock()
        self._terminal_renderer = RichTerminalRenderer()
        self._session_title = ""
        self._session_category = "download"
        self._total_expected_tasks = 0
        self._custom_overall: Optional[Dict[str, Any]] = None

    @classmethod
    def get_instance(cls) -> "TelemetryEventHub":
        return cls()

    def start_session(self, title: str = "任务执行中", total_items: int = 0, category: str = "download"):
        """开启一次新的批处理遥测会话（如批量搜索或批量下载）"""
        with self._hub_lock:
            self._tasks.clear()
            self._session_title = title
            self._session_category = category
            self._total_expected_tasks = total_items
            self._custom_overall = None
            self._terminal_renderer.start(title=title, total_items=total_items)

    def set_overall_progress(self, completed: int, total: int, percent: Optional[float] = None, badge_text: Optional[str] = None):
        """设置统一的全局进度（如搜索过程的分页细粒度单元），作为单一事实来源防止多头争抢"""
        with self._hub_lock:
            if total <= 0:
                self._custom_overall = None
                return
            pct = percent if percent is not None else min(100.0, max(0.0, (completed / total) * 100.0))
            if not badge_text:
                badge_text = f"{int(round(pct))}% 已检索" if total > 20 else f"{completed}/{total} 完成"
            self._custom_overall = {
                "completed": completed,
                "total": total,
                "percent": pct,
                "badge_text": badge_text,
            }

    def add_task(
        self,
        task_id: str,
        category: str = "download",
        title: str = "未知任务",
        subtitle: str = "",
        source: str = "MusicClient",
        total_bytes: int = 0,
        parent_id: Optional[str] = None,
    ) -> TaskTelemetry:
        with self._hub_lock:
            task = TaskTelemetry(
                task_id=task_id,
                category=category,
                title=title,
                subtitle=subtitle,
                source=source,
                total_bytes=total_bytes,
                parent_id=parent_id,
            )
            self._tasks[task_id] = task
            self._terminal_renderer.add_task(task)
            self._notify_listeners(task_id)
            return task

    def update_task_progress(
        self,
        task_id: str,
        advance_bytes: int = 0,
        completed_bytes: Optional[int] = None,
        total_bytes: Optional[int] = None,
        stage: Optional[TaskStage] = None,
        status_text: Optional[str] = None,
    ):
        """高频流式 chunk 更新已下载字节与阶段"""
        with self._hub_lock:
            task = self._tasks.get(task_id)
            if not task:
                return

            now = time.time()
            if total_bytes is not None and total_bytes > 0:
                task.total_bytes = total_bytes

            if completed_bytes is not None:
                task.completed_bytes = completed_bytes
            elif advance_bytes > 0:
                task.completed_bytes += advance_bytes

            if stage is not None:
                task.stage = stage

            # 更新平滑速率与 ETA
            speed = task._smoother.update(task.completed_bytes, now=now)
            task.speed_bps = speed
            if speed > 1024 and task.total_bytes > task.completed_bytes:
                task.eta_seconds = (task.total_bytes - task.completed_bytes) / speed
            else:
                task.eta_seconds = None

            if status_text:
                task.status_text = status_text
            elif task.stage == TaskStage.DOWNLOADING:
                c_str = TelemetryUtils.format_bytes(task.completed_bytes)
                t_str = TelemetryUtils.format_bytes(task.total_bytes) if task.total_bytes > 0 else "? MB"
                task.status_text = f"Downloading: {c_str}/{t_str}"

            task.updated_at = now
            task._dirty = True

            self._terminal_renderer.update_task(task)
            self._notify_listeners(task_id)

    def set_task_stage(
        self,
        task_id: str,
        stage: TaskStage,
        status_text: Optional[str] = None,
        error_message: str = "",
    ):
        with self._hub_lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            task.stage = stage
            if error_message:
                task.error_message = error_message
            if status_text:
                task.status_text = status_text
            else:
                task.status_text = task.stage_display()

            if stage != TaskStage.DOWNLOADING:
                task.speed_bps = 0.0
                task.eta_seconds = None

            if stage == TaskStage.SUCCESS:
                task.finished_at = time.time()
                if task.total_bytes > 0:
                    task.completed_bytes = task.total_bytes
            elif stage == TaskStage.FAILED:
                task.finished_at = time.time()

            task.updated_at = time.time()
            task._dirty = True

            self._terminal_renderer.update_task(task)
            self._check_and_update_overall()
            self._notify_listeners(task_id)

    def _check_and_update_overall(self):
        finished_count = sum(1 for t in self._tasks.values() if t.is_finished)
        total_count = max(self._total_expected_tasks, len(self._tasks))
        self._terminal_renderer.advance_overall(finished_count, total_count, title=self._session_title or "Overall")

    def end_session(self):
        with self._hub_lock:
            self._terminal_renderer.stop()

    def get_snapshot(self) -> Dict[str, Any]:
        """获取当前会话所有任务的瞬时快照字典"""
        with self._hub_lock:
            tasks_list = [t.to_dict() for t in self._tasks.values()]
            total_tasks = max(self._total_expected_tasks, len(tasks_list))
            finished_tasks = sum(1 for t in tasks_list if t["is_finished"])
            success_tasks = sum(1 for t in tasks_list if t["stage"] == TaskStage.SUCCESS.value)
            failed_tasks = sum(1 for t in tasks_list if t["stage"] == TaskStage.FAILED.value)

            total_bytes = sum(t["total_bytes"] for t in tasks_list)
            completed_bytes = sum(t["completed_bytes"] for t in tasks_list)
            overall_speed = sum(t["speed_bps"] for t in tasks_list if not t["is_finished"])

            overall_eta = None
            if overall_speed > 1024 and total_bytes > completed_bytes:
                overall_eta = (total_bytes - completed_bytes) / overall_speed

            if self._custom_overall is not None:
                overall_percent = self._custom_overall["percent"]
                badge_text = self._custom_overall["badge_text"]
            else:
                overall_percent = 0.0
                if total_tasks > 0:
                    overall_percent = (finished_tasks / total_tasks) * 100.0
                badge_text = f"{finished_tasks}/{total_tasks} 完成"

            return {
                "title": self._session_title,
                "category": self._session_category,
                "badge_text": badge_text,
                "total_tasks": total_tasks,
                "finished_tasks": finished_tasks,
                "success_tasks": success_tasks,
                "failed_tasks": failed_tasks,
                "overall_percent": overall_percent,
                "total_bytes": total_bytes,
                "completed_bytes": completed_bytes,
                "overall_speed_bps": overall_speed,
                "overall_speed_display": TelemetryUtils.format_speed(overall_speed),
                "overall_eta_seconds": overall_eta,
                "overall_eta_display": TelemetryUtils.format_time(overall_eta),
                "tasks": tasks_list,
            }

    def register_listener(self, callback: Callable[[Dict[str, Any]], None]):
        with self._hub_lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def unregister_listener(self, callback: Callable[[Dict[str, Any]], None]):
        with self._hub_lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _notify_listeners(self, task_id: str):
        # 仅分发事件标识，UI 桥接器会通过节流阀批量 pull 快照
        pass


if PYQT_AVAILABLE:
    class QtTelemetryBridge(QObject):
        """Qt 节流适配器：使用 QTimer (10 FPS / 100ms) 批量拉取遥测快照，向 GUI 主线程安全发射信号"""

        batch_updated = pyqtSignal(dict)  # 发射整包遥测快照

        def __init__(self, parent=None, fps: int = 10):
            super().__init__(parent)
            self._hub = TelemetryEventHub.get_instance()
            self._timer = QTimer(self)
            self._interval_ms = max(50, int(1000 / max(1, fps)))
            self._timer.setInterval(self._interval_ms)
            self._timer.timeout.connect(self._on_timer_pulse)
            self._is_running = False

        def start(self):
            if not self._is_running:
                self._timer.start()
                self._is_running = True

        def stop(self):
            if self._is_running:
                self._timer.stop()
                self._is_running = False
                # 结束时最后派发一次以确保呈现最终态
                self._on_timer_pulse()

        def _on_timer_pulse(self):
            snapshot = self._hub.get_snapshot()
            self.batch_updated.emit(snapshot)
else:
    class QtTelemetryBridge:
        pass
