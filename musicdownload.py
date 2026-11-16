import sys
import os
import ast
import copy
import html
import inspect
import json
import pickle
import re
import shutil
import tempfile
import textwrap
import time
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
import requests
from requests.adapters import HTTPAdapter
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *

# rich 用于读取 musicdl 内部的真实进度（只读取任务计数，不显示控制台进度条）
try:
    from rich.progress import Progress as _RichProgress
except ImportError:
    _RichProgress = None

# 尝试导入 musicdl
try:
    from musicdl import musicdl
    MUSICDL_AVAILABLE = True
except ImportError:
    MUSICDL_AVAILABLE = False
    print("警告：musicdl 库未安装，请运行 pip install musicdl")

# 下载后元数据兜底校验：musicdl 写标签时若文件被临时占用会静默回退，这里再检查一遍
try:
    from audio_metadata import (
        repair_audio as _repair_audio_file,
        describe_lockers as _describe_lockers,
        bump_mtime as _bump_mtime,
        temp_dir_for as _temp_dir_for,
    )
    AUDIO_METADATA_AVAILABLE = True
except Exception as _audio_metadata_error:
    AUDIO_METADATA_AVAILABLE = False
    _repair_audio_file = None
    _describe_lockers = None
    _bump_mtime = None
    _temp_dir_for = None
    print(f"警告：元数据兜底模块加载失败（{_audio_metadata_error}），下载后不会自动校验封面/标签")

# 导入双语歌词增强模块
try:
    import bilingual_lyric
    BILINGUAL_LYRIC_AVAILABLE = True
except Exception as _bilingual_import_error:
    BILINGUAL_LYRIC_AVAILABLE = False
    print(f"警告：双语歌词增强模块加载失败（{_bilingual_import_error}），将使用基础单语歌词")



# 搜索结果排序方式（“默认顺序” = 按音乐源返回的原始顺序）
SORT_MODES = ["默认顺序", "按歌手", "按专辑", "按歌名", "按时长", "按格式", "按大小", "按来源"]

# 启动时默认勾选的音乐来源（其余来源不勾选，用户可随时手动勾选）
DEFAULT_SOURCES = ["网易云音乐"]

# 启动时默认的下载范围（"全选" / "勾选" / "未勾选"）
DEFAULT_DOWNLOAD_SCOPE = "勾选"

# 启动时搜索结果默认排序方式
DEFAULT_SORT_MODE = "按专辑"


# ==================== musicdl 音源、连接池与歌词修正补丁 ====================
_PARSE_LOCAL = threading.local()
_TESTER_LOCAL = threading.local()
_LYRIC_LOCAL = threading.local()
_COVER_LOCAL = threading.local()

_PARSER_HEALTH_LOCK = threading.Lock()
_PARSER_HEALTH = {}  # (source_name, parser_name) -> health dict
_PROBE_LOCKS = {}    # source_name -> threading.Lock
_LYRIC_CACHE_LOCK = threading.Lock()
_LYRIC_CACHE = {}

_LOSSLESS_EXTS = {'flac', 'm4a', 'wav', 'ape', 'ogg'}
_EXCLUDED_PARSER_METHODS = {
    '_parsewiththirdpartapis',
    '_parsewithofficialapiv1',
    '_parsewithvipofficialapiv1',
    '_parsewithnonvipofficialapiv1',
}


def _get_thread_http_session(local_store, pool_size=32):
    """返回当前线程复用的 requests.Session（带连接池），并清空 Cookie 避免跨站点串用。"""
    sess = getattr(local_store, 'session', None)
    if sess is None:
        sess = requests.Session()
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
        sess.mount('http://', adapter)
        sess.mount('https://', adapter)
        local_store.session = sess
    sess.cookies.clear()
    return sess


def _cap_timeout(timeout, max_connect=2.0, max_read=3.5):
    """为搜索阶段请求补全或收紧超时，避免死链长时间卡住工作线程。"""
    if timeout is None:
        return (max_connect, max_read)
    if isinstance(timeout, (int, float)):
        val = min(float(timeout), max_read)
        return (min(val, max_connect), val)
    if isinstance(timeout, (tuple, list)) and len(timeout) == 2:
        return (min(float(timeout[0]), max_connect), min(float(timeout[1]), max_read))
    return (max_connect, max_read)


def _is_instant_dead_exc(exc):
    """判断异常是否表明该第三方接口当前不可用（无需重试直接熔断）。"""
    if isinstance(exc, (
        requests.exceptions.SSLError,
        requests.exceptions.ConnectTimeout,
        requests.exceptions.ReadTimeout,
        requests.exceptions.ConnectionError,
        UnboundLocalError,
        TypeError,
        KeyError,
    )):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        if exc.response.status_code in (400, 401, 403, 404, 410, 500, 502, 503):
            return True
    return False


def _extract_thirdparty_parser_names(cls):
    """从音源类的 _parsewiththirdpartapis 中提取激活的第三方解析器方法名列表。"""
    if '_parsewiththirdpartapis' not in cls.__dict__:
        return []
    orig_fn = cls.__dict__['_parsewiththirdpartapis']
    try:
        src = textwrap.dedent(inspect.getsource(orig_fn))
        tree = ast.parse(src)
        env = {}

        def eval_list(node):
            if isinstance(node, ast.List):
                out = []
                for elt in node.elts:
                    if (isinstance(elt, ast.Attribute) and isinstance(elt.value, ast.Name)
                            and elt.value.id == 'self' and elt.attr.startswith('_parsewith')):
                        out.append(elt.attr)
                return out
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                return eval_list(node.left) + eval_list(node.right)
            if isinstance(node, ast.Subscript):
                seq = eval_list(node.value)
                if isinstance(node.slice, ast.Slice):
                    lo = ast.literal_eval(node.slice.lower) if node.slice.lower else None
                    hi = ast.literal_eval(node.slice.upper) if node.slice.upper else None
                    return seq[lo:hi]
                return seq
            if isinstance(node, ast.Name) and node.id in env:
                return list(env[node.id])
            return []

        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and 'parser' in tgt.id:
                        env[tgt.id] = eval_list(node.value)
            elif isinstance(node, ast.For):
                cand = eval_list(node.iter)
                if cand:
                    found = cand
        if found:
            return found
    except Exception:
        pass
    # 冻结 exe 等无法读取源码时，退回字节码常量名提取
    return [
        name for name in orig_fn.__code__.co_names
        if name.startswith('_parsewith') and name.endswith('api')
        and name not in _EXCLUDED_PARSER_METHODS and name in cls.__dict__
    ]


def _call_one_parser(client, source_name, parser_name, parser_fn, search_result,
                     request_overrides, valid_exts, validator=None):
    """带熔断、线程复用连接池与耗时统计地执行单个第三方解析器。"""
    from musicdl.modules.utils import SongInfo
    key = (source_name, parser_name)
    now = time.monotonic()
    with _PARSER_HEALTH_LOCK:
        st = _PARSER_HEALTH.get(key)
        if st and now < st['cooldown_until']:
            return None

    prev_active = getattr(_PARSE_LOCAL, 'active', False)
    _PARSE_LOCAL.active = True
    started = time.perf_counter()
    try:
        song_info = parser_fn(client, copy.copy(search_result), request_overrides)
    except Exception as exc:
        with _PARSER_HEALTH_LOCK:
            st = _PARSER_HEALTH.setdefault(
                key, {'hard_fails': 0, 'empty': 0, 'cooldown_until': 0.0, 'ok': 0, 'avg_lat': 10.0, 'lossless': False}
            )
            st['hard_fails'] += 1
            if st['ok'] > 0:
                # 已证实可用的主力源偶发抖动时宽容至连续 3 次失败才短冷却
                if st['hard_fails'] >= 3:
                    st['cooldown_until'] = time.monotonic() + 45.0
            else:
                thresh = 1 if _is_instant_dead_exc(exc) else 2
                if st['hard_fails'] >= thresh:
                    st['cooldown_until'] = time.monotonic() + 180.0
        return None
    finally:
        _PARSE_LOCAL.active = prev_active

    elapsed = time.perf_counter() - started
    ok = bool(song_info and getattr(song_info, 'with_valid_download_url', False)
              and getattr(song_info, 'ext', None) in valid_exts)
    if ok and validator is not None and not validator(song_info):
        ok = False

    with _PARSER_HEALTH_LOCK:
        st = _PARSER_HEALTH.setdefault(
            key, {'hard_fails': 0, 'empty': 0, 'cooldown_until': 0.0, 'ok': 0, 'avg_lat': 10.0, 'lossless': False}
        )
        if ok:
            st['hard_fails'] = 0
            st['empty'] = 0
            st['cooldown_until'] = 0.0
            st['ok'] += 1
            st['avg_lat'] = elapsed if st['ok'] == 1 else (st['avg_lat'] * 0.6 + elapsed * 0.4)
            if getattr(song_info, 'ext', '') in _LOSSLESS_EXTS:
                st['lossless'] = True
        elif st['ok'] == 0:
            # 仅对从未成功过的接口统计空结果熔断；已证实可用的接口可能只是某首冷门歌无版权
            st['empty'] += 1
            if st['empty'] >= 5:
                st['cooldown_until'] = time.monotonic() + 120.0
    return song_info if ok else None


def _is_preferred_song(song_info, prefer_lossless=True):
    return (not prefer_lossless) or (getattr(song_info, 'ext', '') in _LOSSLESS_EXTS)


def _race_parser_batch(client, source_name, batch, method_map, search_result,
                       request_overrides, valid_exts, validator=None,
                       prefer_lossless=True, timeout_s=8.0):
    """并发探测一批第三方解析器；命中可立即采纳的音质即返回，否则返回兜底有效结果。"""
    if not batch:
        return None, None
    pool = ThreadPoolExecutor(max_workers=len(batch))
    futures = [
        pool.submit(_call_one_parser, client, source_name, pn, method_map[pn],
                    search_result, request_overrides, valid_exts, validator)
        for pn in batch
    ]
    preferred, fallback = None, None
    try:
        for fut in as_completed(futures, timeout=timeout_s):
            cand = fut.result()
            if cand is None:
                continue
            if _is_preferred_song(cand, prefer_lossless):
                preferred = cand
                break
            if fallback is None:
                fallback = cand
    except FuturesTimeoutError:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return preferred, fallback


def _run_smart_thirdparty(client, source_name, parser_names, method_map,
                          search_result, request_overrides, valid_exts,
                          validator=None, prefer_lossless=True):
    """按“最快可用源优先 + 冷启动波次探测 + 熔断跳过死链”策略调度第三方解析器。"""
    from musicdl.modules.utils import SongInfo
    now = time.monotonic()
    with _PARSER_HEALTH_LOCK:
        proven, unproven = [], []
        for idx, pn in enumerate(parser_names):
            if pn not in method_map:
                continue
            st = _PARSER_HEALTH.get((source_name, pn))
            if st and now < st['cooldown_until']:
                continue
            if st and st['ok'] > 0:
                proven.append((0 if st['lossless'] else 1, st['avg_lat'], idx, pn))
            else:
                unproven.append((idx, pn))
        proven.sort()
        proven_names = [item[3] for item in proven]
        unproven_names = [item[1] for item in unproven]

    best_fallback = None
    if proven_names:
        res = _call_one_parser(client, source_name, proven_names[0], method_map[proven_names[0]],
                               search_result, request_overrides, valid_exts, validator)
        if res is not None:
            if _is_preferred_song(res, prefer_lossless):
                return res
            best_fallback = res
        if len(proven_names) > 1:
            preferred, fallback = _race_parser_batch(
                client, source_name, proven_names[1:7], method_map,
                search_result, request_overrides, valid_exts, validator,
                prefer_lossless=prefer_lossless, timeout_s=8.0,
            )
            if preferred is not None:
                return preferred
            if best_fallback is None:
                best_fallback = fallback
        if best_fallback is not None and not prefer_lossless:
            return best_fallback

    if unproven_names:
        lock = _PROBE_LOCKS.setdefault(source_name, threading.Lock())
        use_lock = (len(proven_names) == 0)
        if use_lock:
            lock.acquire()
        try:
            if use_lock:
                with _PARSER_HEALTH_LOCK:
                    now2 = time.monotonic()
                    newly_proven = [
                        (0 if _PARSER_HEALTH[(source_name, pn)]['lossless'] else 1,
                         _PARSER_HEALTH[(source_name, pn)]['avg_lat'], pn)
                        for pn in parser_names
                        if (source_name, pn) in _PARSER_HEALTH
                        and _PARSER_HEALTH[(source_name, pn)]['ok'] > 0
                        and now2 >= _PARSER_HEALTH[(source_name, pn)]['cooldown_until']
                    ]
                    newly_proven.sort()
                if newly_proven:
                    for _, _, pn in newly_proven:
                        cand = _call_one_parser(client, source_name, pn, method_map[pn],
                                                search_result, request_overrides, valid_exts, validator)
                        if cand is not None:
                            if _is_preferred_song(cand, prefer_lossless):
                                return cand
                            if best_fallback is None:
                                best_fallback = cand
                    if best_fallback is not None and not prefer_lossless:
                        return best_fallback
                    with _PARSER_HEALTH_LOCK:
                        now3 = time.monotonic()
                        unproven_names = [
                            pn for pn in parser_names
                            if pn in method_map
                            and not (_PARSER_HEALTH.get((source_name, pn))
                                     and now3 < _PARSER_HEALTH[(source_name, pn)]['cooldown_until'])
                            and not (_PARSER_HEALTH.get((source_name, pn))
                                     and _PARSER_HEALTH[(source_name, pn)]['ok'] > 0)
                        ]

            wave_timeout = 8.0 if prefer_lossless else 6.0
            for i in range(0, len(unproven_names), 8):
                preferred, fallback = _race_parser_batch(
                    client, source_name, unproven_names[i:i + 8], method_map,
                    search_result, request_overrides, valid_exts, validator,
                    prefer_lossless=prefer_lossless, timeout_s=wave_timeout,
                )
                if preferred is not None:
                    return preferred
                if best_fallback is None:
                    best_fallback = fallback
        finally:
            if use_lock:
                lock.release()

    if best_fallback is not None:
        return best_fallback
    return SongInfo(source=client.source, raw_data={'search': search_result, 'download': {}, 'lyric': {}})


