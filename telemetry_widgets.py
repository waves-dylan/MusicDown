# -*- coding: utf-8 -*-
"""
telemetry_widgets.py - PyQt5 现代化多任务过程监视器与卡片组件

包含：
1. TaskCardWidget: 单个并发任务的精美微缩卡片（单曲进度条、瞬时速率、ETA、状态胶囊）；
2. OverallDashboardWidget: 顶部总览看板（综合网速、总体进度、剩余时间）；
3. ModernProgressDialog: 替代 SimpleProgressDialog 的现代多任务流式下载与搜索监视器。
"""

import time
from typing import Dict, Optional, Any

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QColor, QPalette
from PyQt5.QtWidgets import (
    QDialog,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QFrame,
    QPushButton,
    QPlainTextEdit,
    QGraphicsDropShadowEffect,
)

from telemetry_hub import TelemetryEventHub, TaskStage, TelemetryUtils, QtTelemetryBridge


SOURCE_LABEL_MAP = {
    "netease": "网易云", "NeteaseMusicClient": "网易云",
    "qq": "QQ音乐", "QQMusicClient": "QQ音乐",
    "kugou": "酷狗", "KugouMusicClient": "酷狗",
    "kuwo": "酷我", "KuwoMusicClient": "酷我",
    "migu": "咪咕", "MiguMusicClient": "咪咕",
    "soda": "汽水音乐", "SodaMusicClient": "汽水音乐",
    "qianqian": "千千", "QianqianMusicClient": "千千",
    "5sing": "5sing", "FivesingMusicClient": "5sing",
    "apple": "Apple", "AppleMusicClient": "Apple",
    "spotify": "Spotify", "SpotifyMusicClient": "Spotify",
    "deezer": "Deezer", "DeezerMusicClient": "Deezer",
    "tidal": "TIDAL", "TidalMusicClient": "TIDAL",
    "qobuz": "Qobuz", "QobuzMusicClient": "Qobuz",
    "joox": "Joox", "JooxMusicClient": "Joox",
}


