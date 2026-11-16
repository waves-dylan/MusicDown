# -*- coding: utf-8 -*-
"""
config_manager.py
MusicDownload 统一配置与偏好持久化管理器 (基于便携式 config.ini 与 QSettings)
"""

import os
import sys
from PyQt5.QtCore import QSettings, QByteArray


def get_app_dir():
    """获取应用程序所在的根目录（兼容 PyInstaller 打包环境与源码调试环境）。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resolve_config_path():
    """
    确定 config.ini 的存储位置。
    优先采用便携模式（同级目录），若无写入权限则回退到 %APPDATA%/MusicDown/config.ini。
    """
    app_dir = get_app_dir()
    preferred_path = os.path.join(app_dir, "config.ini")
    
    # 检测应用程序同级目录是否可写
    try:
        test_file = os.path.join(app_dir, f".write_test_{os.getpid()}")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("test")
        if os.path.exists(test_file):
            os.remove(test_file)
        return preferred_path
    except (PermissionError, OSError):
        app_data = os.path.join(os.environ.get("APPDATA", app_dir), "MusicDown")
        try:
            os.makedirs(app_data, exist_ok=True)
            return os.path.join(app_data, "config.ini")
        except Exception:
            return preferred_path


class ConfigManager:
    """集中式偏好设置管理器"""

    # 预设默认值字典
    DEFAULTS = {
        # 下载配置
        "download/save_dir": "",                   # 为空时默认回退到 app_dir/已下载音乐
        "download/limit_per_source": 100,          # 单源搜索数量上限
        "download/scope": "勾选",                  # 下载范围（"全选" / "勾选" / "未勾选"）
        "download/auto_download": False,           # 搜索后自动下载全部

        # 搜索配置
        "search/mode": "搜索歌曲",                 # 搜索模式（"搜索歌曲" / "解析歌单链接"）
        "search/history": [],                      # 最近搜索关键词历史记录

        # 音源勾选偏好 (默认勾选国内主流：网易云音乐)
        "sources/selected": ["网易云音乐"],

        # 排序偏好
        "sort/mode": "按专辑",                     # 默认排序模式
        "sort/descending": False,                  # 是否倒序排列

        # 歌词与简繁转换偏好
        "lyrics/bilingual_enabled": True,          # 是否启用双语歌词
        "lyrics/layout": "dual_line",              # 双行对齐 / 单行拼合 / 仅中文翻译
        "lyrics/zh_convert": "keep",               # 保持原样 / 统一简体 / 统一繁体

        # 界面布局与状态
        "ui/show_process_details": False,          # 运行日志抽屉展开/折叠状态
        "ui/window_geometry": None,                # 窗口尺寸与屏幕位置 (QByteArray)
        "ui/table_header_state": None,             # 结果表格列宽与排列状态 (QByteArray)
    }

    def __init__(self, ini_path=None):
        self.ini_path = ini_path or resolve_config_path()
        is_new_file = not os.path.exists(self.ini_path)
        
        self.settings = QSettings(self.ini_path, QSettings.IniFormat)
        self.settings.setIniCodec("UTF-8")

        # 若是首次生成 config.ini，尝试从旧版 Windows 注册表平滑迁移配置
        if is_new_file:
            self._migrate_from_registry()

    def _migrate_from_registry(self):
        """兼容老版本：如果 Windows 注册表中存在旧配置，无缝迁移至本地 config.ini。"""
        try:
            old_reg = QSettings("musicdl", "MusicDownloader")
            if old_reg.contains("enable_bilingual_lyric"):
                self.set("lyrics/bilingual_enabled", old_reg.value("enable_bilingual_lyric", True, type=bool))
            if old_reg.contains("bilingual_lyric_layout"):
                self.set("lyrics/layout", old_reg.value("bilingual_lyric_layout", "dual_line", type=str))
            if old_reg.contains("zh_convert_mode"):
                self.set("lyrics/zh_convert", old_reg.value("zh_convert_mode", "keep", type=str))
            if old_reg.contains("show_process_details"):
                self.set("ui/show_process_details", old_reg.value("show_process_details", False, type=bool))
            self.settings.sync()
        except Exception:
            pass

    def get_str(self, key, default=None):
        """获取字符串配置。"""
        fallback = default if default is not None else self.DEFAULTS.get(key, "")
        val = self.settings.value(key, fallback)
        if val is None:
            return str(fallback)
        return str(val)

    def get_int(self, key, default=None):
        """获取整数配置。"""
        fallback = default if default is not None else self.DEFAULTS.get(key, 0)
        try:
            val = self.settings.value(key, fallback, type=int)
            return int(val)
        except (ValueError, TypeError):
            return int(fallback)

    def get_bool(self, key, default=None):
        """获取布尔值配置。"""
        fallback = default if default is not None else self.DEFAULTS.get(key, False)
        return self.settings.value(key, fallback, type=bool)

    def get_list(self, key, default=None):
        """获取字符串列表配置（具备单项、逗号分割、空列表的自动兼容能力）。"""
        fallback = default if default is not None else self.DEFAULTS.get(key, [])
        val = self.settings.value(key, None)
        if val is None:
            return list(fallback)
        if isinstance(val, list):
            res = [str(item).strip() for item in val if str(item).strip()]
            return res if res else list(fallback)
        if isinstance(val, str):
            res = [item.strip() for item in val.split(",") if item.strip()]
            return res if res else list(fallback)
        return list(fallback)

    def get_byte_array(self, key):
        """获取 QByteArray 原生二进制状态（用于窗口 Geometry 和表头 State）。"""
        val = self.settings.value(key, None, type=QByteArray)
        if val is None or not isinstance(val, QByteArray):
            return QByteArray()
        return val

    def set(self, key, value):
        """保存配置项并标记。"""
        self.settings.setValue(key, value)

    def sync(self):
        """强制立即将更改刷新写入磁盘文件。"""
        self.settings.sync()

    def reset_to_defaults(self):
        """重置所有偏好设置至出厂默认。"""
        self.settings.clear()
        for k, v in self.DEFAULTS.items():
            if v is not None:
                self.settings.setValue(k, v)
        self.settings.sync()

    def get_config_path(self):
        """返回当前配置文件的绝对路径。"""
        return os.path.abspath(self.ini_path)


# 全局单例持有者
_GLOBAL_CONFIG = None


def get_config():
    """获取全局唯一的 ConfigManager 实例。"""
    global _GLOBAL_CONFIG
    if _GLOBAL_CONFIG is None:
        _GLOBAL_CONFIG = ConfigManager()
    return _GLOBAL_CONFIG