def _install_http_session_patches(BaseMusicClient, AudioLinkTester):
    """为 musicdl 解析请求、音频探活与官方 API 加上连接池复用与超时上限。"""
    orig_session_request = requests.Session.request
    orig_req = requests.request
    orig_get = requests.get
    orig_post = requests.post

    def _patched_session_request(self, method, url, **kwargs):
        if getattr(_PARSE_LOCAL, 'active', False):
            kwargs['timeout'] = _cap_timeout(kwargs.get('timeout'), 2.5, 5.0)
        return orig_session_request(self, method, url, **kwargs)

    def _patched_request(method, url, **kwargs):
        if getattr(_PARSE_LOCAL, 'active', False):
            sess = _get_thread_http_session(_PARSE_LOCAL)
            kwargs['timeout'] = _cap_timeout(kwargs.get('timeout'))
            return orig_session_request(sess, method, url, **kwargs)
        return orig_req(method, url, **kwargs)

    def _patched_get(url, params=None, **kwargs):
        if getattr(_PARSE_LOCAL, 'active', False):
            kwargs.setdefault('allow_redirects', True)
            return _patched_request('get', url, params=params, **kwargs)
        return orig_get(url, params=params, **kwargs)

    def _patched_post(url, data=None, json=None, **kwargs):
        if getattr(_PARSE_LOCAL, 'active', False):
            return _patched_request('post', url, data=data, json=json, **kwargs)
        return orig_post(url, data=data, json=json, **kwargs)

    requests.Session.request = _patched_session_request
    requests.request = _patched_request
    requests.get = _patched_get
    requests.post = _patched_post

    orig_tester_request = AudioLinkTester.request
    orig_tester_test = AudioLinkTester.test

    def _patched_tester_request(self, method, url, request_overrides=None, range_bytes=None):
        orig_sess = self.session
        ro = dict(request_overrides or {})
        ro['timeout'] = _cap_timeout(ro.get('timeout', self.timeout), 2.0, 3.5)
        self.session = _get_thread_http_session(_TESTER_LOCAL)
        try:
            return orig_tester_request(self, method, url, request_overrides=ro, range_bytes=range_bytes)
        finally:
            self.session = orig_sess

    def _patched_tester_test(self, url, request_overrides=None, renew_session=True):
        return orig_tester_test(self, url, request_overrides=request_overrides, renew_session=False)

    AudioLinkTester.request = _patched_tester_request
    AudioLinkTester.test = _patched_tester_test

    orig_initsession = BaseMusicClient._initsession
    orig_base_get = BaseMusicClient.get
    orig_base_post = BaseMusicClient.post

    def _patched_initsession(self):
        had_session = getattr(self, 'session', None) is not None
        orig_initsession(self)
        sess = getattr(self, 'session', None)
        if isinstance(sess, requests.Session) and (not had_session or not getattr(sess, '_pct_pooled', False)):
            adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32)
            sess.mount('http://', adapter)
            sess.mount('https://', adapter)
            sess._pct_pooled = True

    def _patched_base_get(self, url, **kwargs):
        if kwargs.get('stream'):
            kwargs['timeout'] = _cap_timeout(kwargs.get('timeout'), 5.0, 20.0) if 'timeout' not in kwargs else kwargs['timeout']
        else:
            kwargs['timeout'] = _cap_timeout(kwargs.get('timeout'), 3.0, 6.0)
        return orig_base_get(self, url, **kwargs)

    def _patched_base_post(self, url, **kwargs):
        kwargs['timeout'] = _cap_timeout(kwargs.get('timeout'), 3.0, 6.0)
        return orig_base_post(self, url, **kwargs)

    BaseMusicClient._initsession = _patched_initsession
    BaseMusicClient.get = _patched_base_get
    BaseMusicClient.post = _patched_base_post

    try:
        from musicdl.modules.utils.lyric import LyricSearchClient
        orig_lyric_search = LyricSearchClient.search.__func__ if isinstance(
            LyricSearchClient.__dict__.get('search'), staticmethod
        ) else LyricSearchClient.search

        def _patched_lyric_search(*args, **kwargs):
            prev = getattr(_PARSE_LOCAL, 'active', False)
            _PARSE_LOCAL.active = True
            try:
                return orig_lyric_search(*args, **kwargs)
            finally:
                _PARSE_LOCAL.active = prev

        LyricSearchClient.search = staticmethod(_patched_lyric_search)
    except Exception:
        pass


_MUSIC_FOLDER_LOCK = threading.Lock()
_MUSIC_DESKTOP_INI = b"[ViewState]\r\nMode=\r\nVid=\r\nFolderType=Music\r\n"
_SKIP_MUSIC_FOLDER_PARTS = {'.musicdl_staging', '.musicdl_temp', '__pycache__', '.git'}
_SKIP_MUSIC_FOLDER_PREFIXES = ('musicdl_hls_', 'apple_id_')


def _is_temp_or_internal_dir(dir_path):
    """过滤暂存/临时/内部目录，只为真正的音乐输出目录设置 Windows 音乐文件夹视图。"""
    if not dir_path:
        return True
    try:
        abs_dir = os.path.abspath(dir_path)
    except Exception:
        return True
    if os.path.dirname(abs_dir) == abs_dir:
        return True
    norm = os.path.normcase(abs_dir)
    try:
        tmp_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
        if norm == tmp_root or norm.startswith(tmp_root + os.sep):
            return True
    except Exception:
        pass
    for part in norm.split(os.sep):
        if part in _SKIP_MUSIC_FOLDER_PARTS or part.startswith(_SKIP_MUSIC_FOLDER_PREFIXES):
            return True
    return False


def _collect_missing_ancestors(target_dir):
    """在创建目录前收集待新建的祖先目录链（包含目标目录自身），以便统一设置文件夹类型。"""
    if not target_dir:
        return []
    try:
        abs_target = os.path.abspath(target_dir)
    except Exception:
        return []
    pending = []
    cur = abs_target
    while cur and not os.path.exists(cur):
        pending.append(cur)
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent
    if abs_target not in pending:
        pending.insert(0, abs_target)
    return pending


def _read_ini_text(raw_bytes):
    if raw_bytes.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw_bytes.decode('utf-16', errors='replace')
    return raw_bytes.decode('utf-8-sig', errors='replace')


def _ensure_windows_music_folder(dir_path):
    """在 Windows 下为音乐目录写入 desktop.ini (FolderType=Music) 并设置目录属性。"""
    if os.name != 'nt' or _is_temp_or_internal_dir(dir_path):
        return
    abs_dir = os.path.abspath(dir_path)
    if not os.path.isdir(abs_dir):
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        shell32 = ctypes.windll.shell32
        FILE_ATTRIBUTE_READONLY = 0x1
        FILE_ATTRIBUTE_HIDDEN = 0x2
        FILE_ATTRIBUTE_SYSTEM = 0x4
        FILE_ATTRIBUTE_NORMAL = 0x80
        INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
        SHCNE_UPDATEDIR = 0x00001000
        SHCNF_PATHW = 0x0005
        SHCNF_FLUSHNOWAIT = 0x2000
    except Exception:
        return

    ini_path = os.path.join(abs_dir, 'desktop.ini')
    with _MUSIC_FOLDER_LOCK:
        try:
            changed = False
            if not os.path.exists(ini_path):
                with open(ini_path, 'wb') as fp:
                    fp.write(_MUSIC_DESKTOP_INI)
                changed = True
            else:
                with open(ini_path, 'rb') as fp:
                    raw = fp.read()
                text = _read_ini_text(raw)
                if not re.search(r'(?im)^\s*FolderType\s*=\s*Music\s*$', text):
                    if re.search(r'(?im)^\s*\[ViewState\]\s*$', text):
                        if re.search(r'(?im)^\s*FolderType\s*=.*$', text):
                            new_text = re.sub(r'(?im)^\s*FolderType\s*=.*$', 'FolderType=Music', text, count=1)
                        else:
                            new_text = re.sub(
                                r'(?im)^(\s*\[ViewState\]\s*\r?\n)',
                                r'\1FolderType=Music\r\n',
                                text,
                                count=1,
                            )
                    else:
                        sep = '' if (not text or text.endswith(('\r\n', '\n'))) else '\r\n'
                        new_text = f"{text}{sep}[ViewState]\r\nMode=\r\nVid=\r\nFolderType=Music\r\n"
                    cur_ini_attr = kernel32.GetFileAttributesW(ini_path)
                    if cur_ini_attr != INVALID_FILE_ATTRIBUTES and (
                        cur_ini_attr & (FILE_ATTRIBUTE_READONLY | FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
                    ):
                        kernel32.SetFileAttributesW(ini_path, FILE_ATTRIBUTE_NORMAL)
                    with open(ini_path, 'wb') as fp:
                        fp.write(new_text.encode('utf-8'))
                    changed = True

            ini_attr = kernel32.GetFileAttributesW(ini_path)
            want_ini_attr = (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM) if ini_attr == INVALID_FILE_ATTRIBUTES else (
                ini_attr | FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
            )
            if ini_attr != want_ini_attr:
                kernel32.SetFileAttributesW(ini_path, want_ini_attr)
                changed = True

            dir_attr = kernel32.GetFileAttributesW(abs_dir)
            if dir_attr != INVALID_FILE_ATTRIBUTES and (dir_attr & FILE_ATTRIBUTE_READONLY) == 0:
                kernel32.SetFileAttributesW(abs_dir, dir_attr | FILE_ATTRIBUTE_READONLY)
                changed = True

            if changed:
                shell32.SHChangeNotify(SHCNE_UPDATEDIR, SHCNF_PATHW | SHCNF_FLUSHNOWAIT, abs_dir, None)
        except Exception:
            pass


def _makedirs_music_folder(dir_path, exist_ok=True, mode=0o777):
    """创建目录并为其（含新建的中间父目录）设置 Windows 音乐文件夹类型。"""
    pending = _collect_missing_ancestors(dir_path)
    os.makedirs(dir_path, exist_ok=exist_ok, mode=mode)
    for d in reversed(pending):
        _ensure_windows_music_folder(d)


def _install_musicdl_patches():
    """跳过会返回错版音频的 spotidown.me、加速第三方解析调度并修补临时路径。"""
    if getattr(_install_musicdl_patches, '_installed', False):
        return
    try:
        from musicdl.modules.sources import MusicClientBuilder
        from musicdl.modules.sources.base import BaseMusicClient
        from musicdl.modules.sources.spotify import SpotifyMusicClient
        from musicdl.modules.utils import SongInfo, AudioLinkTester, safeextractfromdict
    except Exception as e:
        print(f"musicdl 补丁安装失败: {e}")
        return

    _install_http_session_patches(BaseMusicClient, AudioLinkTester)

    def _expected_ms(search_result):
        return (safeextractfromdict(search_result, ['item', 'data', 'duration', 'totalMilliseconds'], None)
                or safeextractfromdict(search_result, ['itemV2', 'data', 'trackDuration', 'totalMilliseconds'], None))

    # spotidown.me / spotsaver 的元数据虽对，但转换出的音频常是别的版本；
    # spotubedl 与 musicfab 是按 Spotify track id 取音源，实测时长一致。
    spotify_parsers = [
        '_parsewithspotubedlapi', '_parsewithmusicfabapi', '_parsewithspotsaverapi',
        '_parsewithrapidapi', '_parsewithspotifydownloadsapi', '_parsewithsavemytracksapi',
    ]
    spotify_methods = {pn: SpotifyMusicClient.__dict__[pn] for pn in spotify_parsers if pn in SpotifyMusicClient.__dict__}

    def _patched_spotify_thirdparty(self, search_result: dict, request_overrides: dict = None):
        if self.default_cookies or (request_overrides or {}).get('cookies'):
            return SongInfo(source=self.source)
        expected_ms = _expected_ms(search_result)

        def _check_duration(song_info):
            if expected_ms and song_info.duration_s:
                actual_ms = float(song_info.duration_s) * 1000
                if abs(actual_ms - float(expected_ms)) > 4000:
                    print(f"跳过音源：{song_info.song_name} 实际 {actual_ms/1000:.1f}s / Spotify {float(expected_ms)/1000:.1f}s")
                    return False
            return True

        return _run_smart_thirdparty(
            self, 'SpotifyMusicClient', spotify_parsers, spotify_methods,
            search_result, request_overrides, AudioLinkTester.VALID_AUDIO_EXTS,
            validator=_check_duration, prefer_lossless=False
        )

    SpotifyMusicClient._parsewiththirdpartapis = _patched_spotify_thirdparty
    SpotifyMusicClient._pct_patched = True

    # 为其余带多第三方解析器的音源装上智能调度（单解析器/特殊签名的保留原实现）
    for src_name, cls in MusicClientBuilder.REGISTERED_MODULES.items():
        if src_name in ('SpotifyMusicClient', 'JooxMusicClient'):
            continue
        p_names = _extract_thirdparty_parser_names(cls)
        if len(p_names) < 2:
            continue
        orig_tp = cls._parsewiththirdpartapis
        method_map = {pn: cls.__dict__[pn] for pn in p_names if pn in cls.__dict__}
        if not method_map:
            continue

        def _make_smart_tp(s_name, p_list, orig_tp_fn, m_map):
            def _smart_tp(self, search_result: dict, request_overrides: dict = None):
                cookies = getattr(self, 'default_cookies', None) or (request_overrides or {}).get('cookies')
                if s_name == 'NeteaseMusicClient':
                    from musicdl.modules.utils.neteaseutils import DEFAULT_COOKIES
                    if cookies and cookies != DEFAULT_COOKIES:
                        return SongInfo(source=self.source, raw_data={'quality': 'standard'})
                elif s_name == 'QobuzMusicClient':
                    return orig_tp_fn(self, search_result, request_overrides)
                elif s_name == 'SodaMusicClient':
                    if getattr(self, 'soda_auth_info', None) or (request_overrides or {}).get('cookies'):
                        return SongInfo(source=self.source)
                elif s_name == 'BodianMusicClient':
                    if (getattr(self, 'auth_info', None) or {}).get('token'):
                        return SongInfo(source=self.source)
                elif cookies:
                    return SongInfo(source=self.source)

                return _run_smart_thirdparty(
                    self, s_name, p_list, m_map,
                    search_result, request_overrides, AudioLinkTester.VALID_AUDIO_EXTS
                )
            return _smart_tp

        cls._parsewiththirdpartapis = _make_smart_tp(src_name, p_names, orig_tp, method_map)

    try:
        from musicdl.modules.utils import SongInfoUtils
        from pathlib import Path

        def _safe_maketemppath(audio_path: Path) -> Path:
            temp_dir = _temp_dir_for(str(audio_path)) if _temp_dir_for else str(audio_path.parent)
            fd, temp_name = tempfile.mkstemp(prefix=audio_path.stem + ".", suffix=".musictmp", dir=temp_dir)
            os.close(fd)
            return Path(temp_name)

        SongInfoUtils.maketemppath = staticmethod(_safe_maketemppath)
    except Exception as e:
        print(f"maketemppath 补丁安装失败: {e}")

    try:
        from musicdl.modules.utils import IOUtils
        try:
            from pathvalidate import sanitize_filepath as _sanitize_fp
        except Exception:
            _sanitize_fp = lambda p: p

        touchdir_desc = IOUtils.__dict__.get('touchdir')
        orig_touchdir = touchdir_desc.__func__ if isinstance(touchdir_desc, staticmethod) else IOUtils.touchdir

        def _patched_touchdir(directory: str, exist_ok: bool = True, mode: int = 511, auto_sanitize: bool = True):
            resolved = _sanitize_fp(directory) if (auto_sanitize and directory) else directory
            pending = _collect_missing_ancestors(resolved) if resolved else []
            result = orig_touchdir(directory, exist_ok=exist_ok, mode=mode, auto_sanitize=auto_sanitize)
            for d in reversed(pending):
                _ensure_windows_music_folder(d)
            return result

        IOUtils.touchdir = staticmethod(_patched_touchdir)
    except Exception as e:
        print(f"touchdir 补丁安装失败: {e}")

    _install_musicdl_patches._installed = True


def _netease_fetch_lyric_fallback(title, singer, duration, sess):
    """基础单语歌词抓取兜底。"""
    try:
        norm = lambda s: re.sub(r'\s+', '', (s or '')).lower()
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152.0 Safari/537.36',
                   'Referer': 'https://music.163.com/'}
        resp = sess.get('https://music.163.com/api/search/get',
                        params={'s': f'{title} {singer}', 'type': 1, 'limit': 10, 'offset': 0},
                        headers=headers, timeout=(3.0, 5.0))
        songs = ((resp.json().get('result') or {}).get('songs') or [])
        target = norm(title)
        best = None
        for song in songs:
            if norm(song.get('name')) != target:
                continue
            song_dur = float(song.get('duration') or 0) / 1000
            if duration and song_dur and abs(song_dur - duration) > 4:
                continue
            best = song
            break
        if best:
            lr = sess.get('https://music.163.com/api/song/lyric',
                          params={'id': best['id'], 'lv': 1, 'kv': 1, 'tv': -1},
                          headers=headers, timeout=(3.0, 5.0))
            lrc = ((lr.json().get('lrc') or {}).get('lyric') or '').strip()
            if re.search(r'\[\d{1,2}:\d{2}', lrc):
                return lrc
        return None
    except Exception:
        return None