class TaskCardWidget(QFrame):
    """单任务微缩卡片控件（自适应进度、速率、大小与状态标签）"""

    def __init__(self, task_dict: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.task_id = task_dict.get("task_id", "")
        self._last_stage = None
        self._last_percent = -1.0
        self._last_speed = -1.0

        self.setObjectName("TaskCard")
        self.setStyleSheet("""
            QFrame#TaskCard {
                background-color: #ffffff;
                border: 1px solid #e5e9f0;
                border-radius: 6px;
                padding: 6px;
            }
            QFrame#TaskCard:hover {
                border-color: #0078d4;
                background-color: #fafbfc;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        # 顶行：标题、来源与状态 Badge
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        raw_source = task_dict.get("source", "Music")
        display_source = SOURCE_LABEL_MAP.get(raw_source, raw_source.replace("MusicClient", ""))
        self.source_label = QLabel(display_source)
        self.source_label.setStyleSheet("""
            QLabel {
                background-color: #f0f4f8;
                color: #005a9e;
                font-size: 8pt;
                font-weight: bold;
                padding: 1px 6px;
                border-radius: 3px;
            }
        """)
        top_row.addWidget(self.source_label)

        # 组合 歌名 - 歌手
        title = task_dict.get("title", "未知任务")
        subtitle = task_dict.get("subtitle", "")
        full_title = f"{title} - {subtitle}" if subtitle and subtitle not in title else title
        self.title_label = QLabel(full_title)
        self.title_label.setStyleSheet("font-size: 9.5pt; font-weight: bold; color: #2c3e50;")
        top_row.addWidget(self.title_label, 1)

        self.badge_label = QLabel("排队中")
        self.badge_label.setStyleSheet(self._badge_style("queued"))
        top_row.addWidget(self.badge_label)

        layout.addLayout(top_row)

        # 中行：细线圆角进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: none;
                border-radius: 3px;
                background-color: #edf2f7;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #d13438, stop:1 #e056fd);
                border-radius: 3px;
            }
        """)
        layout.addWidget(self.progress_bar)

        # 底行：数据统计指标（已下/总大小、瞬时速度、ETA）
        stat_row = QHBoxLayout()
        stat_row.setSpacing(12)

        self.size_label = QLabel("0 B / 0 B")
        self.size_label.setStyleSheet("font-size: 8.5pt; color: #5c6b73;")
        stat_row.addWidget(self.size_label)

        self.speed_label = QLabel("0 B/s")
        self.speed_label.setStyleSheet("font-size: 8.5pt; color: #0078d4; font-weight: 500;")
        stat_row.addWidget(self.speed_label)

        stat_row.addStretch(1)

        self.eta_label = QLabel("--:--")
        self.eta_label.setStyleSheet("font-size: 8.5pt; color: #8892b0;")
        stat_row.addWidget(self.eta_label)

        layout.addLayout(stat_row)

        self.update_data(task_dict)

    @staticmethod
    def _badge_style(stage: str) -> str:
        color_map = {
            "queued": ("#6c757d", "#f8f9fa"),
            "preparing": ("#4a69bd", "#ebf2fa"),
            "searching": ("#0a79df", "#e8f4fc"),
            "downloading": ("#d13438", "#fdeeed"),
            "lyrics": ("#8e44ad", "#f4ecf7"),
            "metadata": ("#d35400", "#fdf2e9"),
            "success": ("#27ae60", "#eafaf1"),
            "failed": ("#c0392b", "#fdedec"),
        }
        fg, bg = color_map.get(stage, ("#4a4a4a", "#eeeeee"))
        return f"""
            QLabel {{
                background-color: {bg};
                color: {fg};
                font-size: 8pt;
                font-weight: bold;
                padding: 2px 7px;
                border-radius: 4px;
                border: 1px solid {bg};
            }}
        """

    def update_data(self, t: Dict[str, Any]):
        stage = t.get("stage", "queued")
        percent = float(t.get("percent", 0.0))
        speed = float(t.get("speed_bps", 0.0))

        # 更新标题
        if self.title_label.text() != t.get("title", ""):
            self.title_label.setText(t.get("title", ""))

        # 更新状态 Badge
        if stage != self._last_stage:
            self.badge_label.setText(t.get("stage_display", "进行中"))
            self.badge_label.setStyleSheet(self._badge_style(stage))

            # 根据状态切换进度条颜色
            if stage == "success":
                self.progress_bar.setStyleSheet("""
                    QProgressBar { border: none; border-radius: 3px; background-color: #edf2f7; }
                    QProgressBar::chunk { background-color: #27ae60; border-radius: 3px; }
                """)
            elif stage in ("lyrics", "metadata"):
                self.progress_bar.setStyleSheet("""
                    QProgressBar { border: none; border-radius: 3px; background-color: #edf2f7; }
                    QProgressBar::chunk { background-color: #8e44ad; border-radius: 3px; }
                """)
            self._last_stage = stage

        # 更新进度条
        if abs(percent - self._last_percent) >= 0.2 or percent >= 100.0 or percent <= 0.0:
            self.progress_bar.setValue(int(percent * 10))
            self._last_percent = percent

        # 更新字节大小
        completed_str = TelemetryUtils.format_bytes(t.get("completed_bytes", 0))
        total_bytes = t.get("total_bytes", 0)
        total_str = TelemetryUtils.format_bytes(total_bytes) if total_bytes > 0 else "? MB"
        self.size_label.setText(f"{completed_str} / {total_str}")

        # 更新网速与剩余时间
        if stage == "downloading":
            self.speed_label.setText(t.get("speed_display", "0 B/s"))
            self.eta_label.setText(f"剩余 {t.get('eta_display', '--:--')}")
        elif stage == "success":
            self.speed_label.setText("✔ 完成")
            self.eta_label.setText(f"耗时 {TelemetryUtils.format_time(t.get('elapsed_seconds', 0))}")
        elif stage == "failed":
            self.speed_label.setText("✖ 失败")
            self.eta_label.setText(t.get("error_message", "错误")[:16])
        elif stage in ("lyrics", "metadata"):
            self.speed_label.setText(t.get("status_text", "处理中"))
            self.eta_label.setText("处理中…")
        else:
            self.speed_label.setText("准备中")
            self.eta_label.setText("--:--")


class OverallDashboardWidget(QFrame):
    """顶部仪表盘看板（总进度、综合速率、总剩余时间）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Dashboard")
        self.setStyleSheet("""
            QFrame#Dashboard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffffff, stop:1 #f8fafc);
                border: 1px solid #dce4ec;
                border-radius: 8px;
                padding: 10px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        # 标题与总计数
        top_row = QHBoxLayout()
        self.title_label = QLabel("正在执行任务")
        self.title_label.setStyleSheet("font-size: 11pt; font-weight: bold; color: #1a252f;")
        top_row.addWidget(self.title_label)

        top_row.addStretch()

        self.count_badge = QLabel("准备中")
        self.count_badge.setAlignment(Qt.AlignCenter)
        self.count_badge.setMinimumWidth(88)
        self.count_badge.setStyleSheet("""
            QLabel {
                background-color: #e3f2fd;
                color: #0d47a1;
                font-size: 8.5pt;
                font-weight: bold;
                padding: 2px 8px;
                border-radius: 4px;
            }
        """)
        top_row.addWidget(self.count_badge)

        layout.addLayout(top_row)

        # 总体进度条
        self.overall_bar = QProgressBar()
        self.overall_bar.setFixedHeight(8)
        self.overall_bar.setTextVisible(False)
        self.overall_bar.setRange(0, 1000)
        self.overall_bar.setValue(0)
        self.overall_bar.setStyleSheet("""
            QProgressBar {
                border: none;
                border-radius: 4px;
                background-color: #e2e8f0;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0078d4, stop:1 #00b4d8);
                border-radius: 4px;
            }
        """)
        layout.addWidget(self.overall_bar)

        # 三联度量数据栏
        metric_row = QHBoxLayout()
        metric_row.setSpacing(16)

        self.speed_metric = QLabel("⚡ 综合速率: 0 B/s")
        self.speed_metric.setStyleSheet("font-size: 9pt; color: #0078d4; font-weight: bold;")
        metric_row.addWidget(self.speed_metric)

        self.eta_metric = QLabel("⏳ 预计剩余: --:--")
        self.eta_metric.setStyleSheet("font-size: 9pt; color: #475569;")
        metric_row.addWidget(self.eta_metric)

        metric_row.addStretch()

        self.elapsed_metric = QLabel("⏱ 已用时: 0 秒")
        self.elapsed_metric.setStyleSheet("font-size: 9pt; color: #8a949e;")
        metric_row.addWidget(self.elapsed_metric)

        layout.addLayout(metric_row)

    def update_snapshot(self, snap: Dict[str, Any], elapsed_seconds: int):
        title = snap.get("title", "")
        if title and self.title_label.text() != title:
            self.title_label.setText(title)

        # 统一使用快照中由单一真相源生成的 badge_text 与 overall_percent
        badge_text = snap.get("badge_text", f"{snap.get('finished_tasks', 0)}/{snap.get('total_tasks', 0)} 完成")
        self.count_badge.setText(badge_text)

        percent = snap.get("overall_percent", 0.0)
        self.overall_bar.setValue(int(percent * 10))

        # 智能场景感知：搜索场景 vs 下载场景
        is_search = snap.get("category") == "search" or "搜索" in title
        if is_search:
            self.speed_metric.setText("🔍 音源并发检索中")
            self.eta_metric.setVisible(False)
        else:
            self.eta_metric.setVisible(True)
            speed_str = snap.get("overall_speed_display", "0 B/s")
            self.speed_metric.setText(f"⚡ 综合速率: {speed_str}")
            eta_str = snap.get("overall_eta_display", "--:--")
            self.eta_metric.setText(f"⏳ 预计剩余: {eta_str}")

        self.elapsed_metric.setText(f"⏱ 已用时: {elapsed_seconds} 秒")


class ModernProgressDialog(QDialog):
    """现代化多任务过程监视器（支持自由拉伸、流式卡片、瞬时网速、ETA、折叠日志）"""

    DEFAULT_WIDTH = 580
    DEFAULT_HEIGHT = 500
    LOG_HEIGHT = 160

    def __init__(self, title="任务执行中", save_dir=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        # 允许自由拉伸，并提供充足的默认视口尺寸
        self.resize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
        self.setMinimumSize(480, 360)
        self.setWindowFlags(
            Qt.Dialog
            | Qt.WindowTitleHint
            | Qt.WindowCloseButtonHint
            | Qt.WindowMinMaxButtonsHint
        )
        self.setWindowModality(Qt.ApplicationModal)

        self._start_time = time.time()
        self._cards: Dict[str, TaskCardWidget] = {}

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 14, 16, 14)
        main_layout.setSpacing(10)

        # 1. 顶部总览看板 (固定高度)
        self.dashboard = OverallDashboardWidget(self)
        main_layout.addWidget(self.dashboard, 0)

        # 2. 中部多任务流式卡片容器 (弹性拉伸，支持自适应滚动)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setMinimumHeight(150)
        self.scroll_area.setStyleSheet("""
            QScrollArea {
                background: transparent;
                border: none;
            }
            QScrollBar:vertical {
                border: none;
                background: #f1f5f9;
                width: 6px;
                border-radius: 3px;
            }
            QScrollBar::handle:vertical {
                background: #cbd5e1;
                border-radius: 3px;
            }
            QScrollBar::handle:vertical:hover {
                background: #94a3b8;
            }
        """)

        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 0, 4, 0)
        self.cards_layout.setSpacing(6)
        self.cards_layout.addStretch(1)
        self.scroll_area.setWidget(self.cards_container)

        # 重点：伸缩因子设为 1，窗口拉大时自动填充此区域
        main_layout.addWidget(self.scroll_area, 1)

        # 3. 详细调试日志文本框（默认折叠）
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(500)
        self.log_box.setFixedHeight(self.LOG_HEIGHT)
        self.log_box.setVisible(False)
        self.log_box.setStyleSheet("""
            QPlainTextEdit {
                font-family: Consolas, "Courier New", monospace;
                font-size: 8.5pt;
                color: #1e293b;
                background-color: #f8fafc;
                border: 1px solid #e2e8f0;
                border-radius: 5px;
                padding: 6px;
            }
        """)
        main_layout.addWidget(self.log_box, 0)

        # 4. 底部操作栏（始终稳定停靠，隔离全局 QSS 影响）
        bottom_bar = QWidget()
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(0, 2, 0, 0)
        bottom_layout.setSpacing(10)

        if save_dir:
            dir_label = QLabel(f"保存到：{save_dir}")
            dir_label.setStyleSheet("font-size: 8.5pt; color: #64748b;")
            dir_label.setToolTip(save_dir)
            bottom_layout.addWidget(dir_label, 1)
        else:
            bottom_layout.addStretch(1)

        self.btn_toggle_log = QPushButton("▼ 展开日志")
        self.btn_toggle_log.setObjectName("BtnToggleLog")
        self.btn_toggle_log.setCursor(Qt.PointingHandCursor)
        self.btn_toggle_log.setStyleSheet("""
            QPushButton#BtnToggleLog {
                background-color: #f1f5f9;
                color: #0078d4;
                border: 1px solid #cbd5e1;
                border-radius: 5px;
                padding: 4px 10px;
                font-size: 8.5pt;
                font-weight: 500;
                min-height: 20px;
            }
            QPushButton#BtnToggleLog:hover {
                background-color: #e2e8f0;
                color: #005a9e;
                border-color: #94a3b8;
            }
            QPushButton#BtnToggleLog:pressed {
                background-color: #cbd5e1;
            }
        """)
        self.btn_toggle_log.clicked.connect(self._toggle_log_view)
        bottom_layout.addWidget(self.btn_toggle_log)

        self.is_completed = False

        self.btn_close = QPushButton("后台运行")
        self.btn_close.setObjectName("BtnCloseDialog")
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.setToolTip("隐藏当前看板至后台，任务继续执行，可随时从主界面“任务看板”重新调出")
        self.btn_close.setStyleSheet("""
            QPushButton#BtnCloseDialog {
                background-color: #0078d4;
                color: #ffffff;
                border: none;
                border-radius: 5px;
                padding: 4px 12px;
                font-size: 8.5pt;
                font-weight: bold;
                min-height: 20px;
            }
            QPushButton#BtnCloseDialog:hover {
                background-color: #1089e5;
            }
            QPushButton#BtnCloseDialog:pressed {
                background-color: #0063b8;
            }
        """)
        self.btn_close.clicked.connect(self._on_close_clicked)
        bottom_layout.addWidget(self.btn_close)

        main_layout.addWidget(bottom_bar, 0)

        # 5. 接入 Qt 节流适配器 (10 FPS)
        self.bridge = QtTelemetryBridge(self, fps=10)
        self.bridge.batch_updated.connect(self.on_telemetry_batch)
        self.bridge.start()

    def _on_close_clicked(self):
        """点击底部操作按钮：未完成时安全退至后台，已完成时彻底关闭"""
        if self.is_completed:
            self.accept()
        else:
            self.hide()

    def reopen(self):
        """从主界面重新调出并置顶看板"""
        self.show()
        self.raise_()
        self.activateWindow()

    def append_log(self, text: str):
        """向日志抽屉追加一行调试信息"""
        self.log_box.appendPlainText(text)
        scrollbar = self.log_box.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def set_progress(self, completed: int, total: int):
        """兼容老接口：统筹汇报给 TelemetryEventHub，绝不私自覆盖 UI，彻底杜绝双写跳闪"""
        if total > 0:
            hub = TelemetryEventHub.get_instance()
            if total > 20:
                pct = min(100.0, max(0.0, (completed / total) * 100.0))
                badge = f"{int(round(pct))}% 已检索"
            else:
                badge = f"{completed}/{total} 完成"
            hub.set_overall_progress(completed, total, badge_text=badge)

    def on_telemetry_batch(self, snapshot: Dict[str, Any]):
        """接收 10 FPS 节流快照并批量刷新 GUI"""
        elapsed = int(time.time() - self._start_time)
        self.dashboard.update_snapshot(snapshot, elapsed)

        finished = snapshot.get("finished_tasks", 0)
        total = snapshot.get("total_tasks", 0)
        if total > 0 and finished >= total:
            self.is_completed = True
            self.btn_close.setText("完成并关闭")
            self.btn_close.setToolTip("所有任务已执行完成，点击关闭此看板")

        tasks = snapshot.get("tasks", [])
        if not tasks:
            return

        # 动态增加或更新卡片
        for t in tasks:
            tid = t.get("task_id", "")
            if not tid:
                continue
            card = self._cards.get(tid)
            if card is None:
                card = TaskCardWidget(t, self.cards_container)
                # 插在 stretch 之前
                insert_idx = max(0, self.cards_layout.count() - 1)
                self.cards_layout.insertWidget(insert_idx, card)
                self._cards[tid] = card
            else:
                card.update_data(t)

    def _toggle_log_view(self):
        visible = not self.log_box.isVisible()
        self.log_box.setVisible(visible)
        self.btn_toggle_log.setText("▲ 收起日志" if visible else "▼ 展开日志")
        if visible:
            self.resize(self.width(), self.height() + self.LOG_HEIGHT + 10)
        else:
            self.resize(self.width(), max(self.minimumHeight(), self.height() - self.LOG_HEIGHT - 10))

    def closeEvent(self, event):
        """窗口右上角关闭按钮：任务执行中时默认收缩至后台，避免误关丢失监控"""
        if not self.is_completed:
            event.ignore()
            self.hide()
        else:
            self.bridge.stop()
            super().closeEvent(event)

    def done(self, result):
        self.bridge.stop()
        super().done(result)