def _netease_fetch_lyric(song_info, enable_bilingual=True, layout='dual_line', zh_convert='keep'):
    """获取/补全歌词（智能支持外语歌双语合并与中文歌单语直通，及简繁转换）。"""
    try:
        title = _song_text(song_info, 'song_name').strip()
        singer = _song_text(song_info, 'singers').split(',')[0].strip()
        duration = float((song_info.get('duration_s') if hasattr(song_info, 'get') else getattr(song_info, 'duration_s', 0)) or 0)
        current = (song_info.get('lyric') if hasattr(song_info, 'get') else getattr(song_info, 'lyric', None))
        if not title:
            return None
        sess = _get_thread_http_session(_LYRIC_LOCAL, pool_size=16)
        if BILINGUAL_LYRIC_AVAILABLE:
            return bilingual_lyric.get_enhanced_lyrics(
                current_lrc=current,
                title=title,
                singer=singer,
                duration=duration,
                layout=layout,
                enable_bilingual=enable_bilingual,
                zh_convert=zh_convert,
                session=sess
            )
        else:
            return _netease_fetch_lyric_fallback(title, singer, duration, sess)
    except Exception:
        return None


def _ensure_song_lyric(target_song, source_song=None, enable_bilingual=True, layout='dual_line', zh_convert='keep'):
    """下载单首歌曲前按需补全缺失歌词，或将外语歌词升级为双语歌词，并应用简繁转换。"""
    try:
        title = _song_text(target_song, 'song_name').strip()
        singer = _song_text(target_song, 'singers').split(',')[0].strip()
        duration = float((target_song.get('duration_s') if hasattr(target_song, 'get') else getattr(target_song, 'duration_s', 0)) or 0)
        current = (target_song.get('lyric') if hasattr(target_song, 'get') else getattr(target_song, 'lyric', None))

        norm = lambda s: re.sub(r'\s+', '', (s or '')).lower()
        cache_key = (norm(title), norm(singer), round(duration), bool(enable_bilingual), str(layout), str(zh_convert))
        with _LYRIC_CACHE_LOCK:
            if cache_key in _LYRIC_CACHE:
                cached_lrc = _LYRIC_CACHE[cache_key]
                if cached_lrc:
                    if hasattr(target_song, 'lyric'): target_song.lyric = cached_lrc
                    elif isinstance(target_song, dict): target_song['lyric'] = cached_lrc
                    if source_song is not None and source_song is not target_song:
                        if hasattr(source_song, 'lyric'): source_song.lyric = cached_lrc
                        elif isinstance(source_song, dict): source_song['lyric'] = cached_lrc
                return

        sess = _get_thread_http_session(_LYRIC_LOCAL, pool_size=16)
        if BILINGUAL_LYRIC_AVAILABLE:
            final_lrc = bilingual_lyric.get_enhanced_lyrics(
                current_lrc=current,
                title=title,
                singer=singer,
                duration=duration,
                layout=layout,
                enable_bilingual=enable_bilingual,
                zh_convert=zh_convert,
                session=sess
            )
        else:
            final_lrc = current or _netease_fetch_lyric_fallback(title, singer, duration, sess)

        with _LYRIC_CACHE_LOCK:
            _LYRIC_CACHE[cache_key] = final_lrc

        if not final_lrc:
            return

        if hasattr(target_song, 'lyric'):
            target_song.lyric = final_lrc
        elif isinstance(target_song, dict):
            target_song['lyric'] = final_lrc
        if source_song is not None and source_song is not target_song:
            if hasattr(source_song, 'lyric'):
                source_song.lyric = final_lrc
            elif isinstance(source_song, dict):
                source_song['lyric'] = final_lrc
    except Exception as err:
        print(f"歌词处理异常: {err}")


def _enrich_missing_lyrics(results):
    if not isinstance(results, dict):
        return
    for songs in results.values():
        for song_info in songs or []:
            _ensure_song_lyric(song_info)


# ==================== 专辑曲目编号（所有数据源通用） ====================

_TRACKNUMBER_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                           '(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36')
_ALBUM_TRACKS_CACHE = {}
_ALBUM_LOOKUP_CACHE = {}


def _http_get_json(url, params=None, headers=None, timeout=15):
    try:
        request_headers = {'User-Agent': _TRACKNUMBER_USER_AGENT}
        if headers:
            request_headers.update(headers)
        response = requests.get(url, params=params, headers=request_headers, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def _to_seconds(value):
    try:
        if isinstance(value, str) and ':' in value:
            total = 0.0
            for part in value.split(':'):
                total = total * 60 + float(part)
            return total
        number = float(value or 0)
        return number / 1000.0 if number > 6000 else number
    except Exception:
        return 0.0


def _normalize_id(value):
    return str(value or '').strip().upper().replace('MUSIC_', '').replace('MUSIC:', '')


def _normalize_text(value):
    text = unicodedata.normalize('NFKC', html.unescape(str(value or ''))).lower()
    text = re.sub(r'[\(（\[【].*?[\)）\]】]', '', text)
    text = text.replace('$', 's')
    return re.sub(r'[^\w]+', '', text)


def _build_tracks(items):
    tracks = []
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        tracks.append({'number': index + 1,
                       'id': item.get('id'),
                       'name': item.get('name') or '',
                       'duration': item.get('duration') or 0})
    return tracks


def _match_track_number(tracks, title, duration):
    target = _normalize_text(title)
    if not target or not tracks:
        return None
    decorated = [(_normalize_text(track.get('name')), track) for track in tracks]
    candidates = [track for name, track in decorated if name and name == target]
    if not candidates:
        candidates = [track for name, track in decorated
                      if name and len(target) >= 2 and (target in name or name in target)]
    if not candidates:
        return None
    if len(candidates) > 1 and duration:
        close = [track for track in candidates
                 if track.get('duration') and abs(float(track['duration']) - duration) <= 6]
        if close:
            candidates = close
    if len(candidates) != 1:
        return None
    return candidates[0].get('number')


def _pick_track_number(tracks, identifier, title, duration):
    normalized = _normalize_id(identifier)
    if normalized:
        for track in tracks or []:
            if track.get('id') and _normalize_id(track.get('id')) == normalized:
                return track.get('number')
    return _match_track_number(tracks, title, duration)


# ---------- 各数据源专辑曲目表 ----------
def _qq_album_tracks(album_mid):
    key = ('qq', str(album_mid or ''))
    if key in _ALBUM_TRACKS_CACHE:
        return _ALBUM_TRACKS_CACHE[key]
    tracks = []
    if album_mid:
        data = _http_get_json(
            'https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg',
            params={'albummid': album_mid, 'g_tk': '5381', 'format': 'json', 'inCharset': 'utf8',
                    'outCharset': 'utf-8', 'notice': '0', 'platform': 'yqq.json', 'needNewCode': '0'},
            headers={'Referer': 'https://y.qq.com/'})
        items = ((data or {}).get('data') or {}).get('list') or []
        tracks = _build_tracks([{'id': item.get('songmid'), 'name': item.get('songname'),
                                 'duration': _to_seconds(item.get('interval'))}
                                for item in items if isinstance(item, dict)])
    _ALBUM_TRACKS_CACHE[key] = tracks
    return tracks


def _netease_album_tracks(album_id):
    key = ('netease', str(album_id or ''))
    if key in _ALBUM_TRACKS_CACHE:
        return _ALBUM_TRACKS_CACHE[key]
    songs = []
    if album_id:
        for url in (f'https://music.163.com/api/v1/album/{album_id}',
                    f'https://music.163.com/api/album/{album_id}'):
            data = _http_get_json(url, headers={'Referer': 'https://music.163.com/'})
            if not isinstance(data, dict):
                continue
            songs = data.get('songs') or ((data.get('album') or {}).get('songs') or [])
            if songs:
                break
    tracks = _build_tracks([{'id': song.get('id'), 'name': song.get('name'),
                             'duration': _to_seconds(song.get('dt') or song.get('duration'))}
                            for song in songs if isinstance(song, dict)])
    _ALBUM_TRACKS_CACHE[key] = tracks
    return tracks


def _kuwo_album_tracks(album_id):
    key = ('kuwo', str(album_id or ''))
    if key in _ALBUM_TRACKS_CACHE:
        return _ALBUM_TRACKS_CACHE[key]
    tracks = []
    if album_id:
        try:
            response = requests.get(
                'http://search.kuwo.cn/r.s',
                params={'stype': 'albuminfo', 'albumid': album_id, 'vipver': '1',
                        'encoding': 'utf8', 'rformat': 'json'},
                headers={'User-Agent': _TRACKNUMBER_USER_AGENT}, timeout=15)
            data = ast.literal_eval(response.text.strip())
            items = data.get('musiclist') or []
            tracks = _build_tracks([{'id': item.get('musicrid'), 'name': item.get('songname'),
                                     'duration': _to_seconds(item.get('duration'))}
                                    for item in items if isinstance(item, dict)])
        except Exception:
            tracks = []
    _ALBUM_TRACKS_CACHE[key] = tracks
    return tracks


def _kugou_album_tracks(album_id):
    key = ('kugou', str(album_id or ''))
    if key in _ALBUM_TRACKS_CACHE:
        return _ALBUM_TRACKS_CACHE[key]
    items = []
    page = 1
    while album_id and page <= 20:
        data = _http_get_json('http://mobilecdnbj.kugou.com/api/v3/album/song',
                              params={'albumid': album_id, 'page': page, 'pagesize': 100,
                                      'version': '9108', 'plat': '0'},
                              headers={'Referer': 'https://www.kugou.com/'})
        payload = (data or {}).get('data') or {}
        info = payload.get('info') or []
        for item in info:
            if not isinstance(item, dict):
                continue
            name = item.get('songname') or item.get('filename') or ''
            if item.get('filename') and ' - ' in item['filename']:
                name = item['filename'].split(' - ', 1)[-1]
            items.append({'id': item.get('hash'), 'name': name,
                          'duration': _to_seconds(item.get('duration'))})
        total = int(payload.get('total') or 0)
        page += 1
        if not info or len(info) < 100 or (total and len(items) >= total):
            break
    tracks = _build_tracks(items)
    _ALBUM_TRACKS_CACHE[key] = tracks
    return tracks


def _spotify_album_tracks(album_id):
    key = ('spotify', str(album_id or ''))
    if key in _ALBUM_TRACKS_CACHE:
        return _ALBUM_TRACKS_CACHE[key]
    items = []
    if album_id:
        try:
            response = requests.get(f'https://open.spotify.com/embed/album/{album_id}',
                                    headers={'User-Agent': _TRACKNUMBER_USER_AGENT}, timeout=15)
            matched = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                                response.text, re.S)
            if matched:
                entity = json.loads(matched.group(1))['props']['pageProps']['state']['data']['entity']
                for track in entity.get('trackList') or []:
                    uri = track.get('uri') or ''
                    duration = track.get('duration') or {}
                    items.append({'id': uri.rsplit(':', 1)[-1] if uri else '',
                                  'name': track.get('name') or track.get('title') or '',
                                  'duration': _to_seconds((duration or {}).get('totalMilliseconds'))})
        except Exception:
            items = []
    tracks = _build_tracks(items)
    _ALBUM_TRACKS_CACHE[key] = tracks
    return tracks


def _itunes_album_tracks(collection_id):
    key = ('itunes', str(collection_id or ''))
    if key in _ALBUM_TRACKS_CACHE:
        return _ALBUM_TRACKS_CACHE[key]
    data = _http_get_json('https://itunes.apple.com/lookup',
                          params={'id': collection_id, 'entity': 'song', 'limit': 200})
    results = [item for item in ((data or {}).get('results') or [])
               if isinstance(item, dict) and item.get('wrapperType') == 'track']
    results.sort(key=lambda item: (int(item.get('discNumber') or 1), int(item.get('trackNumber') or 0)))
    tracks = _build_tracks([{'id': item.get('trackId'), 'name': item.get('trackName'),
                             'duration': _to_seconds(item.get('trackTimeMillis'))}
                            for item in results])
    _ALBUM_TRACKS_CACHE[key] = tracks
    return tracks


# ---------- 通用回退：按专辑名 + 歌手反查曲目表 ----------
def _choose_album(candidates, album_name, singer, name_key, artist_key):
    target = _normalize_text(album_name)
    singer_key = _normalize_text(singer)
    if not target or not candidates:
        return None
    best, best_score = None, 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        name = _normalize_text(name_key(candidate))
        artist = _normalize_text(artist_key(candidate))
        if not name:
            continue
        if name == target:
            score = 4
        elif singer_key and len(target) >= 3 and (target in name or name in target):
            score = 2
        else:
            continue
        if singer_key:
            if artist == singer_key:
                score += 3
            elif artist and (singer_key in artist or artist in singer_key):
                score += 2
            else:
                continue
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= (5 if singer_key else 4) else None


def _search_qq_album(term, album_name, singer):
    key = ('qqsearch', _normalize_text(term), _normalize_text(album_name), _normalize_text(singer))
    if key in _ALBUM_LOOKUP_CACHE:
        return _ALBUM_LOOKUP_CACHE[key]
    data = _http_get_json('https://c.y.qq.com/soso/fcgi-bin/client_search_cp',
                          params={'w': term, 'format': 'json', 'p': 1, 'n': 10, 't': 8},
                          headers={'Referer': 'https://y.qq.com/'})
    album_data = ((data or {}).get('data') or {}).get('album') or {}
    album = _choose_album(album_data.get('list') or [], album_name, singer,
                          lambda item: item.get('albumName'), lambda item: item.get('singerName'))
    album_mid = (album or {}).get('albumMID') or ''
    _ALBUM_LOOKUP_CACHE[key] = album_mid
    return album_mid


def _search_netease_album(term, album_name, singer):
    key = ('neteasesearch', _normalize_text(term), _normalize_text(album_name), _normalize_text(singer))
    if key in _ALBUM_LOOKUP_CACHE:
        return _ALBUM_LOOKUP_CACHE[key]
    data = _http_get_json('https://music.163.com/api/search/get',
                          params={'s': term, 'type': 10, 'limit': 10, 'offset': 0},
                          headers={'Referer': 'https://music.163.com/'})
    candidates = (((data or {}).get('result') or {}).get('albums')) or []
    album = _choose_album(candidates, album_name, singer,
                          lambda item: item.get('name'),
                          lambda item: (item.get('artist') or {}).get('name'))
    album_id = (album or {}).get('id')
    result = str(album_id) if album_id else ''
    _ALBUM_LOOKUP_CACHE[key] = result
    return result


def _search_itunes_album(term, album_name, singer):
    key = ('itunessearch', _normalize_text(term), _normalize_text(album_name), _normalize_text(singer))
    if key in _ALBUM_LOOKUP_CACHE:
        return _ALBUM_LOOKUP_CACHE[key]
    data = _http_get_json('https://itunes.apple.com/search',
                          params={'term': term, 'entity': 'album', 'limit': 10, 'country': 'US'})
    album = _choose_album((data or {}).get('results') or [], album_name, singer,
                          lambda item: item.get('collectionName'), lambda item: item.get('artistName'))
    collection_id = (album or {}).get('collectionId')
    result = str(collection_id) if collection_id else ''
    _ALBUM_LOOKUP_CACHE[key] = result
    return result


def _resolve_album_tracks(album_name, singer):
    key = ('resolve', _normalize_text(album_name), _normalize_text(singer))
    if key in _ALBUM_TRACKS_CACHE:
        return _ALBUM_TRACKS_CACHE[key]
    tracks = []
    clean_album = re.sub(r'[\(（\[【].*?[\)）\]】]', '', str(album_name or '')).strip()
    terms = []
    for cand in (
        (str(album_name).strip() + ' ' + str(singer).strip()).strip(),
        str(album_name or '').strip(),
        (clean_album + ' ' + str(singer).strip()).strip() if clean_album else '',
        clean_album,
    ):
        if cand and cand not in terms:
            terms.append(cand)
    # 优先网易云（曲序更接近实体专辑），其次 QQ 音乐，最后 iTunes
    for term in terms:
        album_id = _search_netease_album(term, album_name, singer)
        if album_id:
            tracks = _netease_album_tracks(album_id)
            if tracks:
                break
    if not tracks:
        for term in terms:
            album_mid = _search_qq_album(term, album_name, singer)
            if album_mid:
                tracks = _qq_album_tracks(album_mid)
                if tracks:
                    break
    if not tracks:
        for term in terms:
            collection_id = _search_itunes_album(term, album_name, singer)
            if collection_id:
                tracks = _itunes_album_tracks(collection_id)
                if tracks:
                    break
    # 只缓存成功结果：网络抖动导致的空结果下次还能重试
    if tracks:
        _ALBUM_TRACKS_CACHE[key] = tracks
    return tracks


def _generic_track_number(song_info):
    album_name = str(getattr(song_info, 'album', '') or '').strip()
    singers = getattr(song_info, 'singers', '') or ''
    if isinstance(singers, (list, tuple)):
        singer = next((str(item).strip() for item in singers if str(item).strip()), '')
    else:
        singer = str(singers).split(',')[0].strip()
    title = getattr(song_info, 'song_name', '') or ''
    duration = _to_seconds(getattr(song_info, 'duration_s', 0))
    if not album_name:
        return None
    return _match_track_number(_resolve_album_tracks(album_name, singer), title, duration)


def _first_value(mapping, keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value:
            return value
    return None


def _track_number_for_song(song_info):
    """按数据源精确解析专辑内总曲序；失败时走通用专辑匹配，仍失败返回 (None, None)。"""
    source = str(getattr(song_info, 'source', '') or '')
    identifier = getattr(song_info, 'identifier', '') or ''
    title = getattr(song_info, 'song_name', '') or ''
    duration = _to_seconds(getattr(song_info, 'duration_s', 0))
    raw_data = getattr(song_info, 'raw_data', None) or {}
    search_data = raw_data.get('search') if isinstance(raw_data, dict) else None
    search_data = search_data if isinstance(search_data, dict) else {}

    number = None
    if 'QQ' in source:
        album = search_data.get('album') or {}
        album_mid = (album.get('mid') or album.get('albumMid') or album.get('album_mid')
                     or search_data.get('albummid') or search_data.get('album_mid'))
        if album_mid:
            number = _pick_track_number(_qq_album_tracks(album_mid), identifier, title, duration)
        if not number and search_data.get('index_album'):
            number = search_data.get('index_album')
    elif 'Netease' in source:
        album = search_data.get('al') or search_data.get('album') or {}
        album_id = album.get('id') or search_data.get('albumId')
        if album_id:
            number = _pick_track_number(_netease_album_tracks(album_id), identifier, title, duration)
    elif 'Kuwo' in source:
        album_id = _first_value(search_data, ('ALBUMID', 'albumId', 'albumid'))
        if album_id:
            number = _pick_track_number(_kuwo_album_tracks(album_id), identifier, title, duration)
    elif 'Kugou' in source:
        album_id = _first_value(search_data, ('AlbumID', 'album_id', 'albumid'))
        if album_id:
            number = _pick_track_number(_kugou_album_tracks(album_id), identifier, title, duration)
    elif 'Spotify' in source:
        item = ((search_data.get('item') or {}).get('data')
                or (search_data.get('itemV2') or {}).get('data') or {})
        album = item.get('albumOfTrack') or {}
        album_id = album.get('id') or (album.get('uri') or '').rsplit(':', 1)[-1]
        if album_id:
            number = _pick_track_number(_spotify_album_tracks(album_id), identifier, title, duration)

    if number:
        return int(number), None

    number = _generic_track_number(song_info)
    if number:
        return int(number), None

    if 'Apple' in source:
        attributes = search_data.get('attributes') or {}
        track_number = attributes.get('trackNumber')
        disc_number = attributes.get('discNumber')
        if track_number:
            return int(track_number), (int(disc_number) if disc_number and int(disc_number) > 1 else None)
    return None, None


def _read_track_tags(save_path):
    """读回 tracknumber / discnumber（字符串）；读不到返回 (None, None)。"""
    try:
        from mutagen import File as _MutagenFile
        audio = _MutagenFile(save_path, easy=True)
        if audio is None or audio.tags is None:
            return None, None

        def _first(value):
            if isinstance(value, (list, tuple)):
                return str(value[0]) if value else None
            return str(value) if value else None

        return _first(audio.tags.get('tracknumber')), _first(audio.tags.get('discnumber'))
    except Exception:
        return None, None


def _track_number_matches(save_path, number, disc_number=None):
    current, current_disc = _read_track_tags(save_path)
    if not current:
        return False
    try:
        if int(str(current).split('/')[0]) != int(number):
            return False
    except (TypeError, ValueError):
        return False
    if disc_number:
        try:
            if not current_disc or int(str(current_disc).split('/')[0]) != int(disc_number):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _write_track_tags_once(save_path, number, disc_number=None):
    """尝试一次写入（先 easy 模式，失败再按容器类型显式写）。"""
    try:
        from mutagen import File as _MutagenFile
        audio = _MutagenFile(save_path, easy=True)
        if audio is not None:
            if audio.tags is None:
                audio.add_tags()
            audio['tracknumber'] = str(int(number))
            if disc_number:
                audio['discnumber'] = str(int(disc_number))
            audio.save()
            return True
    except Exception:
        pass
    try:
        from mutagen import File as _MutagenFile
        from mutagen.id3 import ID3, TPOS, TRCK
        from mutagen.mp4 import MP4Tags
        audio = _MutagenFile(save_path)
        if audio is None:
            return False
        if audio.tags is None:
            audio.add_tags()
        if isinstance(audio.tags, ID3):
            audio.tags.add(TRCK(encoding=3, text=str(int(number))))
            if disc_number:
                audio.tags.add(TPOS(encoding=3, text=str(int(disc_number))))
            audio.save(v2_version=3)
        elif isinstance(audio.tags, MP4Tags):
            audio.tags['trkn'] = [(int(number), 0)]
            if disc_number:
                audio.tags['disk'] = [(int(disc_number), 0)]
            audio.save()
        else:
            audio.tags['TRACKNUMBER'] = [str(int(number))]
            if disc_number:
                audio.tags['DISCNUMBER'] = [str(int(disc_number))]
            audio.save()
        return True
    except Exception:
        return False


def _write_track_tags(save_path, number, disc_number=None, attempts=4):
    """写入音轨号并读回校验；文件被占用时退避重试，全部失败才返回 False。"""
    if not save_path or not os.path.exists(save_path):
        return False
    if _track_number_matches(save_path, number, disc_number):
        return True
    try:
        prev_mtime = os.stat(save_path).st_mtime
    except OSError:
        prev_mtime = None
    for attempt in range(max(1, attempts)):
        if _write_track_tags_once(save_path, number, disc_number):
            if _track_number_matches(save_path, number, disc_number):
                if _bump_mtime:
                    _bump_mtime(save_path, prev_mtime)
                else:
                    try:
                        os.utime(save_path, None)
                    except Exception:
                        pass
                return True
        if attempt + 1 < attempts:
            time.sleep(min(0.4 * (2 ** attempt), 3.0))
    return False


def _lockers_text(save_path):
    """文件被谁占用（例如 foobar2000 (PID 15028)），查不到返回空串。"""
    if not save_path or not _describe_lockers:
        return ''
    try:
        return _describe_lockers(save_path) or ''
    except Exception:
        return ''


def _write_album_track_numbers(song_infos, log=None):
    """下载完成后，按数据源专辑曲目表把专辑内编号写入 tracknumber 标签。"""
    emit = log if callable(log) else print
    for song_info in song_infos or []:
        name = getattr(song_info, 'song_name', '') or ''
        try:
            save_path = getattr(song_info, '_save_path', None) or getattr(song_info, 'save_path', None)
            if not save_path or not os.path.exists(save_path):
                continue
            number, disc_number = _track_number_for_song(song_info)
            if not number:
                # 单曲下载时逐首修复可能已写进音轨号，这里不要误报
                current, _ = _read_track_tags(save_path)
                if current:
                    continue
                emit(f"   ⚠ 未获取到专辑曲目编号：{name}")
                continue
            if _write_track_tags(save_path, number, disc_number):
                emit(f"   已写入专辑曲目编号：{name} - {number}")
            else:
                holders = _lockers_text(save_path)
                reason = f"，文件被占用：{holders}" if holders else ""
                emit(f"   ⚠ 写入专辑曲目编号失败：{name}（{number}）{reason}")
        except Exception as exc:
            emit(f"   ⚠ 写入专辑曲目编号失败：{name}（{exc}）")


if MUSICDL_AVAILABLE:
    _install_musicdl_patches()

# 图片下载线程（保留兼容，表格渲染优先走下方的有界线程池 CoverDownloadRunnable）
class ImageDownloadThread(QThread):
    finished = pyqtSignal(int, QPixmap, int, str)
    error = pyqtSignal(int, int, str)
    def __init__(self, row, image_url, generation=0):
        super().__init__()
        self.row = row
        self.image_url = image_url
        self.generation = generation
    def run(self):
        try:
            if not self.image_url or self.image_url == '':
                self.error.emit(self.row, self.generation, self.image_url or '')
                return
            sess = _get_thread_http_session(_COVER_LOCAL, pool_size=16)
            response = sess.get(self.image_url, timeout=(3.0, 6.0))
            if response.status_code == 200:
                image_data = response.content
                pixmap = QPixmap()
                pixmap.loadFromData(image_data)
                scaled_pixmap = pixmap.scaled(60, 60, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.finished.emit(self.row, scaled_pixmap, self.generation, self.image_url)
            else:
                self.error.emit(self.row, self.generation, self.image_url or '')
        except Exception as e:
            print(f"下载图片失败: {e}")
            self.error.emit(self.row, self.generation, self.image_url or '')


class CoverDownloadSignals(QObject):
    finished = pyqtSignal(str, QImage, int)
    error = pyqtSignal(str, int)


class CoverDownloadRunnable(QRunnable):
    """按封面 URL 去重并复用连接池的后台缩放任务，在工作线程只构造线程安全的 QImage。"""

    def __init__(self, image_url, generation, signals):
        super().__init__()
        self.image_url = image_url
        self.generation = generation
        self.signals = signals

    def run(self):
        try:
            if not self.image_url:
                self.signals.error.emit(self.image_url or '', self.generation)
                return
            sess = _get_thread_http_session(_COVER_LOCAL, pool_size=16)
            resp = sess.get(self.image_url, timeout=(3.0, 6.0))
            if resp.status_code != 200 or not resp.content:
                self.signals.error.emit(self.image_url, self.generation)
                return
            image = QImage()
            if not image.loadFromData(resp.content) or image.isNull():
                self.signals.error.emit(self.image_url, self.generation)
                return
            scaled = image.scaled(60, 60, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.signals.finished.emit(self.image_url, scaled, self.generation)
        except Exception as e:
            print(f"下载图片失败: {e}")
            self.signals.error.emit(self.image_url or '', self.generation)

def _song_text(song_info, key, default=""):
    """从 SongInfo / dict 中安全地取出一个字符串字段（列表自动拼接）。"""
    try:
        value = song_info.get(key, default) if hasattr(song_info, "get") else getattr(song_info, key, default)
    except Exception:
        value = default
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value if item)
    return str(value) if value else ""


def _song_metadata_desired(song_info):
    """把 SongInfo 里的可写字段整理成 audio_metadata.repair_audio 需要的格式。"""
    def value(key):
        text = _song_text(song_info, key).strip()
        return "" if text.lower() in ("null", "none", "[]", "{}") else text

    return {
        "title": value("song_name"),
        "artist": value("singers"),
        "album": value("album"),
        "lyrics": value("lyric"),
        "cover_url": value("cover_url"),
    }


def _repair_downloaded_metadata(song_info):
    """下载完成后校验一次元数据，返回 (修复说明, 仍缺失说明)。"""
    if not AUDIO_METADATA_AVAILABLE:
        return [], []
    # 优先用 _save_path：save_path 属性在 _save_path 为空时会重新拼一个带 (1) 的新路径
    save_path = getattr(song_info, "_save_path", None) or getattr(song_info, "save_path", None)
    if not save_path or not os.path.exists(save_path):
        return [], []
    desired = _song_metadata_desired(song_info)
    try:
        number, disc_number = _track_number_for_song(song_info)
        if number:
            desired['tracknumber'] = str(int(number))
            desired['discnumber'] = str(int(disc_number)) if disc_number else ''
    except Exception:
        pass
    try:
        new_path, notes, issues = _repair_audio_file(save_path, desired)
    except Exception as err:
        print(f"元数据校验失败: {err}")
        return [], [f"元数据校验异常：{err}"]
    if new_path and os.path.normcase(new_path) != os.path.normcase(save_path):
        # 真实容器与扩展名不符时已改名，同步给 SongInfo，后续写曲目编号才不会找错文件
        song_info._save_path = new_path
        song_info.ext = os.path.splitext(new_path)[1].lstrip(".").lower() or getattr(song_info, "ext", None)
        print(f"已修正文件扩展名: {new_path}")
    return notes, issues


def _cleanup_old_exe():
    """新版启动时顺手清掉被替换下来的旧 exe 及超过 1 小时的残留暂存文件。"""
    app_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else os.getcwd()
    if getattr(sys, "frozen", False):
        stale = os.path.join(app_dir, "musicdownload.old.exe")
        try:
            if os.path.exists(stale):
                os.remove(stale)
                print(f"已清理旧版本: {stale}")
        except OSError:
            pass
    staging = os.path.join(app_dir, ".musicdl_staging")
    if os.path.isdir(staging):
        cutoff = time.time() - 3600
        try:
            for entry in os.listdir(staging):
                ep = os.path.join(staging, entry)
                try:
                    if os.path.getmtime(ep) < cutoff:
                        if os.path.isdir(ep):
                            shutil.rmtree(ep, ignore_errors=True)
                        else:
                            os.remove(ep)
                except OSError:
                    pass
        except OSError:
            pass


# 后台搜索线程：显示细粒度实时进度（按单曲解析进度推进），并输出过程详情
class SearchThread(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    log = pyqtSignal(str)
    def __init__(self, music_client, keyword, search_type, source_names=None, page_size=10):
        super().__init__()
        self.music_client = music_client
        self.keyword = keyword
        self.search_type = search_type
        self.source_names = source_names or {}
        self.page_size = max(1, int(page_size or 10))
        self._rich_progress = None
        self._rich_task_id = None
        self._progress_lock = None
        self._max_completed = 0

    def progress_snapshot(self):
        """返回 (已完成进度单位, 总进度单位)；结合单曲与整页进度，界面线程可安全调用。"""
        rich_progress, task_id, lock = self._rich_progress, self._rich_task_id, self._progress_lock
        if rich_progress is None or task_id is None:
            return 0, 0
        try:
            with lock:
                main_task = rich_progress.tasks[task_id]
                total_pages = int(main_task.total or 0)
                completed_pages = int(main_task.completed or 0)
                if total_pages <= 0:
                    return 0, 0

                page_size = self.page_size
                total_units = total_pages * page_size
                done_units = 0
                has_subtasks = False
                for t in rich_progress.tasks:
                    desc = str(getattr(t, 'description', '') or '')
                    if '._search >>>' not in desc:
                        continue
                    has_subtasks = True
                    if 'search results processed' in desc or '(Error:' in desc:
                        done_units += page_size
                    else:
                        done_units += min(int(t.completed or 0), max(0, page_size - 1))

                if not has_subtasks:
                    done_units = completed_pages * page_size
                elif completed_pages >= total_pages:
                    done_units = total_units
                else:
                    done_units = max(done_units, completed_pages * page_size)

                self._max_completed = max(self._max_completed, min(done_units, total_units))
                return self._max_completed, total_units
        except Exception:
            return 0, 0

    def _search_per_source(self):
        """按 musicdl 原有的方式并发搜索各音源，同时让界面拿到每个搜索请求的真实进度。"""
        sources = list(getattr(self.music_client, "music_sources", []) or [])
        if not sources or _RichProgress is None:
            self.log.emit("正在搜索音乐（无法获取细粒度进度）……")
            return self.music_client.search(keyword=self.keyword)

        self._progress_lock = threading.Lock()
        self._rich_progress = _RichProgress(disable=True)
        self._rich_task_id = self._rich_progress.add_task("Search From Sources", total=0)
        started_at = time.time()
        self.log.emit(f"开始搜索「{self.keyword}」，共 {len(sources)} 个音源")

        def search_func(source):
            source_cn = self.source_names.get(source, source)
            client = getattr(self.music_client, "music_clients", {}).get(source)
            if client is None:
                self.log.emit(f"✖ {source_cn}：客户端不可用，已跳过")
                return source, []
            self.log.emit(f"▶ {source_cn}：开始搜索")
            begin = time.time()
            try:
                items = client.search(
                    keyword=self.keyword,
                    num_threadings=self.music_client.clients_threadings.get(source, 5),
                    request_overrides=self.music_client.requests_overrides.get(source, {}),
                    rule=self.music_client.search_rules.get(source, {}),
                    main_process_context=self._rich_progress,
                    main_progress_id=self._rich_task_id,
                    main_progress_lock=self._progress_lock,
                )
            except Exception as err:
                self.log.emit(f"✖ {source_cn}：搜索失败（{err}）")
                return source, []
            found = len(items or [])
            self.log.emit(f"✔ {source_cn}：找到 {found} 首，用时 {time.time() - begin:.1f} 秒")
            return source, items

        with ThreadPoolExecutor(max_workers=min(len(sources), 10)) as pool:
            results = dict(pool.map(search_func, sources))
        total_found = sum(len(items or []) for items in results.values())
        self.log.emit(f"搜索结束：{len(sources)} 个音源共 {total_found} 首，用时 {time.time() - started_at:.1f} 秒")
        return results

    def run(self):
        stop_poll = threading.Event()

        def poll_progress():
            while not stop_poll.wait(0.12):
                completed, total = self.progress_snapshot()
                if total > 0:
                    self.progress.emit(completed, total)

        poller = threading.Thread(target=poll_progress, daemon=True)
        poller.start()
        try:
            if self.search_type == "搜索歌曲":
                results = self._search_per_source()
            else:
                self.log.emit("正在解析歌单链接……")
                results = self.music_client.parseplaylist(self.keyword)
                if not isinstance(results, dict):
                    results = {"歌单": results}
            completed, total = self.progress_snapshot()
            if total > 0:
                self.progress.emit(total, total)
            stop_poll.set()
            self.finished.emit(results)
        except Exception as e:
            stop_poll.set()
            self.error.emit(str(e))

_PKL_LOCK = threading.Lock()


def _staging_root_for(work_dir):
    """选择位于媒体库目录之外、尽量同盘的暂存根目录，避免未完成文件被 foobar 抢扫。"""
    if _temp_dir_for and work_dir:
        cand = _temp_dir_for(os.path.join(os.path.abspath(work_dir), 'dummy.flac'))
        if os.path.basename(cand) == '.musicdl_staging':
            return cand
    if work_dir:
        cur = os.path.abspath(work_dir)
        # 典型路径为 <save_dir>/<ClientName>/<AlbumFolder>，尝试取 save_dir 的上一级
        save_dir_guess = os.path.dirname(os.path.dirname(cur))
        parent = os.path.dirname(save_dir_guess) if save_dir_guess else ''
        if parent and parent != os.path.dirname(parent):
            staging = os.path.join(parent, '.musicdl_staging')
            try:
                os.makedirs(staging, exist_ok=True)
                return staging
            except OSError:
                pass
    return tempfile.gettempdir()


def _unique_dest_path(target_dir, filename, max_path=240):
    stem, ext = os.path.splitext(filename)
    max_stem = max(1, max_path - (len(os.path.abspath(target_dir)) + 1 + len(ext)))
    safe_stem = stem[:max_stem].rstrip(' .') or 'track'
    candidate = os.path.join(target_dir, safe_stem + ext)
    idx = 1
    while os.path.exists(candidate):
        suffix = f" ({idx})"
        trim_len = max(1, max_stem - len(suffix))
        candidate = os.path.join(target_dir, f"{safe_stem[:trim_len].rstrip(' .')}{suffix}{ext}")
        idx += 1
    return candidate


def _move_file_safe(src, dst):
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(src, dst)


def _merge_download_results_pkl(target_dir, item):
    pkl_path = os.path.join(target_dir, 'download_results.pkl')
    try:
        record = item.todict() if hasattr(item, 'todict') else dict(item)
    except Exception:
        return
    with _PKL_LOCK:
        items = []
        if os.path.exists(pkl_path):
            try:
                with open(pkl_path, 'rb') as fp:
                    loaded = pickle.load(fp)
                    if isinstance(loaded, list):
                        items = loaded
            except Exception:
                items = []
        items.append(record)
        fd, tmp_pkl = tempfile.mkstemp(prefix='dlres_', suffix='.tmp', dir=_staging_root_for(target_dir))
        try:
            with os.fdopen(fd, 'wb') as fp:
                pickle.dump(items, fp)
            _move_file_safe(tmp_pkl, pkl_path)
        except Exception:
            try:
                os.remove(tmp_pkl)
            except OSError:
                pass


def _promote_staged_item(item, orig_work_dir):
    """把暂存目录里写完所有元数据（含音轨号/封面）的音频及歌词移入最终目录。"""
    staged_audio = getattr(item, '_save_path', None) or getattr(item, 'save_path', None)
    if not staged_audio or not os.path.exists(staged_audio):
        return
    _makedirs_music_folder(orig_work_dir, exist_ok=True)
    final_audio = _unique_dest_path(orig_work_dir, os.path.basename(staged_audio))
    staged_lrc = os.path.splitext(staged_audio)[0] + '.lrc'
    if os.path.exists(staged_lrc):
        final_lrc = os.path.splitext(final_audio)[0] + '.lrc'
        _move_file_safe(staged_lrc, final_lrc)
    _move_file_safe(staged_audio, final_audio)
    item._save_path = final_audio
    item.work_dir = orig_work_dir
    _merge_download_results_pkl(orig_work_dir, item)


# 后台下载线程：逐首统计完成数量，显示真实进度与每首歌结果
class DownloadThread(QThread):
    finished = pyqtSignal(int)
    error = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    log = pyqtSignal(str)
    def __init__(self, music_client, song_infos, max_workers=4, enable_bilingual=True, lyric_layout='dual_line', zh_convert_mode='keep'):
        super().__init__()
        self.music_client = music_client
        self.song_infos = list(song_infos)
        self.max_workers = max(1, min(int(max_workers), max(1, len(self.song_infos))))
        self.enable_bilingual = enable_bilingual
        self.lyric_layout = lyric_layout
        self.zh_convert_mode = zh_convert_mode

    def _download_one(self, song_info):
        name = _song_text(song_info, "song_name") or "未知歌曲"
        singers = _song_text(song_info, "singers")
        label = f"{name} - {singers}" if singers else name
        orig_work_dir = getattr(song_info, "work_dir", None)
        use_staging = bool(orig_work_dir) and not getattr(song_info, "episodes", None)
        stage_dir = None
        try:
            target_song = song_info
            if use_staging:
                staging_root = _staging_root_for(orig_work_dir)
                stage_dir = tempfile.mkdtemp(prefix="dl_", dir=staging_root)
                target_song = copy.deepcopy(song_info)
                target_song.work_dir = stage_dir
                target_song._save_path = None
            _ensure_song_lyric(
                target_song,
                song_info,
                enable_bilingual=self.enable_bilingual,
                layout=self.lyric_layout,
                zh_convert=self.zh_convert_mode
            )
            try:
                downloaded = self.music_client.download(song_infos=[target_song])
            except Exception as err:
                return label, [], str(err), [], []
            results = list(downloaded or [])
            # musicdl 自带写标签，但它遇到文件被占用会静默回退；在移入目标目录前就地校验并补齐音轨号/封面
            notes, issues = [], []
            for item in results:
                item_notes, item_issues = _repair_downloaded_metadata(item)
                notes.extend(item_notes)
                issues.extend(item_issues)
                if use_staging:
                    _promote_staged_item(item, orig_work_dir)
            return label, results, None, notes, issues
        finally:
            if stage_dir and os.path.exists(stage_dir):
                shutil.rmtree(stage_dir, ignore_errors=True)

    def run(self):
        try:
            total = len(self.song_infos)
            if total == 0:
                self.finished.emit(0)
                return
            self.log.emit(f"开始下载：共 {total} 首歌曲（同时下载 {self.max_workers} 首）")
            self.progress.emit(0, total)
            downloaded_song_infos = []
            finished_count = 0
            failures = 0
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = [pool.submit(self._download_one, song_info) for song_info in self.song_infos]
                for future in as_completed(futures):
                    try:
                        label, results, error, notes, issues = future.result()
                    except Exception as err:
                        label, results, error, notes, issues = "未知歌曲", [], str(err), [], []
                    finished_count += 1
                    if results:
                        downloaded_song_infos.extend(results)
                        self.log.emit(f"✔ {label}")
                        if notes:
                            self.log.emit(f"   ↻ 元数据已补全：{'；'.join(notes)}")
                        if issues:
                            self.log.emit(f"   ⚠ 元数据仍异常：{'；'.join(issues)}")
                    else:
                        failures += 1
                        reason = f"（{error}）" if error else "（下载链接可能已失效）"
                        self.log.emit(f"✖ {label} {reason}")
                    self.progress.emit(finished_count, total)
            self.log.emit(f"下载结束：成功 {len(downloaded_song_infos)} 首，失败 {failures} 首")
            self.log.emit("正在补全专辑曲目编号……")
            _write_album_track_numbers(downloaded_song_infos, self.log.emit)
            self.finished.emit(len(downloaded_song_infos))
        except Exception as e:
            self.error.emit(str(e))

# 进度弹窗：显示实际进度、百分比，以及可展开/收起的搜索下载过程详情
class SimpleProgressDialog(QDialog):
    WIDTH = 480
    DETAILS_HEIGHT = 190

    def __init__(self, title, message, save_dir=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setWindowModality(Qt.ApplicationModal)
        self.setFixedWidth(self.WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(8)

        title_label = QLabel(title)
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setStyleSheet("font-size: 12pt; color: #2c3e50; font-weight: bold;")
        layout.addWidget(title_label)

        self.message_label = QLabel(message)
        self.message_label.setAlignment(Qt.AlignCenter)
        self.message_label.setWordWrap(True)
        self.message_label.setStyleSheet("font-size: 10pt; color: #34495e;")
        layout.addWidget(self.message_label)

        if save_dir:
            dir_label = QLabel(f"保存到：{save_dir}")
            dir_label.setAlignment(Qt.AlignCenter)
            dir_label.setStyleSheet("font-size: 9pt; color: #666;")
            dir_label.setWordWrap(True)
            layout.addWidget(dir_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("准备中…")
        self.progress_bar.setStyleSheet("""
            QProgressBar { border: 1px solid #d6dde5; border-radius: 6px; background-color: #eef1f5; height: 18px; text-align: center; color: #2c3e50; }
            QProgressBar::chunk { background-color: #0078d4; border-radius: 5px; }
        """)
        layout.addWidget(self.progress_bar)

        self.elapsed_label = QLabel("已用时 0 秒")
        self.elapsed_label.setAlignment(Qt.AlignCenter)
        self.elapsed_label.setStyleSheet("font-size: 9pt; color: #8a949e;")
        layout.addWidget(self.elapsed_label)
        self._start_ts = time.time()
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(500)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._elapsed_timer.start()

        details_row = QHBoxLayout()
        details_row.addStretch()
        self.check_details = QCheckBox("显示详情")
        self.check_details.setToolTip("展开/收起搜索或下载过程的详细信息")
        self.check_details.setStyleSheet("font-size: 9pt; color: #666;")
        details_row.addWidget(self.check_details)
        layout.addLayout(details_row)

        self.details_box = QPlainTextEdit()
        self.details_box.setReadOnly(True)
        self.details_box.setMaximumBlockCount(400)
        self.details_box.setFixedHeight(self.DETAILS_HEIGHT)
        self.details_box.setPlaceholderText("搜索 / 下载过程的信息会显示在这里")
        self.details_box.setStyleSheet("""
            QPlainTextEdit { font-family: Consolas, "Microsoft YaHei"; font-size: 9pt; color: #2c3e50;
                             background-color: #f7f9fb; border: 1px solid #e1e7ee; border-radius: 4px; padding: 4px; }
        """)
        layout.addWidget(self.details_box)

        self._collapsed_height = 0
        self._expanded_height = 0
        self.check_details.toggled.connect(self._on_details_toggled)
        visible = self._load_details_preference()
        self.check_details.blockSignals(True)
        self.check_details.setChecked(visible)
        self.check_details.blockSignals(False)
        self._apply_details_visibility(visible, initial=True)
        self._center_on_parent()

    def _update_elapsed(self):
        seconds = int(time.time() - self._start_ts)
        self.elapsed_label.setText(f"已用时 {seconds} 秒")

    def done(self, result):
        self._elapsed_timer.stop()
        super().done(result)

    @staticmethod
    def _load_details_preference():
        try:
            return QSettings("musicdl", "MusicDownloader").value("show_process_details", False, type=bool)
        except Exception:
            return False

    def _save_details_preference(self, visible):
        try:
            QSettings("musicdl", "MusicDownloader").setValue("show_process_details", bool(visible))
        except Exception:
            pass

    def _apply_details_visibility(self, visible, initial=False):
        # 只在第一次计算收起状态的高度，之后展开/收起使用缓存，弹窗会平滑变高变矮
        if not self._collapsed_height:
            self.details_box.setVisible(False)
            self.layout().activate()
            self.adjustSize()
            self._collapsed_height = self.height()
            self._expanded_height = self._collapsed_height + self.DETAILS_HEIGHT + 8
        self.details_box.setVisible(bool(visible))
        self.setFixedSize(self.WIDTH, self._expanded_height if visible else self._collapsed_height)
        if not initial:
            self._center_on_parent()

    def _on_details_toggled(self, checked):
        self._apply_details_visibility(checked)
        self._save_details_preference(checked)

    def _center_on_parent(self):
        parent = self.parent()
        if parent:
            self.move(parent.x() + (parent.width() - self.width()) // 2,
                      parent.y() + (parent.height() - self.height()) // 2)

    def set_progress(self, completed, total):
        """更新实际进度：completed / total 为已完成、总数（搜索请求数或歌曲数）。"""
        completed, total = int(completed or 0), int(total or 0)
        if total <= 0:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("准备中…")
            return
        total = max(total, 1)
        completed = min(max(completed, 0), total)
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(completed)
        percent = int(round(completed * 100 / total))
        self.progress_bar.setFormat(f"{completed} / {total}（{percent}%）")

    def append_log(self, text):
        """追加一行过程详情（带时间戳）。"""
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        self.details_box.appendPlainText(line)
        scrollbar = self.details_box.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

class HeaderSelectCheckBox(QCheckBox):
    """表头“选择”三态复选框：全选 / 全不选 / 不全选。

    点击时只发出“应勾选全部还是取消全部”的请求，由表格统一处理；
    勾选状态由行复选框的真实勾选数量同步，因此部分勾选时自动显示半选。
    """

    toggle_requested = pyqtSignal(bool)

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setTristate(True)

    def nextCheckState(self):
        # 用户点击：未全选（含半选）→ 全选；已全选 → 全不选
        self.toggle_requested.emit(self.checkState() != Qt.Checked)

# 流式布局
class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=-1, hspacing=-1, vspacing=-1):
        super(FlowLayout, self).__init__(parent)
        self._item_list = []
        self._hspacing = hspacing
        self._vspacing = vspacing
        self.setContentsMargins(margin, margin, margin, margin)
    def __del__(self):
        item = self.takeAt(0)
        while item:
            item = self.takeAt(0)
    def addItem(self, item):
        self._item_list.append(item)
    def horizontalSpacing(self):
        return self._hspacing if self._hspacing >= 0 else self.smartSpacing(QStyle.PM_LayoutHorizontalSpacing)
    def verticalSpacing(self):
        return self._vspacing if self._vspacing >= 0 else self.smartSpacing(QStyle.PM_LayoutVerticalSpacing)
    def count(self): return len(self._item_list)
    def itemAt(self, index): return self._item_list[index] if 0 <= index < len(self._item_list) else None
    def takeAt(self, index): return self._item_list.pop(index) if 0 <= index < len(self._item_list) else None
    def expandingDirections(self): return Qt.Orientations()
    def hasHeightForWidth(self): return True
    def heightForWidth(self, width): return self.calculateHeight(QRect(0, 0, width, 0), True)
    def setGeometry(self, rect):
        super(FlowLayout, self).setGeometry(rect)
        self.calculateHeight(rect, False)
    def sizeHint(self): return self.minimumSize()
    def minimumSize(self):
        size = QSize()
        for item in self._item_list:
            size = size.expandedTo(item.minimumSize())
        left, top, right, bottom = self.getContentsMargins()
        return size + QSize(left + right, top + bottom)
    def calculateHeight(self, rect, testOnly):
        left, top, right, bottom = self.getContentsMargins()
        effective = rect.adjusted(left, top, -right, -bottom)
        x, y = effective.x(), effective.y()
        lineHeight = 0
        for item in self._item_list:
            widget = item.widget()
            spaceX = self.horizontalSpacing()
            if spaceX == -1:
                spaceX = widget.style().layoutSpacing(QSizePolicy.PushButton, QSizePolicy.PushButton, Qt.Horizontal)
            spaceY = self.verticalSpacing()
            if spaceY == -1:
                spaceY = widget.style().layoutSpacing(QSizePolicy.PushButton, QSizePolicy.PushButton, Qt.Vertical)
            nextX = x + item.sizeHint().width() + spaceX
            if nextX - spaceX > effective.right() and lineHeight > 0:
                x, y = effective.x(), y + lineHeight + spaceY
                nextX, lineHeight = x + item.sizeHint().width() + spaceX, 0
            if not testOnly:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))
            x, lineHeight = nextX, max(lineHeight, item.sizeHint().height())
        return y + lineHeight - rect.y() + bottom
    def smartSpacing(self, pm):
        parent = self.parent()
        if not parent: return -1
        return parent.style().pixelMetric(pm, None, parent) if parent.isWidgetType() else parent.spacing()

# 主窗口
class MusicDownloader(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("音乐下载器 · 宇宙超级无敌版")
        self.setMinimumSize(1200, 800)
        self.resize(1400, 900)
        self.setStyleSheet(self.get_modern_style())
        
        # 音乐源中英文映射
        self.source_map_cn_to_en = {
            "苹果音乐": "AppleMusicClient", "Deezer": "DeezerMusicClient", "5sing": "FiveSingMusicClient",
            "Jamendo": "JamendoMusicClient", "Joox": "JooxMusicClient", "酷我音乐": "KuwoMusicClient",
            "酷狗音乐": "KugouMusicClient", "咪咕音乐": "MiguMusicClient", "网易云音乐": "NeteaseMusicClient",
            "QQ音乐": "QQMusicClient", "千千音乐": "QianqianMusicClient", "Qobuz": "QobuzMusicClient",
            "SoundCloud": "SoundCloudMusicClient", "StreetVoice": "StreetVoiceMusicClient", "汽水音乐": "SodaMusicClient",
            "Spotify": "SpotifyMusicClient", "TIDAL": "TIDALMusicClient"
        }
        self.source_map_en_to_cn = {v: k for k, v in self.source_map_cn_to_en.items()}
        
        # 保存搜索结果
        self.search_results = {}
        self.music_records = {}
        self.music_client = None
        self.image_threads = []
        self.cover_pool = QThreadPool(self)
        self.cover_pool.setMaxThreadCount(6)
        self.cover_signals = CoverDownloadSignals(self)
        self.cover_signals.finished.connect(self.on_cover_downloaded)
        self.cover_signals.error.connect(self.on_cover_error)
        self.cover_pending_rows = {}   # 封面 URL -> [(row, generation)]
        self.current_right_click_row = -1
        # 排序相关
        self.all_songs = []            # 本次搜索的全部歌曲（音乐源原始顺序）
        self.render_generation = 0     # 每次重排递增，用于丢弃过期的封面回调
        self.cover_cache = {}          # 封面 URL -> QPixmap，重排时不再重复下载
        
        # 设置当前路径和保存目录
        self.current_dir = os.getcwd()
        self.save_dir = os.path.join(self.current_dir, "已下载音乐")
        _makedirs_music_folder(self.save_dir, exist_ok=True)
        
        # 搜索后直接下载开关
        self.auto_download_after_search = False
        
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(16)
        
        self.setup_top(main_layout)
        self.setup_table(main_layout)
        
        if not MUSICDL_AVAILABLE:
            QMessageBox.warning(self, "警告", "musicdl 库未安装！\n请运行: pip install musicdl")

    def get_modern_style(self):
        return """
        QMainWindow { background-color: #f8f9fa; }
        QGroupBox {
            font-size: 11pt; font-weight: bold; color: #2c3e50;
            border: 1px solid #dce1e6; border-radius: 8px;
            margin-top: 12px; padding-top: 8px;
        }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; }
        QCheckBox { padding: 3px 1px; }
        QLineEdit, QComboBox, QSpinBox {
            border: 1px solid #d1d5db; border-radius: 6px;
            padding: 6px 10px; background: white; min-height: 26px;
        }
        QPushButton {
            border: none; border-radius: 6px; padding: 8px 16px;
            background-color: #0078d4; color: white; font-weight: bold;
        }
        QPushButton:hover { background-color: #1089e5; }
        QPushButton:pressed { background-color: #0063b8; }
        QTableWidget {
            border: 1px solid #e5e7eb; border-radius: 8px;
            background: white; gridline-color: #f3f4f6;
        }
        QHeaderView::section { background: #e9ecef; border: none; padding: 8px; }
        """

    def setup_top(self, parent_layout):
        layout = QVBoxLayout()
        layout.setSpacing(12)
        
        # 音乐来源
        group = QGroupBox("音乐来源")
        flow = FlowLayout()
        self.source_checkboxes = []
        for chinese_name in self.source_map_cn_to_en.keys():
            cb = QCheckBox(chinese_name)
            if chinese_name in DEFAULT_SOURCES:
                cb.setChecked(True)
            self.source_checkboxes.append(cb)
            flow.addWidget(cb)
        group.setLayout(flow)
        layout.addWidget(group)
        
        # 结果数量限制 + 保存目录 + 自动下载选项
        h1 = QHBoxLayout()
        label_limit = QLabel("结果数量：")
        self.spin_limit = QSpinBox()
        self.spin_limit.setRange(1, 100)
        self.spin_limit.setValue(100)
        self.spin_limit.setSuffix(" 条/源")
        
        label_save = QLabel("保存目录：")
        self.save_dir_edit = QLineEdit(self.save_dir)
        self.save_dir_edit.setReadOnly(True)
        self.btn_browse = QPushButton("浏览...")
        self.btn_browse.clicked.connect(self.on_browse_save_dir)
        
        # 自动下载复选框
        self.check_auto_download = QCheckBox("🚀 搜索后自动下载全部")
        self.check_auto_download.stateChanged.connect(self.on_auto_download_toggle)
        
        h1.addWidget(label_limit)
        h1.addWidget(self.spin_limit)
        h1.addSpacing(30)
        h1.addWidget(label_save)
        h1.addWidget(self.save_dir_edit)
        h1.addWidget(self.btn_browse)
        h1.addSpacing(30)
        h1.addWidget(self.check_auto_download)
        h1.addStretch()
        layout.addLayout(h1)
        
        # 搜索区域
        h2 = QHBoxLayout()
        self.search_mode = QComboBox()
        self.search_mode.addItems(["搜索歌曲", "解析歌单链接"])
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入关键词或歌单链接...")
        self.btn_search = QPushButton("搜索")
        self.btn_search.clicked.connect(self.on_search)
        h2.addWidget(self.search_mode)
        h2.addWidget(self.search_edit)
        h2.addWidget(self.btn_search)
        layout.addLayout(h2)
        
        parent_layout.addLayout(layout)

    def setup_table(self, parent_layout):
        layout = QVBoxLayout()
        
        # 下载范围选择
        batch = QHBoxLayout()
        self.combo_download_scope = QComboBox()
        self.combo_download_scope.addItems(["全选", "勾选", "未勾选"])
        self.combo_download_scope.setCurrentText(DEFAULT_DOWNLOAD_SCOPE)
        self.btn_download = QPushButton("下载选中")
        self.btn_download.clicked.connect(self.on_download)
        self.btn_download.setEnabled(False)
        batch.addWidget(QLabel("下载范围："))
        batch.addWidget(self.combo_download_scope)

        # 排序：默认保持音乐源顺序，可按歌手 / 专辑等字段重新排列
        batch.addSpacing(24)
        batch.addWidget(QLabel("排序："))
        self.combo_sort = QComboBox()
        self.combo_sort.addItems(SORT_MODES)
        self.combo_sort.setCurrentText(DEFAULT_SORT_MODE)
        self.combo_sort.setMinimumWidth(110)
        self.combo_sort.setToolTip("按所选字段重新排列搜索结果，缺失的字段排在最后")
        self.combo_sort.currentIndexChanged.connect(self.on_sort_changed)
        batch.addWidget(self.combo_sort)
        self.check_sort_desc = QCheckBox("降序")
        self.check_sort_desc.setToolTip("勾选后按倒序排列")
        self.check_sort_desc.stateChanged.connect(self.on_sort_changed)
        batch.addWidget(self.check_sort_desc)

        # 双语歌词选项
        batch.addSpacing(20)
        self.check_bilingual = QCheckBox("🌐 双语歌词")
        self.check_bilingual.setToolTip("下载外语歌时自动匹配并合并中文翻译（网易云/QQ音乐）")
        bilingual_saved = QSettings("musicdl", "MusicDownloader").value("enable_bilingual_lyric", True, type=bool)
        self.check_bilingual.setChecked(bilingual_saved)
        self.check_bilingual.stateChanged.connect(self.on_bilingual_toggle)
        batch.addWidget(self.check_bilingual)

        self.combo_lyric_layout = QComboBox()
        self.combo_lyric_layout.addItem("双行对齐", "dual_line")
        self.combo_lyric_layout.addItem("单行拼合 (｜)", "inline")
        self.combo_lyric_layout.addItem("仅中文翻译", "translation_only")
        saved_layout = QSettings("musicdl", "MusicDownloader").value("bilingual_lyric_layout", "dual_line", type=str)
        idx = self.combo_lyric_layout.findData(saved_layout)
        if idx >= 0:
            self.combo_lyric_layout.setCurrentIndex(idx)
        self.combo_lyric_layout.setToolTip("双语歌词排版格式：\n• 双行对齐：同一时间戳两行（主流播放器推荐）\n• 单行拼合：原句与译文单行内拼接（车机系统推荐）\n• 仅中文翻译：只保留中文译文")
        self.combo_lyric_layout.currentIndexChanged.connect(self.on_lyric_layout_changed)
        batch.addWidget(self.combo_lyric_layout)

        # 歌词简繁转换选项
        batch.addSpacing(15)
        self.combo_zh_convert = QComboBox()
        self.combo_zh_convert.addItem("保持原样 (字形)", "keep")
        self.combo_zh_convert.addItem("统一为简体中文", "t2s")
        self.combo_zh_convert.addItem("统一为繁体中文", "s2t")
        saved_zh_convert = QSettings("musicdl", "MusicDownloader").value("zh_convert_mode", "keep", type=str)
        idx_zh = self.combo_zh_convert.findData(saved_zh_convert)
        if idx_zh >= 0:
            self.combo_zh_convert.setCurrentIndex(idx_zh)
        self.combo_zh_convert.setToolTip("歌词简繁中文转换：\n• 保持原样：遵循源站原始字形（港台/粤语歌保留繁体）\n• 统一为简体中文：自动将繁体歌词及翻译转为规范简体（推荐车机及内地用户）\n• 统一为繁体中文：自动将简体歌词及翻译转为繁体")
        self.combo_zh_convert.currentIndexChanged.connect(self.on_zh_convert_changed)
        batch.addWidget(self.combo_zh_convert)

        batch.addStretch()
        batch.addWidget(self.btn_download)
        layout.addLayout(batch)
        
        # 歌曲表格
        self.results_table = QTableWidget()
        self.results_table.setColumnCount(9)
        self.results_table.setHorizontalHeaderLabels([
            "", "专辑封面", "歌曲名", "歌手", 
            "专辑", "格式", "大小", "时长", "来源"
        ])
        self.results_table.horizontalHeader().setStretchLastSection(True)
        
        # 设置表格属性
        self.results_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.results_table.customContextMenuRequested.connect(self.show_table_context_menu)
        
        # 设置列宽
        self.results_table.setColumnWidth(0, 80)
        self.results_table.setColumnWidth(1, 70)
        self.results_table.setColumnWidth(2, 240)
        self.results_table.setColumnWidth(3, 140)
        self.results_table.setColumnWidth(4, 190)
        self.results_table.setColumnWidth(5, 60)
        self.results_table.setColumnWidth(6, 90)
        self.results_table.setColumnWidth(7, 70)

        # 表头“选择”三态复选框（覆盖第 0 列表头）：全选 / 全不选 / 不全选（半选）
        header = self.results_table.horizontalHeader()
        self.header_select = HeaderSelectCheckBox("选择", header.viewport())
        self.header_select.setToolTip("全选 / 全不选；勾选部分歌曲时显示为“不全选”")
        self.header_select.toggle_requested.connect(self.on_header_select_toggle)
        header.sectionResized.connect(self._position_header_select)
        header.geometriesChanged.connect(self._position_header_select)
        self.results_table.horizontalScrollBar().valueChanged.connect(self._position_header_select)
        QTimer.singleShot(0, self._position_header_select)

        self.results_table.verticalHeader().setDefaultSectionSize(70)
        layout.addWidget(self.results_table)
        
        parent_layout.addLayout(layout)

    # 自动下载开关切换（修改：不改变界面布局）
    def on_auto_download_toggle(self, state):
        self.auto_download_after_search = (state == Qt.Checked)
        # 界面布局保持不变，只记录状态

    def show_table_context_menu(self, pos):
        item = self.results_table.itemAt(pos)
        if not item: return
        row = item.row()
        self.current_right_click_row = row
        
        menu = QMenu(self)
        song_name_item = self.results_table.item(row, 2)
        singer_item = self.results_table.item(row, 3)
        action_text = f"下载：{song_name_item.text()} - {singer_item.text()}" if (song_name_item and singer_item) else "下载此歌曲"
        download_action = QAction(action_text, self)
        download_action.triggered.connect(self.download_current_row)
        menu.addAction(download_action)
        
        menu.addSeparator()
        select_all_action = QAction("全选所有歌曲", self)
        select_all_action.triggered.connect(self.select_all_songs)
        menu.addAction(select_all_action)
        deselect_all_action = QAction("取消全选", self)
        deselect_all_action.triggered.connect(self.deselect_all_songs)
        menu.addAction(deselect_all_action)
        
        menu.exec_(self.results_table.mapToGlobal(pos))

    def _connect_progress(self, dialog, thread):
        """把后台线程的真实进度和过程详情接到进度弹窗上。"""
        thread.progress.connect(dialog.set_progress)
        thread.log.connect(dialog.append_log)

    def download_current_row(self):
        if self.current_right_click_row < 0 or not self.music_client: return
        if str(self.current_right_click_row) not in self.music_records: return
        
        song_info = self.music_records[str(self.current_right_click_row)]
        song_name = song_info.get('song_name', '未知歌曲')
        singers = ', '.join(song_info.get('singers', []))
        
        reply = QMessageBox.question(
            self, "确认下载", 
            f"确定要下载这首歌曲吗？\n\n{song_name} - {singers}\n\n保存目录：{self.save_dir}",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes: return
        
        dlg = SimpleProgressDialog("下载中", f"正在下载：{song_name}", self.save_dir, self)
        dlg.show()
        enable_bi, layout, zh_convert = self.get_lyric_settings()
        self.download_thread = DownloadThread(
            self.music_client, [song_info],
            enable_bilingual=enable_bi, lyric_layout=layout, zh_convert_mode=zh_convert
        )
        self._connect_progress(dlg, self.download_thread)
        
        def on_finished(downloaded_count):
            dlg.accept()
            if downloaded_count == 0:
                QMessageBox.warning(self, "下载结果", f"⚠️ 没有下载到文件。\n\n{song_name} - {singers}\n\n下载链接可能已失效，请重新搜索后再试。")
                return
            QMessageBox.information(self, "完成", f"下载完成！\n\n{song_name} - {singers}\n\n文件保存在：{self.save_dir}")
        def on_error(error_msg):
            dlg.accept()
            QMessageBox.critical(self, "错误", f"下载失败：{error_msg}")
        
        self.download_thread.finished.connect(on_finished)
        self.download_thread.error.connect(on_error)
        self.download_thread.start()

    def select_all_songs(self):
        self._set_all_checked(True)

    def deselect_all_songs(self):
        self._set_all_checked(False)

    def _set_all_checked(self, checked):
        """批量勾选/取消时先屏蔽逐行同步，最后统一更新一次表头状态。"""
        self._bulk_selecting = True
        try:
            for row in range(self.results_table.rowCount()):
                try:
                    checkbox = self.results_table.cellWidget(row, 0).findChild(QCheckBox)
                    if checkbox is not None:
                        checkbox.setChecked(checked)
                except Exception:
                    pass
        finally:
            self._bulk_selecting = False
        self._sync_header_select()

    def _position_header_select(self, *args):
        """把表头“选择”复选框对齐到第 0 列表头的位置。"""
        if not hasattr(self, "header_select"):
            return
        header = self.results_table.horizontalHeader()
        x = header.sectionViewportPosition(0)
        width = header.sectionSize(0)
        self.header_select.setVisible(x + width > 0)
        self.header_select.setGeometry(x, 0, width, header.height())
        self.header_select.raise_()

    def on_header_select_toggle(self, should_check):
        """表头复选框请求：True = 全选，False = 全不选。"""
        self._set_all_checked(should_check)

    def _sync_header_select(self, *args):
        """按各行真实勾选数量同步表头状态：全选 / 全不选 / 不全选（半选）。"""
        if not hasattr(self, "header_select") or getattr(self, "_bulk_selecting", False):
            return
        total = self.results_table.rowCount()
        checked = 0
        for row in range(total):
            try:
                checkbox = self.results_table.cellWidget(row, 0).findChild(QCheckBox)
                if checkbox is not None and checkbox.isChecked():
                    checked += 1
            except Exception:
                pass
        if total <= 0 or checked <= 0:
            state = Qt.Unchecked
        elif checked >= total:
            state = Qt.Checked
        else:
            state = Qt.PartiallyChecked
        self.header_select.blockSignals(True)
        self.header_select.setCheckState(state)
        self.header_select.blockSignals(False)

    def on_browse_save_dir(self):
        dir_path = QFileDialog.getExistingDirectory(self, "选择保存目录", self.current_dir)
        if dir_path:
            self.save_dir = dir_path
            self.save_dir_edit.setText(dir_path)

    def init_music_client(self):
        if not MUSICDL_AVAILABLE: return None
        _makedirs_music_folder(self.save_dir, exist_ok=True)
        my_init_music_clients_cfg = {}
        clients_threadings = {}
        src_names = []
        limit = self.spin_limit.value()
        page_workers = min(max(5, (limit + 9) // 10), 10)
        for source in self.get_selected_sources():
            my_init_music_clients_cfg[source] = {
                'search_size_per_source': limit,
                'work_dir': self.save_dir,
                'maintain_session': True,
                'max_retries': 2,
            }
            clients_threadings[source] = page_workers
            src_names.append(source)
        if not src_names:
            QMessageBox.warning(self, "提示", "请至少选择一个音乐来源！")
            return None
        try:
            return musicdl.MusicClient(
                music_sources=src_names,
                init_music_clients_cfg=my_init_music_clients_cfg,
                clients_threadings=clients_threadings,
            )
        except Exception as e:
            QMessageBox.critical(self, "错误", f"初始化 musicdl 客户端失败：{str(e)}")
            return None

    def get_selected_sources(self):
        return [self.source_map_cn_to_en[cb.text()] for cb in self.source_checkboxes if cb.isChecked()]

    def get_file_format(self, song_info):
        format_fields = ['format', 'ext', 'file_format', 'type']
        for field in format_fields:
            if field in song_info and song_info[field]:
                return str(song_info[field]).upper()
        if 'download_url' in song_info and song_info['download_url']:
            url = song_info['download_url'].lower()
            if '.mp3' in url: return 'MP3'
            elif '.flac' in url: return 'FLAC'
            elif '.wav' in url: return 'WAV'
            elif '.m4a' in url: return 'M4A'
            elif '.aac' in url: return 'AAC'
        return '未知'

    def get_album_image_url(self, song_info):
        image_fields = ['cover', 'album_cover', 'pic', 'picture', 'img', 'image', 'album_img', 'album_pic', 'cover_url', 'pic_url']
        for field in image_fields:
            if field in song_info and song_info[field]:
                url = str(song_info[field])
                if url.startswith('http'): return url
        return ''

    # ---------------- 搜索结果表格：重排 / 渲染 ----------------
    def _field_text(self, value):
        """把 musicdl 返回的字段统一成字符串（歌手等字段可能是 list）。"""
        if isinstance(value, (list, tuple)):
            return ", ".join(str(v) for v in value if v not in (None, ""))
        return "" if value is None else str(value)

    @staticmethod
    def _duration_seconds(text):
        """把 “03:45” / “1:02:33” 形式的时长转成秒数，失败返回 None。"""
        text = str(text or "").strip()
        if not text:
            return None
        try:
            parts = [float(part) for part in text.split(":")]
        except ValueError:
            return None
        if not parts or len(parts) > 3:
            return None
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds

    @staticmethod
    def _size_bytes(text):
        """把 “3.5MB” 形式的文件大小转成字节数，失败返回 None。"""
        text = str(text or "").strip().upper().replace(" ", "")
        match = re.match(r"^([\d.]+)([KMGT]?B?)$", text)
        if not match:
            return None
        try:
            value = float(match.group(1))
        except ValueError:
            return None
        unit = match.group(2) or "B"
        factor = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}.get(unit)
        return value * factor if factor else None

    def _song_sort_key(self, song, mode):
        """返回某首歌在指定排序方式下的排序键；None 表示该字段缺失，排到最后。"""
        singers = self._field_text(song.get("singers")).strip()
        album = self._field_text(song.get("album")).strip()
        name = self._field_text(song.get("song_name")).strip()
        if mode == "按歌手":
            return (singers.lower(), album.lower(), name.lower()) if singers else None
        if mode == "按专辑":
            return (album.lower(), singers.lower(), name.lower()) if album else None
        if mode == "按歌名":
            return (name.lower(),) if name else None
        if mode == "按时长":
            seconds = self._duration_seconds(song.get("duration"))
            return (seconds,) if seconds is not None else None
        if mode == "按格式":
            file_format = self.get_file_format(song)
            return (file_format.lower(),) if file_format and file_format != "未知" else None
        if mode == "按大小":
            size = self._size_bytes(song.get("file_size"))
            return (size,) if size is not None else None
        if mode == "按来源":
            source = self._field_text(song.get("source"))
            label = self.source_map_en_to_cn.get(source, source).strip()
            return (label.lower(),) if label else None
        return None

    def sorted_songs(self):
        """按当前下拉框的选择返回排序后的歌曲列表（不修改 self.all_songs）。"""
        songs = list(self.all_songs)
        mode = self.combo_sort.currentText() if hasattr(self, "combo_sort") else SORT_MODES[0]
        descending = bool(self.check_sort_desc.isChecked()) if hasattr(self, "check_sort_desc") else False
        if mode == "默认顺序":
            return list(reversed(songs)) if descending else songs

        known, unknown = [], []
        for index, song in enumerate(songs):
            key = self._song_sort_key(song, mode)
            if key is None:
                unknown.append(song)
            else:
                known.append((key, index, song))
        known.sort(key=lambda item: (item[0], item[1]))
        ordered = [item[2] for item in known]
        if descending:
            ordered.reverse()
        # 字段缺失的歌曲始终排最后，方便先处理信息完整的条目
        return ordered + unknown

    def _song_key(self, song):
        """用于重排时保留用户勾选状态的身份标识。"""
        return (self._field_text(song.get("source")), self._field_text(song.get("song_name")),
                self._field_text(song.get("singers")), self._field_text(song.get("album")))

    def _checked_song_keys(self):
        keys = set()
        for row in range(self.results_table.rowCount()):
            song = self.music_records.get(str(row))
            if not song:
                continue
            try:
                checkbox = self.results_table.cellWidget(row, 0).findChild(QCheckBox)
            except Exception:
                checkbox = None
            if checkbox is not None and checkbox.isChecked():
                keys.add(self._song_key(song))
        return keys

    def _set_cover_label(self, row, pixmap):
        label = QLabel()
        label.setPixmap(pixmap)
        label.setAlignment(Qt.AlignCenter)
        self.results_table.setCellWidget(row, 1, label)

    def on_bilingual_toggle(self, state):
        enabled = bool(state == Qt.Checked)
        QSettings("musicdl", "MusicDownloader").setValue("enable_bilingual_lyric", enabled)

    def on_lyric_layout_changed(self, index):
        layout = self.combo_lyric_layout.currentData()
        QSettings("musicdl", "MusicDownloader").setValue("bilingual_lyric_layout", layout)

    def on_zh_convert_changed(self, index):
        mode = self.combo_zh_convert.currentData()
        QSettings("musicdl", "MusicDownloader").setValue("zh_convert_mode", mode)

    def get_lyric_settings(self):
        enabled = self.check_bilingual.isChecked() if hasattr(self, 'check_bilingual') else True
        layout = self.combo_lyric_layout.currentData() if hasattr(self, 'combo_lyric_layout') else 'dual_line'
        zh_convert = self.combo_zh_convert.currentData() if hasattr(self, 'combo_zh_convert') else 'keep'
        return enabled, layout, zh_convert

    def on_sort_changed(self, *args):
        """排序方式变化后立即重排表格（保留勾选状态，封面走缓存）。"""
        if not self.all_songs:
            return
        self.render_songs()

    def render_songs(self):
        songs = self.sorted_songs()
        checked = self._checked_song_keys()
        self.render_generation += 1
        generation = self.render_generation

        self.results_table.setRowCount(0)
        self.results_table.setRowCount(len(songs))
        self.music_records = {}
        self.cover_pending_rows = {}
        self.image_threads = [t for t in self.image_threads if t.isRunning()]

        for row, song in enumerate(songs):
            w = QWidget()
            lay = QHBoxLayout(w)
            checkbox = QCheckBox()
            checkbox.setChecked(self._song_key(song) in checked)
            checkbox.stateChanged.connect(self._sync_header_select)
            lay.addWidget(checkbox)
            lay.setAlignment(Qt.AlignCenter)
            lay.setContentsMargins(0, 0, 0, 0)
            self.results_table.setCellWidget(row, 0, w)

            try:
                singers = self._field_text(song.get('singers'))
                song_name = self._field_text(song.get('song_name'))
                file_size = self._field_text(song.get('file_size'))
                duration = self._field_text(song.get('duration'))
                album = self._field_text(song.get('album'))
                source = self._field_text(song.get('source'))
                file_format = self.get_file_format(song)
                source_cn = self.source_map_en_to_cn.get(source, source)

                items = ['', song_name, singers, album, file_format, file_size, duration, source_cn]
                for column, item in enumerate(items):
                    if column == 0:
                        continue
                    table_item = QTableWidgetItem(str(item))
                    table_item.setTextAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
                    self.results_table.setItem(row, column + 1, table_item)

                self.music_records[str(row)] = song

                album_image_url = self.get_album_image_url(song)
                if album_image_url:
                    cached = self.cover_cache.get(album_image_url)
                    if cached is not None:
                        self._set_cover_label(row, cached)
                    else:
                        pending = self.cover_pending_rows.setdefault(album_image_url, [])
                        pending.append((row, generation))
                        if len(pending) == 1:
                            self.cover_pool.start(CoverDownloadRunnable(album_image_url, generation, self.cover_signals))
            except Exception as e:
                print(f"处理第 {row} 行时出错: {e}")

        self.btn_download.setEnabled(len(songs) > 0)
        self._sync_header_select()

    def load_table_with_results(self, search_results):
        self.search_results = search_results
        self.cover_cache = {}

        # 收集所有歌曲（按音乐源返回顺序，排序只影响显示，不改这里）
        all_songs = []
        for per_source_search_results in search_results.values():
            all_songs.extend(per_source_search_results)
        self.all_songs = all_songs

        self.render_songs()

        # 【修改】如果开启了自动下载，直接开始下载（不弹窗确认）
        if self.auto_download_after_search and all_songs:
            self.auto_download_all_songs(all_songs)
        else:
            QMessageBox.information(self, "完成", f"搜索完成！共找到 {len(all_songs)} 首歌曲\n专辑图片正在后台加载中...")

    # 自动下载所有歌曲（修改：不弹窗确认，直接下载，弹窗显示保存位置）
    def auto_download_all_songs(self, all_songs):
        if not all_songs or not self.music_client: return
        
        # 直接显示下载弹窗，显示保存位置
        dlg = SimpleProgressDialog("🚀 自动下载中", f"正在下载 {len(all_songs)} 首歌曲", self.save_dir, self)
        dlg.show()
        
        enable_bi, layout, zh_convert = self.get_lyric_settings()
        self.download_thread = DownloadThread(
            self.music_client, all_songs,
            enable_bilingual=enable_bi, lyric_layout=layout, zh_convert_mode=zh_convert
        )
        self._connect_progress(dlg, self.download_thread)
        
        def on_finished(downloaded_count):
            dlg.accept()
            if downloaded_count == 0:
                QMessageBox.warning(self, "下载结果", f"⚠️ 本次没有下载到任何文件。\n\n保存目录：{self.save_dir}")
                return
            QMessageBox.information(self, "🎉 下载完成", f"成功下载 {downloaded_count} 首歌曲！\n保存目录：{self.save_dir}")
        
        def on_error(error_msg):
            dlg.accept()
            QMessageBox.critical(self, "❌ 下载失败", f"批量下载失败：{error_msg}")
        
        self.download_thread.finished.connect(on_finished)
        self.download_thread.error.connect(on_error)
        self.download_thread.start()

    def on_cover_downloaded(self, image_url, qimage, generation=0):
        pixmap = QPixmap.fromImage(qimage)
        if image_url and not pixmap.isNull():
            self.cover_cache[image_url] = pixmap  # 即使已重排也缓存，下次重排直接复用
        if generation != self.render_generation:
            return
        rows = self.cover_pending_rows.pop(image_url, [])
        for row, gen in rows:
            if gen != self.render_generation:
                continue
            try:
                self._set_cover_label(row, pixmap)
            except Exception as e:
                print(f"显示图片失败: {e}")

    def on_cover_error(self, image_url, generation=0):
        if generation != self.render_generation:
            return
        rows = self.cover_pending_rows.pop(image_url, [])
        for row, gen in rows:
            if gen != self.render_generation:
                continue
            self.on_image_error(row, gen, image_url)

    def on_image_downloaded(self, row, pixmap, generation=0, image_url=''):
        if image_url:
            self.cover_cache[image_url] = pixmap  # 即使已重排也缓存，下次重排直接复用
        if generation != self.render_generation:
            return  # 表格已重排，旧线程的结果不再显示
        try:
            self._set_cover_label(row, pixmap)
        except Exception as e:
            print(f"显示图片失败: {e}")

    def on_image_error(self, row, generation=0, image_url=''):
        if generation != self.render_generation:
            return
        try:
            label = QLabel("🎵")
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet("font-size: 24px; color: #ccc;")
            self.results_table.setCellWidget(row, 1, label)
        except Exception as e:
            print(f"显示占位符失败: {e}")

    def get_songs_by_download_scope(self):
        scope = self.combo_download_scope.currentText()
        songs_to_download = []
        for row in range(self.results_table.rowCount()):
            try:
                checkbox = self.results_table.cellWidget(row, 0).findChild(QCheckBox)
                is_checked = checkbox.isChecked()
                if (scope == "全选" or 
                    (scope == "勾选" and is_checked) or 
                    (scope == "未勾选" and not is_checked)):
                    if str(row) in self.music_records:
                        songs_to_download.append(self.music_records[str(row)])
            except Exception as e:
                print(f"获取下载列表时出错: {e}")
        return songs_to_download

    def on_search(self):
        keyword = self.search_edit.text().strip()
        if not keyword:
            QMessageBox.warning(self, "提示", "请输入搜索内容！")
            return
        self.music_client = self.init_music_client()
        if not self.music_client: return
        dlg = SimpleProgressDialog("搜索中", f"正在搜索「{keyword}」…", None, self)
        dlg.show()
        self.search_thread = SearchThread(self.music_client, keyword, self.search_mode.currentText(), self.source_map_en_to_cn)
        self._connect_progress(dlg, self.search_thread)
        
        def on_finished(results):
            dlg.accept()
            self.load_table_with_results(results)
        def on_error(error_msg):
            dlg.accept()
            QMessageBox.critical(self, "错误", f"搜索失败：{error_msg}")
        
        self.search_thread.finished.connect(on_finished)
        self.search_thread.error.connect(on_error)
        self.search_thread.start()

    def count_downloaded_files(self):
        source_counts = {}
        for item in os.listdir(self.save_dir):
            item_path = os.path.join(self.save_dir, item)
            if os.path.isdir(item_path) and item in self.source_map_en_to_cn:
                file_count = 0
                for root_dir, _, filenames in os.walk(item_path):
                    file_count += sum(1 for filename in filenames if filename.lower().endswith(('.mp3', '.m4a', '.flac', '.wav', '.aac', '.ogg')))
                if file_count > 0:
                    source_counts[self.source_map_en_to_cn[item]] = {"path": item_path, "count": file_count}
        return source_counts

    def on_download(self):
        if not self.music_client:
            QMessageBox.warning(self, "提示", "请先搜索音乐！")
            return
        songs_to_download = self.get_songs_by_download_scope()
        if not songs_to_download:
            QMessageBox.warning(self, "提示", "没有符合条件的歌曲！")
            return
        reply = QMessageBox.question(
            self, "确认下载",
            f"确定要下载 {len(songs_to_download)} 首歌曲吗？\n保存目录：{self.save_dir}",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes: return
        
        dlg = SimpleProgressDialog("下载中", f"正在下载 {len(songs_to_download)} 首歌曲", self.save_dir, self)
        dlg.show()
        enable_bi, layout, zh_convert = self.get_lyric_settings()
        self.download_thread = DownloadThread(
            self.music_client, songs_to_download,
            enable_bilingual=enable_bi, lyric_layout=layout, zh_convert_mode=zh_convert
        )
        self._connect_progress(dlg, self.download_thread)
        
        def on_finished(downloaded_count):
            dlg.accept()
            if downloaded_count == 0:
                QMessageBox.warning(self, "下载结果", f"⚠️ 本次没有下载到任何文件。\n\n📁 保存目录：{self.save_dir}\n\n可能原因：下载链接已失效或网络超时，请重新搜索后再试。")
                return
            source_counts = self.count_downloaded_files()
            message = f"✅ 下载完成！\n\n📁 总保存目录：{self.save_dir}\n📊 本次共下载：{downloaded_count} 首歌曲\n\n📂 各音乐源累计文件（含历史）：\n"
            for source_name, info in source_counts.items():
                message += f"  • {source_name}: {info['count']} 个音频\n    路径：{info['path']}\n"
            QMessageBox.information(self, "下载完成", message)
        def on_error(error_msg):
            dlg.accept()
            QMessageBox.critical(self, "错误", f"下载失败：{error_msg}")
        
        self.download_thread.finished.connect(on_finished)
        self.download_thread.error.connect(on_error)
        self.download_thread.start()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    _cleanup_old_exe()
    win = MusicDownloader()
    win.show()
    sys.exit(app.exec_())